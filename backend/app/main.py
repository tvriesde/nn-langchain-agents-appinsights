import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from azure.core.exceptions import AzureError
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain.agents.structured_output import StructuredOutputError
from openai import OpenAIError
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import ValidationError

from app.agents import RecipeAgents
from app.config import Settings
from app.models import ChatRequest, ConversationView, HistoryPage, Menu, Turn
from app.store import ConversationStore, owner_hash
from app.telemetry import (
    configure_telemetry,
    flush_telemetry,
    operation_span,
    record_error,
    trace_id,
)

logger = logging.getLogger("recipe-agents")
COOKIE = "recipe_browser"


@dataclass
class Services:
    store: ConversationStore
    agents: RecipeAgents


class RateLimiter:
    def __init__(self) -> None:
        self.buckets: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        for expired in [k for k, v in self.buckets.items() if not v or v[-1] < now - 60]:
            del self.buckets[expired]
        if key not in self.buckets and len(self.buckets) >= 2048:
            raise HTTPException(429, "Demo is busy; try again in a minute.")
        bucket = self.buckets[key]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(429, "Too many requests. Try again in a minute.")
        bucket.append(now)


def browser_owner(request: Request) -> str:
    token = request.cookies.get(COOKIE)
    if token is None or len(token) != 43:
        raise HTTPException(404, "Browser session not found. Start a new conversation.")
    return owner_hash(token)


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin changes are not allowed.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site changes are not allowed.")


def sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


