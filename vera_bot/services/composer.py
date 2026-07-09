from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vera_bot.services.context_resolver import ResolvedContexts
from vera_bot.services.llm import OpenRouterRefiner
from vera_bot.services.templates import template_name_for, template_params_for
from vera_bot.services.validators import sanitize_text


def _money(value: Any) -> str:
    text = str(value)
    return text if text.startswith("₹") else f"₹{text}"


def _merchant_name(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "there")


def _business_name(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("name", "your business")


def _active_offers(merchant: dict[str, Any]) -> list[dict[str, Any]]:
    return [offer for offer in merchant.get("offers", []) if offer.get("status") == "active"]


def _digest_by_id(category: dict[str, Any], digest_id: str | None) -> dict[str, Any] | None:
    if not digest_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == digest_id:
            return item
    return None


def _language_pref(customer: dict[str, Any] | None) -> str:
    if not customer:
        return "english"
    return str(customer.get("identity", {}).get("language_pref", "english")).lower()


def _humanize_token(text: str) -> str:
    return str(text).replace("_", " ")


@dataclass(slots=True)
class Composed:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]


class Composer:
    def __init__(self, refiner: OpenRouterRefiner | None = None) -> None:
        self.refiner = refiner

    def compose(self, resolved: ResolvedContexts) -> Composed:
        trigger = resolved.trigger
        send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera"
        method = getattr(self, f"_compose_{trigger.get('kind')}", self._compose_generic)
        body, cta, rationale = method(resolved)
        if self.refiner and self.refiner.enabled:
            try:
                refined = self.refiner.refine(resolved, body, cta, rationale)
                body = refined.body or body
                cta = refined.cta or cta
                rationale = refined.rationale or rationale
            except Exception:
                # Keep deterministic fallback if the network/model path fails.
                pass
        body = sanitize_text(body)
        template_name = template_name_for(trigger.get("kind", "generic"), send_as)
        customer_name = resolved.customer.get("identity", {}).get("name") if resolved.customer else None
        template_params = template_params_for(body, _business_name(resolved.merchant), customer_name=customer_name)
        return Composed(
            body=body,
            cta=cta,
            send_as=send_as,
            suppression_key=trigger.get("suppression_key", ""),
            rationale=rationale,
            template_name=template_name,
            template_params=template_params,
        )

    def _compose_research_digest(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("top_item_id"))
        owner = _merchant_name(merchant)
        if digest:
            trial_n = digest.get("trial_n")
            segment = digest.get("patient_segment", "patient cohort").replace("_", " ")
            title = digest.get("title", "")
            summary = digest.get("summary", title).rstrip(".")
            source = digest.get("source", "this week’s digest")
            body = (
                f"Dr. {owner}, {source} has one useful item: {title}. "
                f"{trial_n:,}-patient data on {segment} showed {summary.split('. ')[0]}. "
                f"Want me to draft a patient-friendly WhatsApp or a short Google post from it?"
                if isinstance(trial_n, int)
                else f"Dr. {owner}, this week’s dentistry digest has one useful item: {digest.get('title', '')}. "
                f"Worth a quick look. Want me to turn it into a patient WhatsApp or a short Google post?"
            )
            rationale = "Research-led outreach using category digest evidence and a low-friction follow-up asset."
            return body, "open_ended", rationale
        return self._compose_generic(resolved)

    def _compose_regulation_change(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("top_item_id"))
        owner = _merchant_name(merchant)
        deadline = trigger.get("payload", {}).get("deadline_iso", "")
        date_text = deadline[:10] if deadline else "the deadline"
        title = digest.get("title") if digest else "a compliance update"
        action = digest.get("actionable") if digest else "review your SOPs"
        body = f"Dr. {owner}, quick compliance heads-up: {title}, effective by {date_text}. Suggested next step: {action}. Want a 3-point checklist for your clinic?"
        rationale = "Urgent merchant-facing compliance nudge with concrete next-step framing."
        return body, "binary_yes_no", rationale

    def _compose_cde_opportunity(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("digest_item_id"))
        owner = _merchant_name(merchant)
        credits = trigger.get("payload", {}).get("credits")
        fee = trigger.get("payload", {}).get("fee")
        title = digest.get("title") if digest else "a relevant CDE session"
        body = f"Dr. {owner}, one relevant CDE option this week: {title}. {credits} credits, {fee}. Want the session summary and a reminder draft?"
        rationale = "Professional-development message anchored to category-specific continuing education."
        return body, "binary_yes_no", rationale

    def _compose_competitor_opened(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        body = (
            f"Dr. {owner}, heads-up: {payload.get('competitor_name', 'a nearby clinic')} opened about "
            f"{payload.get('distance_km', '?')} km away with {payload.get('their_offer', 'a low-entry offer')}. "
            f"You already have credibility on your side. Want me to draft a cleaner counter-positioning line for your GBP?"
        )
        rationale = "Competitive alert that turns proximity threat into a messaging opportunity."
        return body, "binary_yes_no", rationale

    def _compose_perf_dip(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        metric = payload.get("metric", "performance")
        delta_pct = abs(int(float(payload.get("delta_pct", 0)) * 100))
        baseline = payload.get("vs_baseline")
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "your next best service offer"
        body = (
            f"{owner}, your {metric} dipped {delta_pct}% in the last {payload.get('window', '7d')} "
            f"vs baseline {baseline}. Rather than a generic discount, I’d push {offer_text} with one sharp line. "
            f"Want me to draft it now?"
        )
        rationale = "Performance dip message uses merchant metrics and a concrete recovery asset."
        return body, "binary_yes_no", rationale

    def _compose_perf_spike(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        metric = payload.get("metric", "performance")
        delta_pct = int(float(payload.get("delta_pct", 0)) * 100)
        body = f"{owner}, your {metric} is up {delta_pct}% this week. Looks like something is working. Want me to turn that momentum into a post or offer follow-up while the signal is hot?"
        rationale = "Positive performance nudge encourages fast follow-through while momentum is fresh."
        return body, "open_ended", rationale

    def _compose_milestone_reached(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        body = f"{owner}, you’re at {payload.get('value_now')} {payload.get('metric', 'milestone units')} and close to {payload.get('milestone_value')}. Want a 2-line push to help you cross it this week?"
        rationale = "Milestone framing uses momentum plus a small, actionable asset."
        return body, "binary_yes_no", rationale

    def _compose_active_planning_intent(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        topic = str(payload.get("intent_topic", "this plan")).replace("_", " ")
        body = f"{owner}, here’s a starter structure for {topic}: clear offer, who it’s for, price/entry point, and one CTA. I can draft the full merchant-ready version next. Want the first draft now?"
        rationale = "Merchant has shown explicit planning intent, so the message moves straight into execution."
        return body, "binary_yes_no", rationale

    def _compose_seasonal_perf_dip(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        delta_pct = abs(int(float(payload.get("delta_pct", 0)) * 100))
        body = f"{owner}, your {payload.get('metric', 'views')} are down {delta_pct}% this week, but this looks seasonal rather than broken. Better move: protect retention now and save heavy spend for the stronger window. Want a retention message for your current base?"
        rationale = "Seasonal framing reduces panic and shifts toward a smarter next step."
        return body, "binary_yes_no", rationale

    def _compose_review_theme_emerged(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        theme = str(payload.get("theme", "service issue")).replace("_", " ")
        count = payload.get("occurrences_30d", 0)
        body = f"{owner}, {count} recent reviews pointed to {theme}. Fixing the issue matters, but so does how you answer it publicly. Want me to draft a calm response template you can reuse?"
        rationale = "Review-pattern outreach pairs operational signal with a ready-to-use response artifact."
        return body, "binary_yes_no", rationale

    def _compose_renewal_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        amount = payload.get("renewal_amount")
        body = f"{owner}, your {payload.get('plan', 'plan')} renewal is due in {payload.get('days_remaining')} days. If useful, I can summarize what’s currently working before you decide on {_money(amount)} renewal. Want that snapshot?"
        rationale = "Renewal nudge avoids pressure and offers a decision-support summary instead."
        return body, "binary_yes_no", rationale

    def _compose_curious_ask_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        owner = _merchant_name(merchant)
        body = f"Hi {owner}, quick check: what’s the most asked-for service at {_business_name(merchant)} this week? I’ll turn your answer into a Google post plus a short WhatsApp reply you can reuse."
        rationale = "Curiosity-led message uses the asking-the-merchant lever and promises immediate reciprocation."
        return body, "open_ended", rationale

    def _compose_festival_upcoming(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        festival = payload.get("festival", "the upcoming festival")
        days = payload.get("days_until")
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "one specific festive offer"
        body = f"{owner}, {festival} is {days} days away. Better than a vague discount: lead with {offer_text} and a clear booking/use window. Want me to draft the festive message?"
        rationale = "Festival message turns timing into a specific, category-correct offer prompt."
        return body, "binary_yes_no", rationale

    def _compose_ipl_match_today(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "a delivery-only combo"
        body = (
            f"Quick heads-up {owner}: {payload.get('match')} is on today at {payload.get('venue')}. "
            f"For this slot, I’d back delivery over dine-in and push {offer_text}. "
            f"Want a 3-line match-night banner draft?"
        )
        rationale = "Trigger-aware restaurant recommendation adds judgment instead of merely restating the event."
        return body, "binary_yes_no", rationale

    def _compose_winback_eligible(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        body = f"{owner}, you’ve been out for {payload.get('days_since_expiry')} days and performance has softened since then. I can draft a sharp reactivation note that focuses on one concrete business win, not a hard sell. Want it?"
        rationale = "Winback framing acknowledges lapse while keeping the re-entry ask low pressure."
        return body, "binary_yes_no", rationale

    def _compose_gbp_unverified(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        uplift = int(float(payload.get("estimated_uplift_pct", 0)) * 100)
        body = f"{owner}, your Google profile is still unverified. That alone may be costing you roughly {uplift}% visibility. Want the fastest verification path broken into 3 steps?"
        rationale = "GBP verification message uses a concrete upside and offers a simple checklist."
        return body, "binary_yes_no", rationale

    def _compose_supply_alert(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        batches = ", ".join(payload.get("affected_batches", []))
        body = f"{owner}, urgent: {payload.get('molecule')} batches {batches} from {payload.get('manufacturer')} were flagged. Want me to draft the customer note and replacement-pickup workflow?"
        rationale = "Compliance-sensitive pharmacy alert grounded in precise batch data."
        return body, "binary_yes_no", rationale

    def _compose_category_seasonal(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        trend = ", ".join(payload.get("trends", [])[:3])
        body = f"{owner}, summer demand is shifting: {trend}. Worth adjusting what stays front-and-center this week. Want a shelf/display suggestion list in one message?"
        rationale = "Seasonal category digest translated into a merchant-operational action."
        return body, "binary_yes_no", rationale

    def _compose_dormant_with_vera(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        body = f"{owner}, it’s been {payload.get('days_since_last_merchant_message')} days since we last spoke. Rather than revisit {payload.get('last_topic', 'the old topic')}, want one fresh idea tied to what’s happening in your category this week?"
        rationale = "Dormancy re-entry deliberately avoids stale follow-up and offers a fresh angle."
        return body, "binary_yes_no", rationale

    def _compose_recall_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        slots = payload.get("available_slots", [])
        slot_labels = [slot.get("label") for slot in slots[:2] if slot.get("label")]
        slot_text = " or ".join(slot_labels) if slot_labels else "two weekday evening slots"
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "your recall visit"
        service_due = _humanize_token(payload.get("service_due", "recall"))
        body = f"Hi {customer_name}, {_business_name(merchant)} here. It’s been a while since your last visit and your {service_due} is due. Apke liye {slot_text} ready hain. {offer_text}. Reply 1 or 2, or share a better time."
        rationale = "Customer recall message uses timing, slot specificity, and the merchant’s active offer."
        return body, "multi_choice_slot", rationale

    def _compose_customer_lapsed_hard(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        focus = payload.get("previous_focus", "your earlier goal").replace("_", " ")
        body = f"Hi {customer_name}, {_merchant_name(merchant)} from {_business_name(merchant)} here. It’s been about {payload.get('days_since_last_visit')} days. No pressure, but we’ve got something that fits your earlier {focus} goal. Want me to hold a trial slot for you this week?"
        rationale = "Winback message uses warm, low-shame framing and refers back to the customer’s prior goal."
        return body, "binary_yes_no", rationale

    def _compose_trial_followup(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        slot = (payload.get("next_session_options") or [{}])[0].get("label", "a follow-up slot")
        body = f"Hi {customer_name}, thanks again for trying {_business_name(merchant)}. If you want to continue, I can hold {slot} for the next session. Reply YES and I’ll block it."
        rationale = "Trial follow-up pushes a single, concrete next slot with a binary commit."
        return body, "binary_yes_no", rationale

    def _compose_chronic_refill_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        lang = _language_pref(customer)
        names = ", ".join(payload.get("molecule_list", [])[:3])
        greeting = "Namaste" if "hi" in lang else "Hello"
        body = f"{greeting}, {_business_name(merchant)} yahan. {names} refill due ho raha hai soon. Same dose pack ready rakh sakte hain, and delivery to saved address is available. Reply CONFIRM to dispatch."
        rationale = "Refill reminder stays precise, respectful, and operationally clear."
        return body, "binary_confirm_cancel", rationale

    def _compose_wedding_package_followup(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        days = payload.get("days_to_wedding")
        body = f"Hi {customer_name}, {_merchant_name(merchant)} from {_business_name(merchant)} here. {days} days to your wedding means this is a smart window to start skin-prep planning. Want me to block your first consultation slot?"
        rationale = "Bridal follow-up uses the wedding timeline as the concrete reason to message now."
        return body, "binary_yes_no", rationale

    def _compose_generic(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        owner = _merchant_name(merchant)
        category = resolved.category.get("display_name", resolved.category.get("slug", "your category"))
        body = f"{owner}, I spotted something relevant for your {category.lower()} business around {trigger.get('kind', 'your current context')}. Want a concise draft message or next-step plan?"
        rationale = "Fallback grounded in trigger kind and merchant context without inventing details."
        return body, "open_ended", rationale
