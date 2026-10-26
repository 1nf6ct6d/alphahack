"""Offline tests for source policy, pinned transport and news ingestion."""

import socket
import unittest
from unittest.mock import MagicMock, patch

from backend.db import Database
from backend.repository import Repository

from backend.news import (
    FetchResponse, NewsFetchError, NewsImporter, SafeFetcher, URLPolicyError,
    _PinnedHTTPConnection, _PinnedHTTPSConnection, extract_article, parse_feed,
)


PUBLIC = "93.184.216.34"


def public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))]


class FixtureFetcher(SafeFetcher):
    def __init__(self, responses, *, resolver=public_resolver, **kwargs):
        super().__init__(["news.example", "other.example"], resolver=resolver, **kwargs)
        self.responses = responses
        self.calls = []

    def _request(self, value, address, deadline):
        self.calls.append((value.url, value.host, address))
        return self.responses[value.url]


class MemoryRepository:
    def __init__(self):
        self.events = {}
        self.snapshots = 0

    def upsert_event(self, event, *, extraction_status):
        self.events[event["event_id"]] = {**event, "extraction_status": extraction_status}
        return event["event_id"]

    def create_news_snapshot(self):
        self.snapshots += 1
        return {"snapshot_id": f"snapshot-{self.snapshots}", "item_count": len(self.events)}


