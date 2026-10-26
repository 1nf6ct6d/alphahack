"""Operational command line for migrations, imports, demo seed, and serving."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from typing import Sequence

from pydantic import ValidationError

from .api_models import BankReportIn, NormalizedEventIn
from .config import ConfigurationError, Settings
from .db import Database
from .imports import ImportTracker, import_profile_bundle
from .repository import Repository
from .seed import seed_demo
from .services import RULES_VERSION


ROOT = Path(__file__).resolve().parent.parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfa-context",
        description="Alfa Context backend operations",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite path (overrides ALFA_DATABASE_PATH)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="Apply database migrations")
    user = commands.add_parser("create-user", help="Issue a bearer session with explicit company grants")
    user.add_argument("--user-id", required=True)
    user.add_argument("--label")
    user.add_argument("--company-id", action="append", default=[])
    user.add_argument("--hours", type=int, default=24)
    user.add_argument("--token-env", help="Read the token from this environment variable")
    user.add_argument("--show-token", action="store_true", help="Print the issued token once")
    datasets = commands.add_parser("import-datasets", help="Stage all three official CSV files, preserving raw rows")
    datasets.add_argument("--data-dir", type=Path, required=True)
    datasets.add_argument("--encoding", default="auto")

    seed = commands.add_parser("seed-demo", help="Load synthetic demo data")
    seed.add_argument(
        "--show-tokens",
        action="store_true",
        help="Print synthetic demo tokens to stdout",
    )

    profile = commands.add_parser(
        "import-profile",
        help="Assemble and persist one profile from official CSV exports",
    )
    profile.add_argument("--company-id", required=True)
    profile.add_argument("--companies", required=True, type=Path)
    profile.add_argument("--payments", required=True, type=Path)
    profile.add_argument("--as-of-date")
    profile.add_argument("--display-name")
    profile.add_argument("--grant-user", action="append", default=[])
    profile.add_argument("--encoding", default="auto")
    profile.add_argument(
        "--rows-are-not-additive",
        action="store_true",
        help="Disable the explicit additive-payment-row assumption",
    )

    event = commands.add_parser(
        "import-event", help="Import one normalized news event JSON"
    )
    event.add_argument("json_file", type=Path)
    normalize = commands.add_parser("normalize-event", help="Normalize one extracted article using the configured LLM, with budget accounting")
    normalize.add_argument("event_id")
    url = commands.add_parser("import-url", help="Fetch and store a full news article from an allowed host")
    url.add_argument("url")
    url.add_argument("--source-id", required=True)
    feed = commands.add_parser("import-feed", help="Fetch an allowed RSS/Atom feed")
    feed.add_argument("url")
    feed.add_argument("--source-id", required=True)
    feed.add_argument("--max-items", type=int, default=25)
    feed.add_argument("--fetch-articles", action="store_true")

    bank = commands.add_parser(
        "import-bank-report",
        help="Import OCR fragments prepared by an external OCR pipeline",
    )
    bank.add_argument("json_file", type=Path)
    pdf = commands.add_parser("import-bank-pdf", help="Extract native text/OCR from a bank PDF and import fragments")
    pdf.add_argument("pdf_file", type=Path)
    pdf.add_argument("--source-id", required=True)
    pdf.add_argument("--title", required=True)
    pdf.add_argument("--report-date")
    pdf.add_argument("--language", default="rus+eng")
    pdf.add_argument("--verified", action="store_true", help="Operator confirms that the extracted pages were manually checked")
    pdf.add_argument("--review-json", type=Path, help="Write extracted fragments for manual review without importing")

    export = commands.add_parser(
        "export-openapi", help="Write the generated OpenAPI document"
    )
    export.add_argument("output", type=Path)

    serve = commands.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--log-level", default="info")
    return parser


def _settings(database_override: Path | None) -> Settings:
    if database_override is None:
        return Settings.from_env()
    values = dict(os.environ)
    values["ALFA_DATABASE_PATH"] = str(database_override)
    return Settings.from_env(values)


def _read_json(path: Path) -> object:
    if not path.is_file():
        raise ValueError(f"JSON file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON in {path}: line {error.lineno}, column {error.colno}"
        ) from error


def _database(settings: Settings) -> Database:
    database = Database(
        settings.database_path,
        timeout_seconds=settings.sqlite_timeout_seconds,
    )
    database.migrate()
    return database


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = _settings(args.database)

    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        import uvicorn

        from .main import create_app

        uvicorn.run(
            create_app(settings),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            proxy_headers=False,
            server_header=False,
        )
        return 0

    if args.command == "export-openapi":
        from .main import create_app

        application = create_app(settings, auto_migrate=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(application.openapi(), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(args.output)
        return 0

    database = _database(settings)
    try:
        repository = Repository(database)
        if args.command == "migrate":
            print(f"Database is current: {settings.database_path}")
            return 0

        if args.command == "create-user":
            from .security import issue_demo_session, grant_company_access
            if not 1 <= args.hours <= 720:
                raise ValueError("--hours must be between 1 and 720")
            token = os.environ.get(args.token_env, "") if args.token_env else None
            if args.token_env and not token:
                raise ValueError("Token environment variable is empty")
            if not args.token_env and not args.show_token:
                raise ValueError("Use --show-token or --token-env to receive a session")
            with database.transaction(immediate=True) as connection:
                for company_id in args.company_id:
                    if connection.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone() is None:
                        raise ValueError("Import the company profile before granting access")
                connection.execute("INSERT INTO users(id,label) VALUES (?, ?) ON CONFLICT(id) DO NOTHING", (args.user_id, args.label or args.user_id))
                session_id, raw_token = issue_demo_session(connection, user_id=args.user_id,
                    pepper=settings.auth_token_pepper, token=token,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=args.hours))
                for company_id in args.company_id:
                    grant_company_access(connection, user_id=args.user_id, company_id=company_id)
            output = {"user_id": args.user_id, "session_id": session_id, "expires_in_hours": args.hours}
            if args.show_token:
                output["token"] = raw_token
            print(json.dumps(output, ensure_ascii=False))
            return 0

        if args.command == "import-datasets":
            from ml_audit.csv_io import discover_datasets
            from .datasets import import_dataset
            files = discover_datasets(args.data_dir)
            absent = [kind for kind, path in files.items() if path is None]
            if absent:
                raise ValueError(f"Missing datasets: {absent}")
            for kind, path in files.items():
                result = import_dataset(database, kind, path, encoding=args.encoding)
                print(json.dumps({"kind": kind, **result}, ensure_ascii=False))
            return 0

        if args.command == "seed-demo":
            tokens = seed_demo(
                database,
                root=ROOT,
                pepper=settings.auth_token_pepper,
            )
            print("Synthetic demo data loaded for TEST_A and TEST_B.")
            if args.show_tokens:
                print(json.dumps(tokens, ensure_ascii=False, indent=2))
            return 0

        if args.command == "import-profile":
            for path in (args.companies, args.payments):
                if not path.is_file():
                    raise ValueError(f"CSV file does not exist: {path}")
            result = import_profile_bundle(
                repository,
                ImportTracker(database),
                args.company_id,
                args.companies,
                args.payments,
                as_of_date=args.as_of_date,
                assume_additive_rows=not args.rows_are_not_additive,
                encoding=args.encoding,
                rules_version=RULES_VERSION,
                display_name=args.display_name,
                source_record={
                    "companies_csv": str(args.companies.resolve()),
                    "payments_csv": str(args.payments.resolve()),
                },
            )
            for user_id in args.grant_user:
                repository.grant_access(user_id, args.company_id)
            print(
                json.dumps(
                    {
                        "company_id": args.company_id,
                        "profile_id": result.profile_id,
                        "import_id": result.import_id,
                        "reused": result.reused,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "import-event":
            payload = NormalizedEventIn.model_validate(
                _read_json(args.json_file)
            )
            event_id = repository.upsert_event(
                payload.ml_payload(),
                extraction_status=payload.extraction_status,
            )
            snapshot = repository.create_news_snapshot()
            print(json.dumps({"event_id": event_id, **snapshot}))
            return 0

        if args.command == "normalize-event":
            from .budget import BudgetManager
            from .llm_gateway import Gateway
            from .services import ApplicationServices
            service = ApplicationServices(repository, BudgetManager(database, internal_limit_kopecks=settings.internal_budget_limit_kopecks,
                timezone_name=settings.budget_timezone), gateway=Gateway.from_env())
            print(json.dumps(service.normalize_event(args.event_id), ensure_ascii=False))
            return 0

        if args.command in {"import-url", "import-feed"}:
            from .news import NewsImporter
            importer = NewsImporter(repository, settings.allowed_source_hosts)
            if args.command == "import-url":
                result = importer.import_url(args.url, args.source_id)
            else:
                result = importer.import_feed(args.url, args.source_id, max_items=args.max_items, fetch_articles=args.fetch_articles)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command == "import-bank-report":
            report = BankReportIn.model_validate(_read_json(args.json_file))
            report_id = repository.import_bank_report(report.model_dump())
            print(json.dumps({"report_id": report_id}))
            return 0

        if args.command == "import-bank-pdf":
            from .ocr import extract_bank_pdf
            report = extract_bank_pdf(args.pdf_file, source_id=args.source_id, title=args.title,
                                      report_date=args.report_date, language=args.language, verified=args.verified)
            if args.review_json:
                args.review_json.parent.mkdir(parents=True, exist_ok=True)
                args.review_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"Review file written: {args.review_json}")
            else:
                print(json.dumps({"report_id": repository.import_bank_report(report), "verified": args.verified}))
            return 0
    finally:
        database.close()
    raise RuntimeError(f"Unhandled command: {args.command}")


def main() -> None:
    try:
        raise SystemExit(run())
    except (ConfigurationError, ValidationError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()

