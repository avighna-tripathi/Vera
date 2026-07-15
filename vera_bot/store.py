from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class StoredContext:
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None
    stored_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class ConversationRecord:
    conversation_id: str
    merchant_id: str
    customer_id: str | None
    trigger_id: str
    send_as: str
    created_at: str
    trigger_kind: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    sent_bodies: list[str] = field(default_factory=list)
    auto_reply_count: int = 0
    ended: bool = False
    opt_out: bool = False


class ContextStore:
    def __init__(self) -> None:
        self._contexts: dict[tuple[str, str], StoredContext] = {}

    def upsert(self, scope: str, context_id: str, version: int, payload: dict[str, Any], delivered_at: str | None) -> tuple[bool, StoredContext]:
        key = (scope, context_id)
        current = self._contexts.get(key)
        if current and current.version >= version:
            return False, current
        stored = StoredContext(version=version, payload=payload, delivered_at=delivered_at)
        self._contexts[key] = stored
        return True, stored

    def get_payload(self, scope: str, context_id: str | None) -> dict[str, Any] | None:
        if not context_id:
            return None
        stored = self._contexts.get((scope, context_id))
        return stored.payload if stored else None

    def counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for scope, _context_id in self._contexts:
            counts[scope] = counts.get(scope, 0) + 1
        return counts


class ConversationStore:
    def __init__(self) -> None:
        self._items: dict[str, ConversationRecord] = {}

    def create_or_replace(
        self,
        conversation_id: str,
        merchant_id: str,
        customer_id: str | None,
        trigger_id: str,
        send_as: str,
        trigger_kind: str,
    ) -> ConversationRecord:
        record = ConversationRecord(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trigger_id,
            send_as=send_as,
            created_at=utc_now_iso(),
            trigger_kind=trigger_kind,
        )
        self._items[conversation_id] = record
        return record

    def get(self, conversation_id: str) -> ConversationRecord | None:
        return self._items.get(conversation_id)

    def append_turn(self, conversation_id: str, turn: dict[str, Any]) -> ConversationRecord | None:
        record = self._items.get(conversation_id)
        if record:
            record.turns.append(turn)
        return record

    def remember_sent_body(self, conversation_id: str, body: str) -> None:
        record = self._items.get(conversation_id)
        if record and body:
            record.sent_bodies.append(body.strip())


class SuppressionStore:
    def __init__(self) -> None:
        self._suppressed: dict[str, datetime] = {}
        self._merchant_cooldowns: dict[str, datetime] = {}
        self._opted_out_merchants: dict[str, datetime] = {}
        self._merchant_auto_reply_counts: dict[str, int] = {}

    def suppress(self, key: str, until: datetime) -> None:
        self._suppressed[key] = until

    def is_suppressed(self, key: str, now: datetime | None = None) -> bool:
        if not key:
            return False
        now = now or utc_now()
        expires_at = self._suppressed.get(key)
        return bool(expires_at and expires_at > now)

    def set_merchant_cooldown(self, merchant_id: str, seconds: int, now: datetime | None = None) -> None:
        """Anchor cooldowns to the event time when a caller provides one."""
        self._merchant_cooldowns[merchant_id] = (now or utc_now()) + timedelta(seconds=seconds)

    def merchant_on_cooldown(self, merchant_id: str, now: datetime | None = None) -> bool:
        now = now or utc_now()
        expires_at = self._merchant_cooldowns.get(merchant_id)
        return bool(expires_at and expires_at > now)

    def opt_out_merchant(self, merchant_id: str, days: int = 30) -> None:
        self._opted_out_merchants[merchant_id] = utc_now() + timedelta(days=days)

    def merchant_opted_out(self, merchant_id: str, now: datetime | None = None) -> bool:
        now = now or utc_now()
        expires_at = self._opted_out_merchants.get(merchant_id)
        return bool(expires_at and expires_at > now)

    def increment_auto_reply_count(self, merchant_id: str) -> int:
        count = self._merchant_auto_reply_counts.get(merchant_id, 0) + 1
        self._merchant_auto_reply_counts[merchant_id] = count
        return count

    def get_auto_reply_count(self, merchant_id: str) -> int:
        return self._merchant_auto_reply_counts.get(merchant_id, 0)
