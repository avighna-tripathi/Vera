from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vera_bot.store import ContextStore


@dataclass(slots=True)
class ResolvedContexts:
    category: dict[str, Any]
    merchant: dict[str, Any]
    trigger: dict[str, Any]
    customer: dict[str, Any] | None


class ContextResolver:
    def __init__(self, context_store: ContextStore) -> None:
        self.context_store = context_store

    def resolve_for_trigger(self, trigger_id: str) -> ResolvedContexts | None:
        trigger = self.context_store.get_payload("trigger", trigger_id)
        if not trigger:
            return None
        merchant_id = trigger.get("merchant_id")
        merchant = self.context_store.get_payload("merchant", merchant_id)
        if not merchant:
            return None
        category = self.context_store.get_payload("category", merchant.get("category_slug"))
        if not category:
            return None
        customer = None
        if trigger.get("scope") == "customer":
            customer = self.context_store.get_payload("customer", trigger.get("customer_id"))
            if not customer:
                return None
        return ResolvedContexts(category=category, merchant=merchant, trigger=trigger, customer=customer)

