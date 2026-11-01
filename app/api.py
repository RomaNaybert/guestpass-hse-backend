import base64
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings
from app.qr import read_qr
from app.security import InvalidToken, issue_session, telegram_user, verify_session
from app.service import GuestService


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    service = GuestService(settings)

    @asynccontextmanager
    async def lifespan(app):
        service.open()
        yield
        service.pool.close()

    app = FastAPI(title="GuestPass API", version="1.0.0", lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method == "POST":
            if request.headers.get("X-Guestpass-Request") != "1":
                return JSONResponse({"detail": "Недопустимый запрос"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != f"{urlsplit(settings.public_url).scheme}://{urlsplit(settings.public_url).netloc}":
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
        )
        if request.url.path.startswith(("/api/", "/internal/")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/" or request.url.path.endswith("index.html"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(InvalidToken)
    def invalid_token(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    def not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(PermissionError)
    def forbidden(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    def organizer_id(actor_id: int):
        service.context_for(actor_id)
        return actor_id

    def bot_auth(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + settings.bot_api_key):
            raise HTTPException(401, "Требуется авторизация сервиса")

    def web_auth(request: Request):
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else request.cookies.get("guestpass_session", "")
        return organizer_id(verify_session(token, settings.signing_key))

    @app.get("/health")
    def health():
        with service.pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta():
        return {"event_name": settings.event_name, "bot_username": settings.bot_username, "check_in_points": settings.check_in_points}

    @app.post("/api/session")
    def session(body: SessionInput, response: Response):
        actor = organizer_id(telegram_user(body.init_data, settings.bot_token))
        session_token = issue_session(actor, settings.signing_key)
        response.set_cookie(
            "guestpass_session", session_token, httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=3600
        )
        return {**service.profile_for(actor), "session_token": session_token}

    @app.get("/api/me")
    def me(actor: int = Depends(web_auth)):
        return service.profile_for(actor)

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("guestpass_session")
        return {"ok": True}

    @app.get("/api/stats")
    def stats(actor: int = Depends(web_auth)):
        return service.context_for(actor).stats()

    @app.post("/api/scan")
    def scan(body: ScanInput, actor: int = Depends(web_auth)):
        return service.scan_for(actor, body.token)

    @app.post("/api/scan-photo")
    def scan_photo(body: PhotoInput, actor: int = Depends(web_auth)):
        try:
            data = base64.b64decode(body.image_base64, validate=True)
        except ValueError as exc:
            raise InvalidToken("Не удалось прочитать файл") from exc
        return service.scan_for(actor, read_qr(data))

    @app.post("/api/guests/{guest_id}/check-in")
    def check_in(guest_id: UUID, actor: int = Depends(web_auth)):
        return service.check_in_for(guest_id, actor)

    @app.post("/internal/register", dependencies=[Depends(bot_auth)])
    def register(body: RegisterInput):
        return service.register(body.telegram_id, body.name)

    @app.get("/internal/registration/{telegram_id}", dependencies=[Depends(bot_auth)])
    def registration(telegram_id: int):
        return service.registration_for(telegram_id)

    @app.get("/internal/pass/{telegram_id}", dependencies=[Depends(bot_auth)])
    def guest_pass(telegram_id: int):
        return {"token": service.pass_for(telegram_id)}

    @app.post("/internal/scan", dependencies=[Depends(bot_auth)])
    def bot_scan(body: BotScanInput):
        return service.scan_for(body.actor_id, body.token)

    @app.post("/internal/guests/{guest_id}/check-in", dependencies=[Depends(bot_auth)])
    def bot_check_in(guest_id: UUID, body: ActorInput):
        return service.check_in_for(guest_id, body.actor_id)

    @app.get("/internal/access/{actor_id}", dependencies=[Depends(bot_auth)])
    def access(actor_id: int):
        return service.profile_for(actor_id)

    @app.post("/internal/review", dependencies=[Depends(bot_auth)])
    def start_review(body: ActorInput):
        return service.start_review(body.actor_id)

    @app.post("/internal/leave-review", dependencies=[Depends(bot_auth)])
    def leave_review(body: ActorInput):
        service.leave_review(body.actor_id)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app


class RegisterInput(BaseModel):
    telegram_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")


class SessionInput(BaseModel):
    init_data: str = Field(min_length=1, max_length=10000)


class ScanInput(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


class ActorInput(BaseModel):
    actor_id: int = Field(gt=0)


class BotScanInput(ScanInput, ActorInput):
    pass


class PhotoInput(BaseModel):
    image_base64: str = Field(min_length=1, max_length=14_000_000)
