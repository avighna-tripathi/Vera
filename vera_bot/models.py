from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Scope = Literal["category", "merchant", "customer", "trigger"]
SendAs = Literal["vera", "merchant_on_behalf"]
CTAType = Literal[
    "none",
    "open_ended",
    "binary_yes_no",
    "binary_confirm_cancel",
    "multi_choice_slot",
]
ReplyAction = Literal["send", "wait", "end"]


class ContextPushRequest(BaseModel):
    scope: Scope
    context_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: str | None = None


class ContextPushResponse(BaseModel):
    accepted: bool
    ack_id: str | None = None
    stored_at: str | None = None
    reason: str | None = None
    current_version: int | None = None
    details: str | None = None


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class OutboundAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: str | None = None
    send_as: SendAs
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str
    cta: CTAType
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: list[OutboundAction]


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int = Field(ge=1)


class ReplyResponse(BaseModel):
    action: ReplyAction
    body: str | None = None
    cta: CTAType | None = None
    wait_seconds: int | None = None
    rationale: str


class MetadataResponse(BaseModel):
    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: int
    contexts_loaded: dict[str, int]

