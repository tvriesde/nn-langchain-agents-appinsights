import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.cosmos.exceptions import CosmosBatchOperationError
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_openai import AzureChatOpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agents import RecipeAgents
from app.config import Settings
from app.main import Services, create_app
from app.models import Turn
from app.store import ConversationStore, owner_hash
from app.telemetry import ConversationCorrelationProcessor

RECIPE = {
    "title": "Lemon salmon",
    "servings": 2,
    "ingredients": ["salmon", "lemon"],
    "steps": ["Cook salmon and serve with lemon."],
    "notes": "Cook thoroughly.",
}
PAIRING = {
    "wine": "Chardonnay",
    "rationale": "Matches the salmon.",
    "serving_tip": "Serve chilled.",
}
MENU = {
    "title": "A little dinner",
    "introduction": "Salmon and a fresh pairing.",
    "recipe": RECIPE,
    "pairing": PAIRING,
    "review_notes": "Checked ingredient consistency.",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        cosmos_endpoint="https://cosmos.example.test",
        azure_openai_endpoint="https://model.example.test",
        cookie_secure=False,
        applicationinsights_connection_string=None,
    )


@pytest.fixture(scope="module")
def spans():
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(ConversationCorrelationProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    instrumentor = next(
        entry.load()
        for entry in entry_points(group="opentelemetry_instrumentor")
        if entry.name == "langchain"
    )()
    instrumentor.instrument(tracer_provider=provider, enable_sensitive_data=False)
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


class Credential:
    def get_token(self, *scopes, **kwargs) -> AccessToken:
        return AccessToken("test-only-token", 9999999999)


class MemoryStore(ConversationStore):
    def __init__(self):
        self.owners: dict[str, str] = {}
        self.turns: dict[str, list[Turn]] = {}
        self.finished: list[str] = []
        self.container = MagicMock()
        self.container.read = AsyncMock(return_value={})

    async def create(self, owner: str) -> str:
        cid = str(uuid4())
        self.owners[cid] = owner
        self.turns[cid] = []
        return cid

    async def authorize(self, conversation_id, owner, *, allow_deleting=False):
        if self.owners.get(conversation_id) != owner:
            raise HTTPException(404, "Conversation not found.")
        return {}

    async def recover_interrupted(self, conversation_id, owner):
        await self.authorize(conversation_id, owner)

    async def context(self, conversation_id, limit):
        completed = [t for t in self.turns[conversation_id] if t.status == "completed"]
        return completed[-limit:], len(completed) > limit

    async def begin(self, conversation_id, owner, turn):
        await self.authorize(conversation_id, owner)
        if any(t.id == turn.id or t.status == "pending" for t in self.turns[conversation_id]):
            raise HTTPException(409, "Already submitted.")
        self.turns[conversation_id].append(turn.model_copy(deep=True))

    async def finish(self, turn):
        self.turns[turn.conversation_id] = [
            turn.model_copy(deep=True) if existing.id == turn.id else existing
            for existing in self.turns[turn.conversation_id]
        ]
        self.finished.append(turn.status)

    async def history(self, conversation_id, continuation):
        from app.models import HistoryPage

        return HistoryPage(turns=list(reversed(self.turns[conversation_id])))

    async def delete(self, conversation_id, owner):
        await self.authorize(conversation_id, owner)
        del self.turns[conversation_id]
        del self.owners[conversation_id]


def model_agents(settings: Settings, outputs: list[dict]) -> tuple[RecipeAgents, list[dict]]:
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        value = outputs[len(requests) - 1]
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-6-luna",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(value)},
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            },
        )

    agents = RecipeAgents(settings, Credential())
    agents.model = AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_deployment="gpt-6-luna",
        model="gpt-6-luna",
        api_version="2024-10-21",
        api_key="test-only",
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return agents, requests


