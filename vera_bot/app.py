from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, Response

from vera_bot.config import SETTINGS
from vera_bot.models import (
    ContextPushRequest,
    ContextPushResponse,
    HealthResponse,
    MetadataResponse,
    OutboundAction,
    ReplyRequest,
    ReplyResponse,
    TickRequest,
    TickResponse,
)
from vera_bot.services.composer import Composer
from vera_bot.services.context_resolver import ContextResolver
from vera_bot.services.llm import OpenRouterRefiner
from vera_bot.services.reply_policy_v2 import ReplyPolicy
from vera_bot.services.trigger_selector import TriggerSelector, score_trigger
from vera_bot.services.validators import avoid_repetition, validate_action_shape
from vera_bot.store import ContextStore, ConversationStore, SuppressionStore, parse_dt, utc_now, utc_now_iso

START = time.time()

app = FastAPI(title="magicpin challenge bot", version=SETTINGS.version)

context_store = ContextStore()
conversation_store = ConversationStore()
suppression_store = SuppressionStore()
resolver = ContextResolver(context_store)
selector = TriggerSelector(suppression_store)
composer = Composer(
    refiner=OpenRouterRefiner(
        api_key=SETTINGS.openrouter_api_key,
        model=SETTINGS.openrouter_model,
        referer=SETTINGS.openrouter_referer,
        title=SETTINGS.openrouter_title,
    )
)
reply_policy = ReplyPolicy(suppression_store)


@dataclass(slots=True)
class ReplyResolvedFallback:
    merchant: dict
    trigger: dict
    category: dict
    customer: dict | None = None


@app.get("/v1/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", uptime_seconds=int(time.time() - START), contexts_loaded=context_store.counts())


@app.get("/v1/metadata", response_model=MetadataResponse)
async def metadata() -> MetadataResponse:
    return MetadataResponse(
        team_name=SETTINGS.team_name,
        team_members=SETTINGS.team_members,
        model=SETTINGS.model_name,
        approach=SETTINGS.approach,
        contact_email=SETTINGS.contact_email,
        version=SETTINGS.version,
        submitted_at=SETTINGS.submitted_at,
    )


@app.post("/v1/context", response_model=ContextPushResponse)
async def push_context(body: ContextPushRequest, response: Response) -> ContextPushResponse:
    accepted, stored = context_store.upsert(body.scope, body.context_id, body.version, body.payload, body.delivered_at)
    if not accepted:
        response.status_code = 409
        return ContextPushResponse(accepted=False, reason="stale_version", current_version=stored.version)
    return ContextPushResponse(
        accepted=True,
        ack_id=f"ack_{body.context_id}_v{body.version}",
        stored_at=stored.stored_at,
    )


@app.post("/v1/tick", response_model=TickResponse)
async def tick(body: TickRequest) -> TickResponse:
    resolved_batch = []
    for trigger_id in body.available_triggers:
        resolved = resolver.resolve_for_trigger(trigger_id)
        if not resolved:
            continue
        decision = selector.should_send(resolved, body.now)
        if not decision.allowed:
            continue
        resolved_batch.append((score_trigger(resolved), resolved))

    resolved_batch.sort(key=lambda item: item[0], reverse=True)

    actions: list[OutboundAction] = []
    touched_merchants: set[str] = set()
    for _score, resolved in resolved_batch:
        merchant_id = resolved.merchant.get("merchant_id", "")
        if merchant_id in touched_merchants:
            continue

        composed = composer.compose(resolved)
        conversation_id = build_conversation_id(resolved)
        record = conversation_store.create_or_replace(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=resolved.trigger.get("customer_id"),
            trigger_id=resolved.trigger.get("id", ""),
            send_as=composed.send_as,
            trigger_kind=resolved.trigger.get("kind", "generic"),
        )
        final_body = avoid_repetition(composed.body, record.sent_bodies)
        valid, _error = validate_action_shape(final_body, composed.rationale)
        if not valid:
            continue

        action = OutboundAction(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=resolved.trigger.get("customer_id"),
            send_as=composed.send_as,
            trigger_id=resolved.trigger.get("id", ""),
            template_name=composed.template_name,
            template_params=composed.template_params,
            body=final_body,
            cta=composed.cta,
            suppression_key=composed.suppression_key,
            rationale=composed.rationale,
        )
        record.turns.append({"from": "bot", "body": final_body, "at": utc_now_iso()})
        conversation_store.remember_sent_body(conversation_id, final_body)
        suppression_store.suppress(composed.suppression_key, _suppression_expiry(resolved.trigger))
        suppression_store.set_merchant_cooldown(merchant_id, seconds=4 * 3600)
        touched_merchants.add(merchant_id)
        actions.append(action)
        if len(actions) >= 20:
            break

    return TickResponse(actions=actions)


@app.post("/v1/reply", response_model=ReplyResponse)
async def reply(body: ReplyRequest) -> ReplyResponse:
    record = conversation_store.get(body.conversation_id)
    if not record:
        merchant_id = body.merchant_id or "unknown_merchant"
        record = conversation_store.create_or_replace(
            conversation_id=body.conversation_id,
            merchant_id=merchant_id,
            customer_id=body.customer_id,
            trigger_id="ad_hoc",
            send_as="vera",
            trigger_kind="generic",
        )

    conversation_store.append_turn(
        body.conversation_id,
        {"from": body.from_role, "body": body.message, "at": body.received_at, "turn_number": body.turn_number},
    )

    resolved = resolver.resolve_for_trigger(record.trigger_id)
    if not resolved:
        merchant = context_store.get_payload("merchant", record.merchant_id) or {"merchant_id": record.merchant_id, "identity": {"name": record.merchant_id}}
        fallback_trigger = {"id": record.trigger_id, "kind": record.trigger_kind, "scope": "merchant", "payload": {}, "suppression_key": ""}
        category = context_store.get_payload("category", merchant.get("category_slug")) or {"slug": "generic", "display_name": "Business"}
        resolved = ReplyResolvedFallback(merchant=merchant, trigger=fallback_trigger, category=category, customer=None)

    outcome = reply_policy.decide(record, resolved, body.message)
    action = outcome["action"]
    if action == "send":
        message_body = outcome["body"]
        conversation_store.remember_sent_body(body.conversation_id, message_body)
        record.turns.append({"from": "bot", "body": message_body, "at": utc_now_iso()})
        return ReplyResponse(action="send", body=message_body, cta=outcome.get("cta", "open_ended"), rationale=outcome["rationale"])
    if action == "wait":
        suppression_store.set_merchant_cooldown(record.merchant_id, outcome["wait_seconds"])
        return ReplyResponse(action="wait", wait_seconds=outcome["wait_seconds"], rationale=outcome["rationale"])
    record.ended = True
    return ReplyResponse(action="end", rationale=outcome["rationale"])


def build_conversation_id(resolved) -> str:
    trigger = resolved.trigger
    merchant = resolved.merchant
    customer_id = trigger.get("customer_id")
    suffix = trigger.get("kind", "generic")
    if customer_id:
        return f"conv_{customer_id}_{suffix}"
    return f"conv_{merchant.get('merchant_id', 'merchant')}_{suffix}"


def _suppression_expiry(trigger: dict) -> object:
    expires = parse_dt(trigger.get("expires_at"))
    if expires:
        return expires
    return utc_now() + timedelta(days=7)
