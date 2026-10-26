"""FastAPI transport for the Alfa Context backend.

The HTTP layer is deliberately thin: authentication and tenant isolation are
enforced before calling the application service, while persistence and domain
logic remain framework-independent and testable on their own.
"""

from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import sqlite3
from typing import Annotated, AsyncIterator
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api_models import (
    AnalyticFocus,
    BankReportIn,
    FeedbackRequest,
    InsightCreateRequest,
    NormalizedEventIn,
    OverviewRefreshRequest,
    QuestionRequest,
    RssImportRequest,
    UrlImportRequest,
)
from .budget import BudgetExceeded, BudgetManager
from .config import Settings
from .db import Database
from .repository import Repository
from .repository import OperationInProgress
from .llm_gateway import Gateway, GatewayError
from .middleware import BodyLimitMiddleware
from .news import NewsImporter, NewsFetchError
from .security import (
    AccessDenied,
    AuthContext,
    AuthenticationError,
    authenticate_bearer,
    require_company_access,
    require_resource_access,
)
from .seed import seed_demo
from .services import ApplicationServices, InvalidRequest, ServiceError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _parse_demo_tokens() -> dict[str, str] | None:
    """Read optional ``USER=token`` pairs without ever logging them."""

    raw = os.environ.get("ALFA_DEMO_TOKENS", "").strip()
    if not raw:
        return None
    result: dict[str, str] = {}
    for pair in raw.split(","):
        user, separator, token = pair.partition("=")
        if not separator or not user.strip() or not token.strip():
            raise ValueError("ALFA_DEMO_TOKENS must contain USER=token pairs")
        result[user.strip()] = token.strip()
    return result


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    auto_migrate: bool = True,
    seed_demo_data: bool | None = None,
    project_root: str | Path = PROJECT_ROOT,
) -> FastAPI:
    """Build an application with injectable settings/database for tests."""

    resolved = settings or Settings.from_env()
    db = database or Database(
        resolved.database_path,
        timeout_seconds=resolved.sqlite_timeout_seconds,
    )
    repository = Repository(db)
    budget = BudgetManager(
        db,
        internal_limit_kopecks=resolved.internal_budget_limit_kopecks,
        timezone_name=resolved.budget_timezone,
    )
    gateway = Gateway.from_env()
    services = ApplicationServices(repository, budget, gateway=gateway)
    should_seed = (
        os.environ.get("ALFA_DEMO_SEED", "0") == "1"
        if seed_demo_data is None
        else seed_demo_data
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if auto_migrate:
            db.migrate()
        if should_seed:
            seed_demo(
                db,
                root=project_root,
                pepper=resolved.auth_token_pepper,
                tokens=_parse_demo_tokens(),
            )
        yield
        if database is None:
            db.close()

    app = FastAPI(
        title="Альфа Контекст API",
        version="1.0.0",
        description=(
            "Backend MVP: профили компаний, снимки новостей, ранжирование, "
            "evidence-bound insights, вопросы и обратная связь."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.database = db
    app.state.repository = repository
    app.state.services = services
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)

    origins = [
        item.strip()
        for item in os.environ.get("ALFA_CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    @app.middleware("http")
    async def request_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied if supplied.isascii() and 0 < len(supplied) <= 64
            else uuid4().hex
        )
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > MAX_REQUEST_BYTES
            except ValueError:
                too_large = True
            if too_large:
                return _error(
                    request,
                    status_code=413,
                    code="request_too_large",
                    message="Request body is too large",
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthenticationError)
    async def auth_error(request: Request, error: AuthenticationError):
        return _error(
            request,
            status_code=401,
            code="unauthorized",
            message=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(AccessDenied)
    async def access_error(request: Request, _error_value: AccessDenied):
        # Deliberately indistinguishable from a missing resource.
        return _error(
            request,
            status_code=404,
            code="not_found",
            message="Resource is unavailable",
        )

    @app.exception_handler(BudgetExceeded)
    async def budget_error(request: Request, error: BudgetExceeded):
        return _error(
            request,
            status_code=429,
            code="daily_budget_exceeded",
            message=str(error),
            headers={"Retry-After": "3600"},
        )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        return _error(
            request,
            status_code=error.http_status,
            code=error.code,
            message=str(error),
            details=error.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return _error(
            request,
            status_code=400,
            code="validation_error",
            message="Request validation failed",
            details=[{"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                     for item in error.errors()],
        )

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError):
        return _error(request, status_code=503, code=error.code,
                      message="The model service did not return a valid response; retry later")

    @app.exception_handler(NewsFetchError)
    async def news_error(request: Request, error: NewsFetchError):
        return _error(request, status_code=503, code="news_source_unavailable", message=str(error))

    @app.exception_handler(OperationInProgress)
    async def busy_error(request: Request, error: OperationInProgress):
        return _error(request, status_code=409, code="operation_in_progress",
                      message=str(error), headers={"Retry-After": "3"})

    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request: Request, _error_value: sqlite3.OperationalError):
        return _error(request, status_code=503, code="storage_unavailable",
                      message="Storage temporarily unavailable")

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return _error(
            request,
            status_code=400,
            code="invalid_request",
            message=str(error),
        )

    bearer = HTTPBearer(auto_error=False)

    def current_user(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> AuthContext:
        with db.connection() as connection:
            return authenticate_bearer(
                connection,
                f"Bearer {credential.credentials}" if credential else None,
                pepper=resolved.auth_token_pepper,
            )

    def ensure_company(user: AuthContext, company_id: str) -> None:
        with db.connection() as connection:
            require_company_access(
                connection, user_id=user.user_id, company_id=company_id
            )

    def ensure_insight(user: AuthContext, insight_id: str) -> None:
        with db.connection() as connection:
            require_resource_access(
                connection,
                user_id=user.user_id,
                resource_kind="insight",
                resource_id=insight_id,
            )

    def require_admin(
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> None:
        expected = os.environ.get("ALFA_ADMIN_TOKEN", "")
        valid = bool(expected and x_admin_token) and hmac.compare_digest(
            expected, x_admin_token
        )
        if not valid:
            raise AuthenticationError("Invalid administrator credential")

    @app.get("/health/live", tags=["health"])
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> dict:
        try:
            with db.connection() as connection:
                connection.execute("SELECT 1").fetchone()
                migration = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
            return {"status": "ready", "migration": migration["version"]}
        except sqlite3.Error as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error

    @app.get("/meta/capabilities", tags=["meta"])
    def capabilities() -> dict:
        return {
            "profile_assembly": "implemented",
            "ranking": "implemented",
            "insight_provider": "llm_evidence_bound" if gateway else "deterministic_evidence_bound",
            "remote_llm_gateway": "configured" if gateway else "not_configured",
            "ocr_engine": "poppler_tesseract_cli_optional",
            "real_news_coverage": "depends_on_configured_sources",
            "production_readiness": False,
        }

    @app.get("/companies", tags=["companies"])
    def companies(user: Annotated[AuthContext, Depends(current_user)]) -> dict:
        return {"items": repository.list_companies(user.user_id)}

    @app.get("/companies/{company_id}", tags=["companies"])
    def company(
        company_id: str,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_company(user, company_id)
        return services.company_detail(company_id)

    @app.get("/companies/{company_id}/overview", tags=["overview"])
    def overview(
        company_id: str,
        analytic_focus: Annotated[AnalyticFocus, Query()] = AnalyticFocus.CFO,
        user: Annotated[AuthContext, Depends(current_user)] = None,
    ) -> dict:
        assert user is not None
        ensure_company(user, company_id)
        return services.get_overview(
            user.user_id, company_id, analytic_focus.value
        )

    @app.post("/companies/{company_id}/overview/refresh", tags=["overview"])
    def refresh_overview(
        company_id: str,
        body: OverviewRefreshRequest,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_company(user, company_id)
        if body.as_of is not None and resolved.environment != "test":
            raise InvalidRequest("as_of override is available only in test mode")
        return services.refresh_overview(
            user.user_id,
            company_id,
            body.analytic_focus.value,
            as_of=body.as_of,
        )

    @app.post("/companies/{company_id}/insights", tags=["insights"])
    def create_insight(
        company_id: str,
        body: InsightCreateRequest,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_company(user, company_id)
        return services.create_insight(
            user.user_id,
            company_id,
            body.analytic_focus.value,
            body.event_id,
        )

    @app.get("/insights/{insight_id}", tags=["insights"])
    def get_insight(
        insight_id: str,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_insight(user, insight_id)
        return services.insight(insight_id)

    @app.post("/insights/{insight_id}/questions", tags=["insights"])
    def ask_question(
        insight_id: str,
        body: QuestionRequest,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_insight(user, insight_id)
        return services.ask(
            user_id=user.user_id,
            insight_id=insight_id,
            question=body.question,
            conversation_id=body.conversation_id,
        )

    @app.put("/insights/{insight_id}/feedback", tags=["feedback"])
    def put_feedback(
        insight_id: str,
        body: FeedbackRequest,
        user: Annotated[AuthContext, Depends(current_user)],
    ) -> dict:
        ensure_insight(user, insight_id)
        return services.feedback(
            user_id=user.user_id,
            insight_id=insight_id,
            rating=body.rating,
            reason=body.reason,
            comment=body.comment,
        )

    @app.post("/admin/events", tags=["admin"])
    def import_event(
        body: NormalizedEventIn,
        _admin: Annotated[None, Depends(require_admin)],
    ) -> dict:
        payload = body.ml_payload()
        event_id = repository.upsert_event(
            payload, extraction_status=body.extraction_status
        )
        snapshot = repository.create_news_snapshot()
        return {"event_id": event_id, **snapshot}

    @app.get("/admin/events/{event_id}", tags=["admin"])
    def inspect_event(event_id: str, _admin: Annotated[None, Depends(require_admin)]) -> dict:
        value = repository.event(event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Event unavailable")
        return value

    @app.post("/admin/events/{event_id}/normalize", tags=["admin"])
    def normalize_event(event_id: str, _admin: Annotated[None, Depends(require_admin)]) -> dict:
        return services.normalize_event(event_id)

    @app.post("/admin/news/url", tags=["admin"])
    def import_url(body: UrlImportRequest, _admin: Annotated[None, Depends(require_admin)]) -> dict:
        return NewsImporter(repository, resolved.allowed_source_hosts).import_url(body.url, body.source_id)

    @app.post("/admin/news/feed", tags=["admin"])
    def import_feed(body: RssImportRequest, _admin: Annotated[None, Depends(require_admin)]) -> dict:
        return NewsImporter(repository, resolved.allowed_source_hosts).import_feed(
            body.url, body.source_id, max_items=body.max_items, fetch_articles=body.fetch_articles)

    @app.post("/admin/bank-reports", tags=["admin"])
    def import_bank_report(
        body: BankReportIn,
        _admin: Annotated[None, Depends(require_admin)],
    ) -> dict:
        return {"report_id": repository.import_bank_report(body.model_dump())}

    @app.get("/admin/budget", tags=["admin"])
    def budget_status(
        _admin: Annotated[None, Depends(require_admin)],
    ) -> dict:
        snapshot = budget.snapshot()
        return {
            "budget_date": snapshot.budget_date,
            "confirmed_kopecks": snapshot.confirmed_kopecks,
            "reserved_kopecks": snapshot.reserved_kopecks,
            "available_kopecks": snapshot.available_kopecks,
            "internal_limit_kopecks": snapshot.internal_limit_kopecks,
            "external_limit_kopecks": resolved.daily_budget_limit_kopecks,
        }

    return app


app = create_app()


__all__ = ["app", "create_app"]
