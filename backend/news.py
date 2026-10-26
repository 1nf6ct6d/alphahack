from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
from time import monotonic
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .api_models import NormalizedEventIn


MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class URLPolicyError(ValueError):


class NewsFetchError(RuntimeError):

    http_status = 503
    code = "news_source_unavailable"


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return _decode(self.body, self.headers.get("content-type", ""))


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    target: str


def _canonical_host(value: str) -> str:
    candidate = value.lower().rstrip(".")
    if not candidate or any(character in candidate for character in ("/", "\\", "%", "@")):
        raise URLPolicyError("Invalid source host")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise URLPolicyError("Invalid source host") from error


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise URLPolicyError("DNS returned an invalid address") from error
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise URLPolicyError("Source resolves to a non-public address")
    if isinstance(address, ipaddress.IPv6Address):
        if address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
            raise URLPolicyError("Translated IPv6 destinations are not permitted")
        if address.ipv4_mapped is not None:
            _public_address(str(address.ipv4_mapped))
        if address.sixtofour is not None or address.teredo is not None:
            raise URLPolicyError("Transition IPv6 addresses are not permitted")
    return str(address)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise NewsFetchError("News fetch deadline exceeded")
    return remaining


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect((self.address, self.port))
        except BaseException:
            connection.close()
            raise
        self.sock = connection


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        try:
            self.sock = ssl.create_default_context().wrap_socket(
                self.sock, server_hostname=self.host
            )
        except BaseException:
            self.sock.close()
            raise