def test_stream_handoffs_history_and_native_spans(settings, spans):
    spans.clear()
    agents, requests = model_agents(settings, [RECIPE, PAIRING, MENU] * 2)
    store = MemoryStore()
    with TestClient(create_app(settings, Services(store, agents))) as client:
        conversation = client.post("/api/conversations").json()["id"]
        cookie = client.cookies.get("recipe_browser")
        assert cookie and len(cookie) == 43
        traces = []
        for message in ["salmon and lemon", "make it dinner for two"]:
            response = client.post(
                f"/api/conversations/{conversation}/chat",
                json={
                    "turn_id": str(uuid4()),
                    "message": message,
                },
            )
            assert response.status_code == 200
            assert response.text.index("Chef Agent") < response.text.index("Wine Agent")
            assert response.text.index("Wine Agent") < response.text.index("Review Agent")
            assert "event: completed" in response.text
            assert store.finished[-1] == "completed"
            saved = client.get(f"/api/conversations/{conversation}").json()["turns"]
            traces.append(saved[0]["trace_id"])
        assert traces[0] != traces[1]
        assert all(t != "0" * 32 for t in traces)
        assert RECIPE["title"] in requests[1]["messages"][-1]["content"]
        assert PAIRING["wine"] in requests[2]["messages"][-1]["content"]
        assert "salmon and lemon" in requests[3]["messages"][-1]["content"]
        assert all("json_schema" in json.dumps(r["response_format"]) for r in requests)
        assert len(client.get(f"/api/conversations/{conversation}").json()["turns"]) == 2
        assert client.get("/health/ready").status_code == 200

    finished = spans.get_finished_spans()
    agent_spans = [s for s in finished if s.name.startswith("invoke_agent ")]
    assert len(agent_spans) == 6
    assert {s.attributes["gen_ai.agent.name"] for s in agent_spans} == {
        "Chef Agent",
        "Wine Agent",
        "Review Agent",
    }
    llm_spans = [s for s in finished if s.attributes.get("gen_ai.operation.name") == "chat"]
    assert len(llm_spans) == 6
    assert all(s.attributes["gen_ai.conversation.id"] == conversation for s in llm_spans)
    assert all(s.attributes["session.id"] == conversation for s in llm_spans)
    assert all(s.attributes["gen_ai.usage.input_tokens"] == 42 for s in llm_spans)
    assert all(s.attributes["gen_ai.usage.output_tokens"] == 12 for s in llm_spans)
    assert all("gen_ai.input.messages" in s.attributes for s in agent_spans)
    assert all("gen_ai.output.messages" in s.attributes for s in agent_spans)
    workflow_ids = {
        s.context.trace_id: s.context.span_id for s in finished if s.name == "recipe workflow"
    }
    assert all(
        s.parent.span_id == workflow_ids[s.context.trace_id] for s in agent_spans
    ), "Sequential agents must be siblings under their workflow, not children of completed agents."
    by_id = {s.context.span_id: s for s in finished}
    for model_span in llm_spans:
        parent = by_id[model_span.parent.span_id]
        while parent.name not in {s.name for s in agent_spans}:
            parent = by_id[parent.parent.span_id]
        assert parent.attributes["gen_ai.agent.name"] in {
            "Chef Agent",
            "Wine Agent",
            "Review Agent",
        }


def test_conversations_are_private_deletable_and_same_origin(settings):
    agents, _ = model_agents(settings, [])
    store = MemoryStore()
    app = create_app(settings, Services(store, agents))
    with TestClient(app) as owner, TestClient(app) as stranger:
        cid = owner.post("/api/conversations").json()["id"]
        stranger.post("/api/conversations")
        assert stranger.get(f"/api/conversations/{cid}").status_code == 404
        assert stranger.delete(f"/api/conversations/{cid}").status_code == 404
        assert (
            owner.delete(
                f"/api/conversations/{cid}",
                headers={
                    "Origin": "https://evil.example",
                },
            ).status_code
            == 403
        )
        assert (
            owner.post(
                "/api/conversations",
                headers={
                    "Sec-Fetch-Site": "cross-site",
                },
            ).status_code
            == 403
        )
        assert owner.delete(f"/api/conversations/{cid}").status_code == 204
        assert owner.get(f"/api/conversations/{cid}").status_code == 404