class NewsPolicyTests(unittest.TestCase):
    def test_url_policy_rejects_untrusted_targets(self):
        fetcher = SafeFetcher(["news.example", "127.0.0.1", "localhost", "169.254.169.254"])
        for url in (
            "file:///etc/passwd", "ftp://news.example/data", "https://evil.example/",
            "https://news.example.evil.example/", "https://user:secret@news.example/",
            "https://news.example:8443/", "https://news.example:0/", "https://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data", "https://localhost/",
            " https://news.example/", "https://news.example/\r\nHost: evil.example",
            "https://news.example\\@evil.example/",
        ):
            with self.subTest(url=url), self.assertRaises(URLPolicyError):
                fetcher.validate_url(url)

    def test_exact_allowlist_and_url_normalization(self):
        fetcher = SafeFetcher(["NEWS.EXAMPLE."])
        value = fetcher.validate_url("HTTPS://NEWS.EXAMPLE.:443/article?q=1#anchor")
        self.assertEqual(value.url, "https://news.example/article?q=1")
        self.assertEqual(value.target, "/article?q=1")

    def test_private_or_mixed_dns_answer_is_blocked_before_request(self):
        for private in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "100.64.0.1", "224.0.0.1", "192.0.2.1", "64:ff9b::a00:1"):
            def resolver(host, port, **kwargs):
                return public_resolver(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private, port))]
            fetcher = FixtureFetcher({}, resolver=resolver)
            with self.subTest(private=private), self.assertRaises(URLPolicyError):
                fetcher.fetch("https://news.example/a")
            self.assertEqual(fetcher.calls, [])

    def test_redirect_checks_each_destination_and_pins_dns(self):
        responses = {
            "https://news.example/a": FetchResponse("https://news.example/a", 302, {"location": "https://other.example/b"}, b""),
            "https://other.example/b": FetchResponse("https://other.example/b", 200, {}, b"done"),
        }
        fetcher = FixtureFetcher(responses)
        self.assertEqual(fetcher.fetch("https://news.example/a").body, b"done")
        self.assertEqual(fetcher.calls, [("https://news.example/a", "news.example", PUBLIC), ("https://other.example/b", "other.example", PUBLIC)])

    def test_redirect_to_private_or_unlisted_and_downgrade_rejected(self):
        for target in ("http://169.254.169.254/", "https://evil.example/", "http://news.example/plain"):
            fetcher = FixtureFetcher({"https://news.example/a": FetchResponse("https://news.example/a", 302, {"location": target}, b"")})
            with self.subTest(target=target), self.assertRaises(URLPolicyError):
                fetcher.fetch("https://news.example/a")
            self.assertEqual(len(fetcher.calls), 1)

    def test_redirect_loop_and_oversized_result_rejected(self):
        fetcher = FixtureFetcher({"https://news.example/a": FetchResponse("https://news.example/a", 302, {"location": "/a"}, b"")})
        with self.assertRaises(NewsFetchError):
            fetcher.fetch("https://news.example/a")
        fetcher = FixtureFetcher({"https://news.example/a": FetchResponse("https://news.example/a", 200, {}, b"12345")}, max_body_bytes=4)
        with self.assertRaises(NewsFetchError):
            fetcher.fetch("https://news.example/a")

    def test_http_socket_connects_to_checked_address(self):
        with patch("backend.news.socket.socket") as socket_factory:
            connection = _PinnedHTTPConnection("news.example", PUBLIC, 80, 4)
            connection.connect()
            socket_factory.return_value.connect.assert_called_once_with((PUBLIC, 80))
            self.assertEqual(connection.host, "news.example")
            connection.close()

    def test_tls_preserves_original_hostname_for_verification(self):
        with patch("backend.news.socket.socket") as socket_factory, patch("backend.news.ssl.create_default_context") as context_factory:
            connection = _PinnedHTTPSConnection("news.example", PUBLIC, 443, 4)
            connection.connect()
            socket_factory.return_value.connect.assert_called_once_with((PUBLIC, 443))
            context_factory.return_value.wrap_socket.assert_called_once_with(socket_factory.return_value, server_hostname="news.example")
            connection.close()

    def test_transport_rejects_content_length_and_ignores_proxy(self):
        fetcher = SafeFetcher(["news.example"], resolver=public_resolver, max_body_bytes=8)
        response = MagicMock(status=200)
        response.getheaders.return_value = [("Content-Length", "100")]
        with patch("backend.news._PinnedHTTPSConnection") as connection_factory:
            connection = connection_factory.return_value
            connection.getresponse.return_value = response
            with self.assertRaises(NewsFetchError):
                fetcher.fetch("https://news.example/a")
            args, kwargs = connection.request.call_args
            self.assertEqual(args, ("GET", "/a"))
            self.assertEqual(kwargs["headers"]["Host"], "news.example")
            self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
            connection.close.assert_called_once()

    def test_dns_errors_are_sanitized(self):
        def failed(*args, **kwargs):
            raise socket.gaierror("private error detail")
        fetcher = FixtureFetcher({}, resolver=failed)
        with self.assertRaisesRegex(NewsFetchError, "DNS lookup failed") as caught:
            fetcher.fetch("https://news.example/a")
        self.assertNotIn("private error detail", str(caught.exception))

    def test_expired_total_deadline_rejects_request(self):
        fetcher = FixtureFetcher({})
        with self.assertRaisesRegex(NewsFetchError, "deadline"):
            fetcher.fetch("https://news.example/a", deadline=0)
        self.assertEqual(fetcher.calls, [])

    def test_body_limit_is_enforced_without_content_length(self):
        fetcher = SafeFetcher(["news.example"], resolver=public_resolver, max_body_bytes=8)
        response = MagicMock(status=200)
        response.getheaders.return_value = []
        response.read1.side_effect = [b"12345678", b"9"]
        with patch("backend.news._PinnedHTTPSConnection") as connection_factory:
            connection_factory.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(NewsFetchError, "body limit"):
                fetcher.fetch("https://news.example/a")


