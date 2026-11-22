# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from webstudy import USERS, access
from webstudy.auth import SESSION_TTL, Auth, Session
from webstudy.catalog import (
    MAX_AMBIGUOUS_ROWS,
    Catalog,
    InvalidImport,
    PreviewNotFound,
    StalePreview,
)
from webstudy.service import StaleTurn, StudyCard, StudyService

COOKIE = "webstudy_session"
MAX_UPLOAD = 2 * 1024 * 1024
RATING_LABELS = {"again": "重来", "hard": "困难", "good": "良好", "easy": "简单"}
ENTRY_LINKS = "、".join(f"/{user}" for user in USERS)
FormText = Annotated[str, Form()]
T = TypeVar("T")
log = logging.getLogger(__name__)


def _json(request: Request) -> bool:
    return request.headers.get("accept") == "application/json"


def _cookie(request: Request, response: Response, token: str) -> Response:
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response


def _study_operation(user: str, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except StaleTurn as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        log.exception("Study collection unavailable for user %s", user)
        raise HTTPException(
            503, f"账号 {user.upper()} 的牌组暂不可用，请稍后重试。"
        ) from exc


def create_app(data_dir: Path) -> FastAPI:
    auth = Auth(data_dir / "auth.sqlite3")
    catalog = Catalog(data_dir / "catalog.sqlite3")
    templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        study = StudyService(data_dir, catalog)
        app.state.study = study
        try:
            await run_in_threadpool(study.catch_up_all)
            yield
        finally:
            await run_in_threadpool(study.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.access = access.load_access_config(data_dir / "access.json")

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def page(request: Request, name: str, status: int = 200, **context) -> Response:
        return templates.TemplateResponse(
            request=request, name=f"{name}.html", context=context, status_code=status
        )

    @app.exception_handler(HTTPException)
    async def failure(request: Request, exc: HTTPException):
        if _json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return page(request, "error", exc.status_code, message=exc.detail)

    def session_for(request: Request) -> Session | None:
        return auth.session(request.cookies.get(COOKIE))

    def _trusted_entry(request: Request) -> bool:
        client = request.client
        # Commands issued on this machine (the import CLI) are trusted as is.
        if client is not None and client.host in ("127.0.0.1", "::1"):
            return True
        # Everything else must arrive through the tunnel with a Cloudflare
        # Access assertion. Proxy headers are only honored from loopback, so
        # an https scheme proves the request came through cloudflared.
        config: access.AccessConfig | None = request.app.state.access
        if config is None or request.url.scheme != "https":
            return False
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            return False
        if not access.valid_assertion(config, token):
            log.warning("Rejected a Cloudflare Access assertion.")
            return False
        return True

    def access_entry(request: Request, user: str) -> Response:
        if not _trusted_entry(request):
            raise HTTPException(401, f"请通过入口链接进入：{ENTRY_LINKS}。")
        token, session = auth.access_session(user)
        response = (
            JSONResponse({"user": user, "csrf": session.csrf})
            if _json(request)
            else RedirectResponse("/study", status_code=303)
        )
        return _cookie(request, response, token)

    def protected(request: Request, csrf: str | None = None) -> tuple[Session, str]:
        session = session_for(request)
        if session is None:
            raise HTTPException(401, f"请通过入口链接进入：{ENTRY_LINKS}。")
        if csrf is not None and not session.accepts_csrf(csrf):
            raise HTTPException(403, "表单已失效，请刷新后重试。")
        return session, session.user

    @app.get("/")
    def index():
        return RedirectResponse("/study", status_code=303)

    def study_page(
        request: Request, session: Session, card: StudyCard | None
    ) -> Response:
        study: StudyService = request.app.state.study
        return page(
            request,
            "study",
            card=card,
            session=session,
            ratings=RATING_LABELS,
            pending=study.pending_delivery(),
        )

    @app.get("/study")
    def study(request: Request):
        session = session_for(request)
        if session is None:
            raise HTTPException(401, f"请通过入口链接进入：{ENTRY_LINKS}。")
        service: StudyService = request.app.state.study
        card = _study_operation(session.user, lambda: service.next_card(session.user))
        return study_page(request, session, card)

    @app.post("/study/reveal")
    def reveal(request: Request, csrf: FormText, token: FormText):
        session, user = protected(request, csrf)
        service: StudyService = request.app.state.study
        card = _study_operation(user, lambda: service.reveal(user, token))
        return study_page(request, session, card)

    @app.post("/study/answer")
    def answer(request: Request, csrf: FormText, token: FormText, rating: FormText):
        _, user = protected(request, csrf)
        if rating not in RATING_LABELS:
            raise HTTPException(400, "请选择有效的评分。")
        service: StudyService = request.app.state.study
        _study_operation(user, lambda: service.answer(user, token, rating))
        return RedirectResponse("/study", status_code=303)

    @app.post("/imports/preview")
    def preview_import(request: Request, csrf: FormText, file: UploadFile):
        session, _ = protected(request, csrf)
        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "文件不能超过 2 MiB。")
        format = Path(file.filename or "").suffix.lower().lstrip(".")
        try:
            preview = catalog.preview(data, format, owner=session.id)
        except InvalidImport as exc:
            raise HTTPException(400, str(exc)) from exc
        if _json(request):
            return JSONResponse(
                {**asdict(preview), "added": preview.added, "updated": preview.updated}
            )
        return page(request, "preview", session=session, preview=preview)

    def apply_import(
        request: Request, csrf: str, preview_id: str, resolutions: dict[int, str]
    ):
        session, _ = protected(request, csrf)
        try:
            version = catalog.confirm(preview_id, resolutions, owner=session.id)
        except InvalidImport as exc:
            status = (
                404
                if isinstance(exc, PreviewNotFound)
                else 409
                if isinstance(exc, StalePreview)
                else 400
            )
            raise HTTPException(status, str(exc)) from exc
        service: StudyService = request.app.state.study
        failures = service.catch_up_all()
        pending = sorted(set(failures) | set(service.pending_delivery()))
        if _json(request):
            return JSONResponse({"version": version, "pending": pending})
        return page(request, "confirmed", version=version, pending=pending)

    @app.post("/imports/confirm")
    async def confirm_import(request: Request):
        resolutions = {}
        # Keep the parser limit in sync with the largest preview we can produce,
        # allowing two extra fields for CSRF and the preview ID.
        async with request.form(max_fields=MAX_AMBIGUOUS_ROWS + 2, max_files=0) as form:
            csrf, preview_id = form.get("csrf"), form.get("preview_id")
            if (
                not isinstance(csrf, str)
                or not isinstance(preview_id, str)
                or len(form.getlist("csrf")) != 1
                or len(form.getlist("preview_id")) != 1
            ):
                raise HTTPException(400, "确认表单无效，请重新预览。")
            for key, choice in form.multi_items():
                if key.startswith("choice_"):
                    number = key.removeprefix("choice_")
                    if (
                        len(number) > 9
                        or not number.isascii()
                        or not number.isdecimal()
                        or not isinstance(choice, str)
                        or int(number) in resolutions
                    ):
                        raise HTTPException(400, "词义选择无效。")
                    resolutions[int(number)] = choice
        return await run_in_threadpool(
            apply_import, request, csrf, preview_id, resolutions
        )

    @app.get("/vocabulary.csv")
    def vocabulary(request: Request):
        protected(request)
        return Response(
            catalog.export_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="vocabulary.csv"'},
        )

    # One entry link per account, e.g. https://anki.aizhi.app/xct.
    for user in USERS:

        def entry_route(request: Request, user: str = user) -> Response:
            return access_entry(request, user)

        app.add_api_route(
            f"/{user}", entry_route, methods=["GET"], name=f"access-entry-{user}"
        )

    return app