async def stream_turn(
    services: Services, settings: Settings, turn: Turn, history: list[Turn]
) -> AsyncIterator[str]:
    with operation_span("recipe workflow", turn.conversation_id, turn.id) as span:
        turn.trace_id = trace_id()
        persisted = False
        try:
            yield sse(
                "turn_started",
                {
                    "turn_id": turn.id,
                    "trace_id": turn.trace_id,
                    "conversation_id": turn.conversation_id,
                    "context_truncated": turn.context_truncated,
                },
            )
            async with asyncio.timeout(settings.turn_timeout_seconds):
                async for event, data in services.agents.run(
                    turn.conversation_id, turn.id, turn.message, history
                ):
                    if event == "menu":
                        turn.menu = Menu.model_validate(data)
                    else:
                        yield sse(event, data)
                if turn.menu is None:
                    raise RuntimeError("The workflow did not produce a menu.")
                turn.status = "completed"
                await services.store.finish(turn)
                persisted = True
            yield sse("completed", turn.model_dump(mode="json"))
        except (asyncio.CancelledError, GeneratorExit):
            if persisted:
                raise
            turn.status = "failed"
            turn.menu = None
            turn.error = "Request disconnected before completion. Refresh to inspect history."
            span.set_attribute("error.type", "client_disconnect")
            try:
                await asyncio.shield(services.store.finish(turn))
            except (CosmosHttpResponseError, RuntimeError):
                logger.exception("Could not persist disconnected turn %s", turn.id)
            raise
        except (
            AzureError,
            OpenAIError,
            StructuredOutputError,
            ValidationError,
            TimeoutError,
            RuntimeError,
        ) as exc:
            record_error(span, exc)
            logger.exception("Recipe turn failed: %s", turn.id)
            turn.status = "failed"
            turn.menu = None
            turn.error = (
                "The request timed out. Try a new request."
                if isinstance(exc, TimeoutError)
                else "The recipe could not be completed. Refresh history before retrying."
            )
            try:
                await services.store.finish(turn)
            except (CosmosHttpResponseError, RuntimeError):
                logger.exception("Could not persist failed turn %s", turn.id)
                turn.error += " Saving its failure also failed."
            yield sse("error", turn.model_dump(mode="json"))


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_telemetry(settings)
    limiter = RateLimiter()
    slots = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if services is not None:
            app.state.services = services
            yield
            return
        async with (
            AsyncDefaultAzureCredential(
                managed_identity_client_id=settings.azure_client_id
            ) as cosmos_credential,
            CosmosClient(settings.cosmos_endpoint, credential=cosmos_credential) as cosmos_client,
        ):
            with DefaultAzureCredential(
                managed_identity_client_id=settings.azure_client_id
            ) as model_credential:
                container = cosmos_client.get_database_client(
                    settings.cosmos_database
                ).get_container_client(settings.cosmos_container)
                await container.read()
                app.state.services = Services(
                    ConversationStore(container), RecipeAgents(settings, model_credential)
                )
                try:
                    yield
                finally:
                    await asyncio.to_thread(flush_telemetry)

    app = FastAPI(title="The Agent's Table", lifespan=lifespan)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(CosmosHttpResponseError)
    async def cosmos_error(request: Request, exc: CosmosHttpResponseError) -> JSONResponse:
        logger.error("Cosmos request failed: %s", type(exc).__name__, exc_info=exc)
        record_error(trace.get_current_span(), exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "Conversation storage is unavailable. Try again later."},
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        await request.app.state.services.store.container.read()
        return {"status": "ready"}

    @app.post("/api/conversations", response_model=ConversationView, status_code=201)
    async def create_conversation(request: Request, response: Response) -> ConversationView:
        same_origin(request)
        host = request.client.host if request.client else "unknown"
        limiter.check(f"create:{host}", 20)
        token = request.cookies.get(COOKIE)
        if token is None or len(token) != 43:
            token = secrets.token_urlsafe(32)
        conversation_id = await request.app.state.services.store.create(owner_hash(token))
        response.set_cookie(
            COOKIE,
            token,
            max_age=365 * 24 * 3600,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/api",
        )
        return ConversationView(id=conversation_id)

    @app.get("/api/conversations/{conversation_id}", response_model=HistoryPage)
    async def history(
        conversation_id: UUID, request: Request, continuation: str | None = None
    ) -> HistoryPage:
        store = request.app.state.services.store
        await store.recover_interrupted(str(conversation_id), browser_owner(request))
        return await store.history(str(conversation_id), continuation)

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete(conversation_id: UUID, request: Request) -> Response:
        same_origin(request)
        await request.app.state.services.store.delete(str(conversation_id), browser_owner(request))
        return Response(status_code=204)

    @app.post("/api/conversations/{conversation_id}/chat")
    async def chat(conversation_id: UUID, body: ChatRequest, request: Request) -> StreamingResponse:
        same_origin(request)
        owner = browser_owner(request)
        message = body.message.strip()
        if not message or len(message) > settings.max_input_chars:
            raise HTTPException(
                422, f"Enter between 1 and {settings.max_input_chars} nonblank characters."
            )
        runtime: Services = request.app.state.services
        await runtime.store.authorize(str(conversation_id), owner)
        request_span = trace.get_current_span()
        request_span.set_attribute("gen_ai.conversation.id", str(conversation_id))
        request_span.set_attribute("session.id", str(conversation_id))
        request_span.set_attribute("recipe.turn.id", str(body.turn_id))
        limiter.check(f"chat:{owner}", settings.requests_per_minute)
        if slots.locked():
            raise HTTPException(429, "Both demo workflow slots are busy. Try again shortly.")
        await slots.acquire()
        acquired = True
        try:
            prior, truncated = await runtime.store.context(
                str(conversation_id), settings.max_context_turns
            )
            turn = Turn(
                id=str(body.turn_id),
                conversation_id=str(conversation_id),
                message=message,
                created_at=datetime.now(UTC).isoformat(),
                status="pending",
                trace_id=trace_id(),
                context_truncated=truncated,
            )
            await runtime.store.begin(str(conversation_id), owner, turn)
            acquired = False
        finally:
            if acquired:
                slots.release()

        async def events() -> AsyncIterator[str]:
            try:
                async for event in stream_turn(runtime, settings, turn, prior):
                    yield event
            finally:
                slots.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    if settings.static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=settings.static_dir / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(settings.static_dir / "index.html")

    return app