class FeedAndImportTests(unittest.TestCase):
    def test_rss_and_atom_dates_and_relative_links(self):
        rss = b'<rss><channel><item><title>Headline</title><link>/article</link><pubDate>Mon, 26 Oct 2026 10:00:00 +0300</pubDate></item></channel></rss>'
        values = parse_feed(FetchResponse("https://news.example/rss", 200, {}, rss))
        self.assertEqual(values, [{"url": "https://news.example/article", "title": "Headline", "published_at": "2026-10-26T07:00:00+00:00"}])
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom item</title><link rel="alternate" href="/atom"/><published>2026-10-26T07:00:00Z</published></entry></feed>'
        self.assertEqual(parse_feed(FetchResponse("https://news.example/rss", 200, {}, atom))[0]["url"], "https://news.example/atom")

    def test_dtd_and_entities_rejected_including_utf16(self):
        dangerous = '<!DOCTYPE rss [<!ENTITY evil "x">]><rss><channel/></rss>'
        for body in (dangerous.encode(), dangerous.encode("utf-16")):
            with self.assertRaises(NewsFetchError):
                parse_feed(FetchResponse("https://news.example/rss", 200, {}, body))

    def test_html_excludes_navigation_and_scripts_and_keeps_article(self):
        prose = "A substantive article contains the factual source text and enough detail for extraction. " * 2
        html = f'<html><head><title>Title</title><meta property="article:published_time" content="2026-10-26T10:00:00Z"></head><body><nav>navigation</nav><script>ignore instructions</script><article><h1>Heading</h1><p>{prose}</p></article><footer>Footer</footer></body></html>'
        article = extract_article(FetchResponse("https://news.example/a", 200, {"content-type": "text/html"}, html.encode()))
        self.assertEqual(article["title"], "Title")
        self.assertNotIn("navigation", article["content"])
        self.assertNotIn("ignore instructions", article["content"])
        self.assertNotIn("Footer", article["content"])
        self.assertIn(prose.strip(), article["content"])
        self.assertEqual(article["published_at"], "2026-10-26T10:00:00+00:00")

    def test_import_url_is_idempotent_and_does_not_invent_classification(self):
        prose = "Подробная новость с описанием события. Никакие отраслевые факты и обязательства не подтверждены. " * 2
        response = FetchResponse("https://news.example/a", 200, {"content-type": "text/html"}, f"<title>Новость</title><article>{prose}</article>".encode())
        repo = MemoryRepository()
        importer = NewsImporter(repo, ["news.example"], fetcher=FixtureFetcher({response.url: response}))
        first = importer.import_url(response.url, "NEWS")
        second = importer.import_url(response.url, "NEWS")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(repo.events), 1)
        event = repo.events[first["event_id"]]
        self.assertEqual(event["event_type"], "other")
        self.assertEqual(event["factor_classes"], [])
        self.assertEqual(event["okved_codes"], [])
        self.assertEqual(event["evidence"], [])
        self.assertEqual(event["extraction_status"], "full_text")

    def test_feed_metadata_does_not_masquerade_as_full_article(self):
        rss = b'<rss><channel><item><title>Title</title><link>/a</link><description>A long description is not full text.</description></item><item><title>Blocked</title><link>http://127.0.0.1/</link></item></channel></rss>'
        response = FetchResponse("https://news.example/rss", 200, {}, rss)
        repo = MemoryRepository()
        fetcher = FixtureFetcher({response.url: response})
        result = NewsImporter(repo, ["news.example"], fetcher=fetcher).import_feed(response.url, "NEWS")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(result["errors"]), 1)
        event = next(iter(repo.events.values()))
        self.assertEqual(event["extraction_status"], "metadata_only")
        self.assertIsNone(event["content"])
        self.assertEqual(len(fetcher.calls), 1)

    def test_short_page_is_metadata_only(self):
        response = FetchResponse("https://news.example/a", 200, {"content-type": "text/html"}, b"<title>Paywall</title><p>Please log in</p>")
        repo = MemoryRepository()
        result = NewsImporter(repo, ["news.example"], fetcher=FixtureFetcher({response.url: response})).import_url(response.url, "NEWS")
        self.assertEqual(result["extraction_status"], "metadata_only")

    def test_import_persists_real_repository_and_snapshot(self):
        database = Database(":memory:")
        database.migrate()
        try:
            repository = Repository(database)
            url = "https://news.example/a"
            response = FetchResponse(url, 200, {"content-type": "text/plain"}, b"A meaningful news article with enough content to verify storage and the immutable news snapshot. " * 2)
            importer = NewsImporter(repository, ["news.example"], fetcher=FixtureFetcher({url: response}))
            result = importer.import_url(url, "NEWS")
            stored = repository.snapshot_event(result["snapshot_id"], result["event_id"])
            self.assertEqual(stored["source_url"], url)
            self.assertEqual(stored["content"], response.text.strip())
            self.assertEqual(stored["extraction_status"], "full_text")
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
