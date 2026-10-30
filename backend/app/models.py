from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    turn_id: UUID
    message: str = Field(min_length=1, max_length=16000)


class Recipe(BaseModel):
    title: str
    servings: int = Field(ge=1, le=12)
    ingredients: list[str] = Field(min_length=1, max_length=30)
    steps: list[str] = Field(min_length=1, max_length=20)
    notes: str


class WinePairing(BaseModel):
    wine: str
    rationale: str
    serving_tip: str


class Menu(BaseModel):
    title: str
    introduction: str
    recipe: Recipe
    pairing: WinePairing
    review_notes: str


class Turn(BaseModel):
    id: str
    conversation_id: str
    message: str
    created_at: str
    status: Literal["pending", "completed", "failed"]
    trace_id: str
    menu: Menu | None = None
    error: str | None = None
    context_truncated: bool = False


class HistoryPage(BaseModel):
    turns: list[Turn]
    continuation: str | None = None


class ConversationView(BaseModel):
    id: str