class FailedAgents(RecipeAgents):
    def __init__(self):
        pass

    async def run(self, *args, **kwargs) -> AsyncIterator[tuple[str, dict]]:
        yield "agent_started", {"agent": "Chef Agent"}
        raise RuntimeError("model failure")


def test_failure_duplicate_and_input_limits(settings):
    store = MemoryStore()
    with TestClient(create_app(settings, Services(store, FailedAgents()))) as client:
        cid = client.post("/api/conversations").json()["id"]
        body = {"turn_id": str(uuid4()), "message": "salmon"}
        response = client.post(f"/api/conversations/{cid}/chat", json=body)
        assert "event: error" in response.text
        assert "event: completed" not in response.text
        assert store.finished == ["failed"]
        assert client.post(f"/api/conversations/{cid}/chat", json=body).status_code == 409
        body = {"turn_id": str(uuid4()), "message": " "}
        assert client.post(f"/api/conversations/{cid}/chat", json=body).status_code == 422
        body["message"] = "x" * (settings.max_input_chars + 1)
        assert client.post(f"/api/conversations/{cid}/chat", json=body).status_code == 422


async def test_cosmos_batches_and_stale_turn_recovery():
    cid = str(uuid4())
    turn = Turn(
        id=str(uuid4()),
        conversation_id=cid,
        message="salmon",
        status="pending",
        trace_id="abc",
        created_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )
    metadata = {
        "id": "metadata",
        "conversationId": cid,
        "owner": owner_hash("browser"),
        "state": "active",
        "_etag": "etag",
    }
    container = MagicMock()
    container.read_item = AsyncMock(return_value=metadata)
    container.execute_item_batch = AsyncMock(return_value=[])
    store = ConversationStore(container)
    await store.begin(cid, owner_hash("browser"), turn)
    batch = container.execute_item_batch.call_args.args[0]
    assert batch[0][2] == {"if_match_etag": "etag"}
    assert batch[1][0] == "create"
    container.execute_item_batch.side_effect = CosmosBatchOperationError(
        status_code=409, headers={}
    )
    with pytest.raises(HTTPException) as error:
        await store.begin(cid, owner_hash("browser"), turn)
    assert error.value.status_code == 409
    container.execute_item_batch.side_effect = None
    active = {**metadata, "activeTurn": turn.id}
    container.read_item.side_effect = [active, turn.model_dump(), active]
    await store.recover_interrupted(cid, owner_hash("browser"))
    recovered = container.execute_item_batch.call_args.args[0][0][1][1]
    assert recovered["status"] == "failed"
    assert "interrupted" in recovered["error"]


async def test_cosmos_pagination_preserves_continuation():
    from azure.core.async_paging import AsyncPageIterator

    class Pages(AsyncPageIterator):
        def __init__(self):
            super().__init__(self.get_next, self.extract)

        async def get_next(self, token):
            return SimpleNamespace(items=[], token="next-page")

        async def extract(self, response):
            async def items():
                for item in response.items:
                    yield item

            return response.token, items()

    container = MagicMock()
    container.query_items.return_value.by_page.return_value = Pages()
    page = await ConversationStore(container).history(str(uuid4()), None)
    assert page.continuation == "next-page"
    assert page.turns == []


def test_content_capture_can_be_disabled(settings, spans):
    settings = settings.model_copy(update={"capture_message_content": False})
    spans.clear()
    agents, _ = model_agents(settings, [RECIPE, PAIRING, MENU])
    with TestClient(create_app(settings, Services(MemoryStore(), agents))) as client:
        assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "NO_CONTENT"
        cid = client.post("/api/conversations").json()["id"]
        response = client.post(
            f"/api/conversations/{cid}/chat",
            json={
                "turn_id": str(uuid4()),
                "message": "salmon and lemon",
            },
        )
        assert "event: completed" in response.text
    ai_spans = [s for s in spans.get_finished_spans() if "gen_ai.operation.name" in s.attributes]
    assert len(ai_spans) == 6
    assert [s.name for s in ai_spans if "gen_ai.input.messages" in s.attributes] == []
    assert [s.name for s in ai_spans if "gen_ai.output.messages" in s.attributes] == []
