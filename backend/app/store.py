import hashlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.async_paging import AsyncPageIterator
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError
from fastapi import HTTPException

from app.models import HistoryPage, Turn


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ConversationStore:
    def __init__(self, container: ContainerProxy):
        self.container = container

    async def create(self, owner: str) -> str:
        conversation_id = str(uuid4())
        await self.container.create_item(
            {
                "id": "metadata",
                "conversationId": conversation_id,
                "owner": owner,
                "state": "active",
            }
        )
        return conversation_id

    async def authorize(
        self, conversation_id: str, owner: str, *, allow_deleting: bool = False
    ) -> dict[str, Any]:
        try:
            item = await self.container.read_item("metadata", partition_key=conversation_id)
        except CosmosResourceNotFoundError as exc:
            raise HTTPException(404, "Conversation not found.") from exc
        if not secrets.compare_digest(item["owner"], owner):
            raise HTTPException(404, "Conversation not found.")
        if item["state"] != "active" and not allow_deleting:
            raise HTTPException(409, "Conversation deletion is incomplete. Retry deleting it.")
        return item

    async def begin(self, conversation_id: str, owner: str, turn: Turn) -> None:
        metadata = await self.authorize(conversation_id, owner)
        if metadata.get("activeTurn"):
            raise HTTPException(409, "A turn is already running. Refresh to inspect its status.")
        try:
            await self.container.execute_item_batch(
                [
                    (
                        "replace",
                        ("metadata", {**metadata, "activeTurn": turn.id}),
                        {"if_match_etag": metadata["_etag"]},
                    ),
                    ("create", (self._document(turn),)),
                ],
                partition_key=conversation_id,
            )
        except CosmosBatchOperationError as exc:
            if exc.status_code in (409, 412):
                raise HTTPException(409, "Turn already submitted or conversation changed.") from exc
            raise

    async def recover_interrupted(self, conversation_id: str, owner: str) -> None:
        metadata = await self.authorize(conversation_id, owner)
        if not metadata.get("activeTurn"):
            return
        item = await self.container.read_item(metadata["activeTurn"], partition_key=conversation_id)
        turn = Turn.model_validate(item)
        if datetime.fromisoformat(turn.created_at) > datetime.now(UTC) - timedelta(seconds=250):
            return
        turn.status = "failed"
        turn.error = "The previous request was interrupted. Submit a new request to try again."
        await self.finish(turn)

    @staticmethod
    def _document(turn: Turn) -> dict[str, Any]:
        return {
            **turn.model_dump(mode="json"),
            "conversationId": turn.conversation_id,
            "kind": "turn",
        }

    async def finish(self, turn: Turn) -> None:
        metadata = await self.container.read_item("metadata", partition_key=turn.conversation_id)
        if metadata.get("activeTurn") != turn.id:
            saved = await self.container.read_item(turn.id, partition_key=turn.conversation_id)
            if saved["status"] == turn.status:
                return
            raise RuntimeError("The conversation no longer owns this active turn.")
        released = {k: v for k, v in metadata.items() if k != "activeTurn"}
        await self.container.execute_item_batch(
            [
                ("replace", (turn.id, self._document(turn))),
                ("replace", ("metadata", released), {"if_match_etag": metadata["_etag"]}),
            ],
            partition_key=turn.conversation_id,
        )

    async def context(self, conversation_id: str, limit: int) -> tuple[list[Turn], bool]:
        items = self.container.query_items(
            query=(
                f"SELECT TOP {limit + 1} * FROM c WHERE c.kind = 'turn' "
                "AND c.status = 'completed' ORDER BY c.created_at DESC"
            ),
            partition_key=conversation_id,
        )
        turns = [Turn.model_validate(item) async for item in items]
        return list(reversed(turns[:limit])), len(turns) > limit

    async def history(self, conversation_id: str, continuation: str | None) -> HistoryPage:
        items = self.container.query_items(
            query="SELECT * FROM c WHERE c.kind = 'turn' ORDER BY c.created_at DESC",
            partition_key=conversation_id,
            max_item_count=20,
        )
        pages = items.by_page(continuation_token=continuation)
        if not isinstance(pages, AsyncPageIterator):
            raise RuntimeError("Cosmos SDK did not return a continuation-aware page iterator.")
        turns = []
        async for page in pages:
            turns = [Turn.model_validate(item) async for item in page]
            break
        return HistoryPage(turns=turns, continuation=pages.continuation_token)

    async def delete(self, conversation_id: str, owner: str) -> None:
        metadata = await self.authorize(conversation_id, owner, allow_deleting=True)
        if metadata.get("activeTurn"):
            raise HTTPException(409, "Wait for the active turn before deleting the conversation.")
        if metadata["state"] != "deleting":
            await self.container.replace_item(
                "metadata",
                {**metadata, "state": "deleting"},
                etag=metadata["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        items: AsyncIterator[dict[str, Any]] = self.container.query_items(
            query="SELECT c.id FROM c WHERE c.id != 'metadata'", partition_key=conversation_id
        )
        async for item in items:
            await self.container.delete_item(item["id"], partition_key=conversation_id)
        await self.container.delete_item("metadata", partition_key=conversation_id)