class SafeFetcher:

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        timeout_seconds: float = 20,
        max_body_bytes: int = MAX_BODY_BYTES,
        max_redirects: int = 3,
        resolver: Callable | None = None,
    ):
        self.allowed_hosts = frozenset(_canonical_host(host) for host in allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("At least one allowed source host is required")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not 1 <= max_body_bytes <= MAX_BODY_BYTES:
            raise ValueError("max_body_bytes must be between 1 and 2 MiB")
        if not 0 <= max_redirects <= 5:
            raise ValueError("max_redirects must be between 0 and 5")
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver or socket.getaddrinfo

    def validate_url(self, url: str) -> ValidatedURL:
        if not isinstance(url, str) or not url or len(url) > 2000:
            raise URLPolicyError("Source URL must contain 1 to 2000 characters")
        if any(ord(character) <= 32 or ord(character) == 127 for character in url) or "\\" in url:
            raise URLPolicyError("Source URL contains forbidden characters")
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"}:
                raise URLPolicyError("Only HTTP and HTTPS sources are supported")
            if parsed.username is not None or parsed.password is not None:
                raise URLPolicyError("URL credentials are forbidden")
            if parsed.hostname is None:
                raise URLPolicyError("Source URL must contain a host")
            host = _canonical_host(parsed.hostname)
            port = parsed.port
            if port is None:
                port = 443 if parsed.scheme.lower() == "https" else 80
        except (ValueError, UnicodeError) as error:
            if isinstance(error, URLPolicyError):
                raise
            raise URLPolicyError("Malformed source URL") from error
        if port != (443 if parsed.scheme.lower() == "https" else 80):
            raise URLPolicyError("Nonstandard source ports are forbidden")
        if host not in self.allowed_hosts:
            raise URLPolicyError("Source host is not in the allowlist")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            _public_address(host)
        if host == "localhost" or host.endswith(".localhost"):
            raise URLPolicyError("Local hosts are forbidden")
        authority = f"[{host}]" if ":" in host else host
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        normalized = urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))
        return ValidatedURL(normalized, parsed.scheme.lower(), host, port, target)

    def _resolve(self, value: ValidatedURL, deadline: float) -> str:
        # OS resolver calls have no portable cancellation API. A daemon keeps
        # a stuck resolver from blocking this request past its total deadline.
        answers: queue.Queue = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                answers.put((True, self.resolver(value.host, value.port, type=socket.SOCK_STREAM)))
            except Exception as error:
                answers.put((False, error))

        threading.Thread(target=resolve, name="news-dns", daemon=True).start()
        try:
            success, records = answers.get(timeout=_remaining(deadline))
        except queue.Empty as error:
            raise NewsFetchError("News DNS lookup timed out") from error
        if not success:
            raise NewsFetchError("News DNS lookup failed")
        addresses = sorted({_public_address(record[4][0]) for record in records})
        if not addresses:
            raise NewsFetchError("News source has no usable addresses")
        return addresses[0]

    def _request(self, value: ValidatedURL, address: str, deadline: float) -> FetchResponse:
        connection_type = _PinnedHTTPSConnection if value.scheme == "https" else _PinnedHTTPConnection
        connection = connection_type(value.host, address, value.port, _remaining(deadline))
        try:
            connection.request("GET", value.target, headers={
                "Host": f"[{value.host}]" if ":" in value.host else value.host,
                "User-Agent": "AlfaContextNews/1.0",
                "Accept": "text/html, application/xhtml+xml, application/rss+xml, application/atom+xml, application/xml, text/plain;q=0.8",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            assert connection.sock is not None
            stream_socket = connection.sock
            stream_socket.settimeout(_remaining(deadline))
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            if response.status in REDIRECT_STATUSES:
                return FetchResponse(value.url, response.status, headers, b"")
            if response.status != 200:
                raise NewsFetchError("News source returned a non-success status")
            encoding = headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise NewsFetchError("Compressed news responses are not accepted")
            if "content-length" in headers:
                try:
                    length = int(headers["content-length"])
                except ValueError as error:
                    raise NewsFetchError("Invalid source Content-Length") from error
                if length < 0 or length > self.max_body_bytes:
                    raise NewsFetchError("News response exceeds the body limit")
            chunks: list[bytes] = []
            received = 0
            while True:
                stream_socket.settimeout(_remaining(deadline))
                chunk = response.read1(min(64 * 1024, self.max_body_bytes + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > self.max_body_bytes:
                    raise NewsFetchError("News response exceeds the body limit")
                chunks.append(chunk)
            _remaining(deadline)
            return FetchResponse(value.url, response.status, headers, b"".join(chunks))
        except NewsFetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise NewsFetchError("News source request failed") from error
        finally:
            connection.close()

    def fetch(self, url: str, *, deadline: float | None = None) -> FetchResponse:
        request_deadline = monotonic() + self.timeout_seconds
        deadline = request_deadline if deadline is None else min(deadline, request_deadline)
        current = self.validate_url(url)
        visited: set[str] = set()
        for hop in range(self.max_redirects + 1):
            if current.url in visited:
                raise NewsFetchError("News redirect loop detected")
            visited.add(current.url)
            address = self._resolve(current, deadline)
            response = self._request(current, address, deadline)
            if response.status not in REDIRECT_STATUSES:
                if response.status != 200 or len(response.body) > self.max_body_bytes:
                    raise NewsFetchError("Invalid or oversized news response")
                _remaining(deadline)
                return response
            if hop >= self.max_redirects:
                raise NewsFetchError("News redirect limit exceeded")
            location = response.headers.get("location")
            if not location:
                raise NewsFetchError("News redirect has no location")
            target = self.validate_url(urljoin(current.url, location))
            if current.scheme == "https" and target.scheme != "https":
                raise URLPolicyError("HTTPS downgrade redirects are forbidden")
            current = target
        raise NewsFetchError("News fetch did not complete")


def _decode(body: bytes, content_type: str) -> str:
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", errors="replace")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    if charset is None:
        declaration = re.search(br"encoding\s*=\s*[\"']([\w-]+)[\"']", body[:200], re.I)
        encoding = declaration.group(1).decode("ascii") if declaration else "utf-8-sig"
    else:
        encoding = charset.group(1)
    # No arbitrary codecs (e.g. unicode_escape) selected by an untrusted server.
    encoding = encoding.lower().replace("_", "-")
    if encoding not in {"utf-8", "utf-8-sig", "windows-1251", "cp1251", "iso-8859-1", "windows-1252", "ascii"}:
        encoding = "utf-8-sig"
    return body.decode(encoding, errors="replace")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _ArticleParser(HTMLParser):
    ignored = {"script", "style", "nav", "header", "footer", "form", "noscript", "svg", "template", "iframe"}
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.title: list[str] = []
        self.heading: list[str] = []
        self.body: list[str] = []
        self.article: list[str] = []
        self.published_at: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property", "").lower() in {"article:published_time", "datepublished"}:
            self.published_at = _timestamp(attributes.get("content"))
        if tag not in self.void_tags:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def handle_data(self, data: str) -> None:
        if any(tag in self.ignored for tag in self.stack):
            return
        if "title" in self.stack:
            self.title.append(data)
            return
        if "head" in self.stack:
            return
        if "h1" in self.stack:
            self.heading.append(data)
        self.body.append(data)
        if "article" in self.stack or "main" in self.stack:
            self.article.append(data)


def extract_article(response: FetchResponse) -> dict:
    content_type = response.headers.get("content-type", "text/html").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise NewsFetchError("Source is not an HTML or text article")
    text = response.text
    if content_type == "text/plain":
        content = _clean(text)[:MAX_TEXT_CHARS]
        return {"title": content[:120] or response.url, "content": content, "published_at": None}
    parser = _ArticleParser()
    parser.feed(text)
    article = _clean(" ".join(parser.article))
    content = article if len(article) >= 80 else _clean(" ".join(parser.body))
    title = _clean(" ".join(parser.title)) or _clean(" ".join(parser.heading)) or response.url
    return {"title": title[:500], "content": content[:MAX_TEXT_CHARS], "published_at": parser.published_at}


def _timestamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(response: FetchResponse, *, max_items: int = 25) -> list[dict]:
    if type(max_items) is not int or not 1 <= max_items <= 100:
        raise ValueError("max_items must be between 1 and 100")
    text = response.text
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.I):
        raise NewsFetchError("DTD and entity declarations are forbidden in feeds")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise NewsFetchError("Source returned malformed RSS/Atom XML") from error
    if _local_name(root.tag) not in {"rss", "feed", "rdf"}:
        raise NewsFetchError("Source is not an RSS/Atom feed")
    records: list[dict] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        title = ""
        link = None
        date = None
        for field in node:
            tag = _local_name(field.tag)
            if tag == "title":
                title = _clean("".join(field.itertext()))
            elif tag == "link":
                relation = field.attrib.get("rel", "alternate")
                if relation == "alternate":
                    candidate = field.attrib.get("href") or field.text
                    if candidate:
                        link = candidate.strip()
            elif tag in {"pubdate", "published", "date", "updated"}:
                parsed = _timestamp(field.text)
                if parsed and (date is None or tag == "published"):
                    date = parsed
        if link and title:
            records.append({"url": urljoin(response.url, link), "title": title[:500], "published_at": date})
            if len(records) >= max_items:
                break
    return records


class NewsImporter:
    def __init__(self, repository, allowed_hosts: Iterable[str], *, fetcher: SafeFetcher | None = None):
        self.repository = repository
        self.fetcher = fetcher or SafeFetcher(allowed_hosts)

    def _store(self, *, url: str, source_id: str, title: str, content: str | None, published_at: str | None) -> dict:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
            raise ValueError("source_id must contain 1 to 200 characters")
        full_text = bool(content and len(content) >= 80)
        event_id = "NEWS-" + hashlib.sha256(f"{source_id}\n{url}".encode("utf-8")).hexdigest()[:24]
        value = NormalizedEventIn(
            event_id=event_id, title=title, source_id=source_id,
            source_url=url, published_at=published_at, event_type="other",
            content=content if full_text else None,
            extraction_status="full_text" if full_text else "metadata_only",
            evidence=[], factor_classes=[], okved_codes=[],
        )
        self.repository.upsert_event(value.ml_payload(), extraction_status=value.extraction_status)
        return {"event_id": event_id, "extraction_status": value.extraction_status,
                "normalization_status": "needs_review"}

    def import_url(self, url: str, source_id: str) -> dict:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
            raise ValueError("source_id must contain 1 to 200 characters")
        response = self.fetcher.fetch(url)
        article = extract_article(response)
        result = self._store(url=response.url, source_id=source_id, **article)
        return {**result, **self.repository.create_news_snapshot()}

    def import_feed(self, url: str, source_id: str, *, max_items: int = 25, fetch_articles: bool = False) -> dict:
        if type(fetch_articles) is not bool:
            raise ValueError("fetch_articles must be a boolean")
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise ValueError("max_items must be between 1 and 100")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
            raise ValueError("source_id must contain 1 to 200 characters")
        batch_deadline = monotonic() + 60
        response = self.fetcher.fetch(url, deadline=batch_deadline)
        items = parse_feed(response, max_items=max_items)
        results: list[dict] = []
        errors: list[dict] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            if monotonic() >= batch_deadline:
                errors.append({"item_index": index, "code": "batch_deadline", "message": "Feed import deadline reached; remaining items were not fetched"})
                break
            try:
                normalized = self.fetcher.validate_url(item["url"]).url
                if normalized in seen:
                    continue
                seen.add(normalized)
                if fetch_articles:
                    article_response = self.fetcher.fetch(normalized, deadline=batch_deadline)
                    article = extract_article(article_response)
                    article["published_at"] = article["published_at"] or item["published_at"]
                    results.append(self._store(url=article_response.url, source_id=source_id, **article))
                else:
                    results.append(self._store(url=normalized, source_id=source_id,
                                               title=item["title"], published_at=item["published_at"], content=None))
            except (URLPolicyError, NewsFetchError) as error:
                errors.append({"item_index": index, "code": type(error).__name__, "message": str(error)})
        return {"items": results, "errors": errors, "imported": len(results),
                **self.repository.create_news_snapshot()}


__all__ = ["FetchResponse", "NewsFetchError", "NewsImporter", "SafeFetcher",
           "URLPolicyError", "extract_article", "parse_feed"]
