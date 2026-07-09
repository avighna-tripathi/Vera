from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from vera_bot.services.context_resolver import ResolvedContexts
from vera_bot.store import SuppressionStore, parse_dt


def score_trigger(resolved: ResolvedContexts) -> int:
    trigger = resolved.trigger
    merchant = resolved.merchant
    score = int(trigger.get("urgency", 0)) * 10
    kind = trigger.get("kind", "")
    if kind in {"active_planning_intent", "renewal_due", "supply_alert", "regulation_change"}:
        score += 25
    if "engaged_in_last_24h" in merchant.get("signals", []) or "engaged_in_last_48h" in merchant.get("signals", []):
        score += 8
    if trigger.get("scope") == "customer":
        score += 6
    return score


@dataclass(slots=True)
class SelectionDecision:
    allowed: bool
    reason: str | None = None


class TriggerSelector:
    def __init__(self, suppression_store: SuppressionStore) -> None:
        self.suppression_store = suppression_store

    def should_send(self, resolved: ResolvedContexts, now_iso: str) -> SelectionDecision:
        now = parse_dt(now_iso) or datetime.now(timezone.utc)
        trigger = resolved.trigger
        merchant = resolved.merchant
        customer = resolved.customer

        expires_at = parse_dt(trigger.get("expires_at"))
        if expires_at and expires_at <= now:
            return SelectionDecision(False, "expired")

        merchant_id = merchant.get("merchant_id", "")
        if self.suppression_store.merchant_opted_out(merchant_id, now):
            return SelectionDecision(False, "merchant_opted_out")

        if self.suppression_store.merchant_on_cooldown(merchant_id, now):
            return SelectionDecision(False, "merchant_cooldown")

        suppression_key = trigger.get("suppression_key", "")
        if self.suppression_store.is_suppressed(suppression_key, now):
            return SelectionDecision(False, "suppressed")

        if trigger.get("scope") == "customer":
            if not customer:
                return SelectionDecision(False, "missing_customer")
            if not customer.get("preferences", {}).get("reminder_opt_in", False):
                return SelectionDecision(False, "customer_not_opted_in")

        if trigger.get("kind") == "curious_ask_due" and "merchant_no_reply" in [
            item.get("engagement") for item in merchant.get("conversation_history", [])[-2:]
        ]:
            return SelectionDecision(False, "recent_non_response")

        return SelectionDecision(True)

