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


def _category_label(merchant: dict[str, Any]) -> str:
    labels = {
        "dentists": "dental clinic",
        "gyms": "fitness studio",
        "pharmacies": "pharmacy",
        "restaurants": "restaurant",
        "salons": "salon",
    }
    return labels.get(merchant.get("category_slug", ""), _humanize_token(merchant.get("category_slug", "business")).rstrip("s"))


def _fmt_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _place_text(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    locality = identity.get("locality")
    city = identity.get("city")
    if locality and city:
        return f"{locality}, {city}"
    return locality or city or "your area"


def _performance_snapshot(merchant: dict[str, Any]) -> str:
    performance = merchant.get("performance", {})
    window = performance.get("window_days", 30)
    views = _fmt_number(performance.get("views", 0))
    calls = _fmt_number(performance.get("calls", 0))
    ctr = _fmt_pct(performance.get("ctr", 0))
    return f"Last {window} days: {views} views, {calls} calls, {ctr} CTR"


def _category_action(merchant: dict[str, Any]) -> str:
    actions = {
        "dentists": "push one recall or treatment booking message to patients who already showed intent",
        "gyms": "convert warm trial interest into a fixed 7-day class or PT follow-up",
        "pharmacies": "move repeat buyers into refill reminders with pickup or delivery confirmation",
        "restaurants": "promote one high-margin order window instead of a broad all-day discount",
        "salons": "fill the next weak slots with one rebooking message for a specific service",
    }
    return actions.get(merchant.get("category_slug", ""), "turn this into one clear customer-facing next step")


def _category_review_fix(merchant: dict[str, Any]) -> str:
    fixes = {
        "dentists": "separate clinical concern from waiting-time feedback before replying publicly",
        "gyms": "separate trainer availability, crowding, and trial follow-up before asking for new reviews",
        "pharmacies": "separate stock availability, delivery delay, and pharmacist counsel before replying",
        "restaurants": "separate food quality, late delivery, and weekend rush comments before replying",
        "salons": "separate wait time, finishing quality, and staff mention before replying",
    }
    return fixes.get(merchant.get("category_slug", ""), "group the comments into one fixable theme before replying")


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
    def __init__(self, refiner: OpenRouterRefiner | None = None, refine_known_triggers: bool = True) -> None:
        self.refiner = refiner
        self.refine_known_triggers = refine_known_triggers

    def compose(self, resolved: ResolvedContexts) -> Composed:
        trigger = resolved.trigger
        send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera"
        method_name = f"_compose_{trigger.get('kind')}"
        method = getattr(self, method_name, self._compose_generic)
        if trigger.get("payload", {}).get("placeholder"):
            body, cta, rationale = self._compose_placeholder(resolved)
        else:
            body, cta, rationale = method(resolved)
        if self.refiner and self.refiner.enabled and (self.refine_known_triggers or method_name == "_compose_generic"):
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

    def _compose_placeholder(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        """Use profile facts when a generated trigger deliberately has no detail payload."""
        merchant = resolved.merchant
        customer = resolved.customer or {}
        trigger = resolved.trigger
        payload = trigger.get("payload", {})
        kind = trigger.get("kind", "update")
        topic = _humanize_token(payload.get("metric_or_topic", kind))
        business = _business_name(merchant)

        if trigger.get("scope") == "customer":
            customer_name = customer.get("identity", {}).get("name", "there")
            if kind == "appointment_tomorrow":
                body = f"Hi {customer_name}, {business} here. Reminder for your appointment tomorrow: reply CONFIRM if you are set, or RESCHEDULE and we will help with another time."
                return body, "binary_confirm_cancel", "Appointment reminder stays useful without inventing service or time details."
            if kind == "recall_due":
                body = f"Hi {customer_name}, {business} here. You are due for a follow-up. Reply 1 for a slot this week, 2 for next week, or NO if you want us to pause reminders."
                return body, "multi_choice_slot", "Recall reminder gives a clear booking choice without inventing a service."
            if kind == "customer_lapsed_soft":
                body = f"Hi {customer_name}, {business} here. It has been a while since your last visit. Want us to hold an easy comeback slot this week? Reply YES and we will suggest two options."
                return body, "binary_yes_no", "Gentle lapsed-customer nudge keeps the ask low pressure and replyable."
            if kind == "chronic_refill_due":
                body = f"Hi {customer_name}, {business} here. Quick refill reminder: reply CONFIRM if you want us to check availability and pickup or delivery, or SKIP if you are stocked for now."
                return body, "binary_confirm_cancel", "Refill reminder is operational and consent-led even when the exact molecule is absent."
            if kind == "trial_followup":
                body = f"Hi {customer_name}, {business} here. If the trial felt useful, reply YES and we will hold the next beginner-friendly slot; reply LATER if this week is packed."
                return body, "binary_yes_no", "Trial follow-up asks for one simple commitment without inventing attendance details."
            body = f"Hi {customer_name}, {business} here. Quick check-in: reply YES if you want the simplest next step from us, or NO and we will leave it here."
            return body, "binary_yes_no", "Customer follow-up uses a low-friction opt-in without inventing history."

        owner = _merchant_name(merchant)
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        action = _category_action(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else None

        if kind == "research_digest":
            body = f"{owner}, one useful {label} update is worth turning into customer content, not just reading. {snapshot} in {place}. Want a WhatsApp plus Google post that connects it to one bookable next step?"
            return body, "binary_yes_no", "Placeholder research digest becomes a concrete content asset using profile performance."
        if kind == "perf_dip":
            body = f"{owner}, {snapshot} in {place}. If this is the dip alert, I would avoid a blanket discount; for your {label}, {action}. Want the exact 3-line recovery message?"
            return body, "binary_yes_no", "Performance dip message makes a conservative recovery decision anchored to current profile metrics."
        if kind == "perf_spike":
            body = f"{owner}, {snapshot} in {place}. If this spike is fresh, capture demand now: for your {label}, {action}. Want a same-day post and reply script?"
            return body, "binary_yes_no", "Performance spike message pushes immediate follow-through while momentum is active."
        if kind == "milestone_reached":
            body = f"{owner}, {snapshot} in {place}. Since a milestone was just hit, the next move is proof, not another offer: turn the win into one short post and one customer reply script for your {label}. Want both drafts?"
            return body, "binary_yes_no", "Milestone message converts the achievement into proof and a concrete reusable asset."
        if kind == "dormant_with_vera":
            body = f"{owner}, restarting with a generic idea would waste the moment. {snapshot} in {place}; for your {label}, the clean restart is to {action}. Want the ready-to-use draft?"
            return body, "binary_yes_no", "Dormant re-entry avoids stale follow-up and uses current profile numbers to pick one restart action."
        if kind == "review_theme_emerged":
            body = f"{owner}, a review theme is emerging and {snapshot} in {place} means public replies can affect conversion. For your {label}, first {_category_review_fix(merchant)}; then answer with one calm template. Want that template?"
            return body, "binary_yes_no", "Review-theme placeholder gives a category-specific triage decision before drafting a public response."
        if kind == "competitor_opened":
            offer_line = f" Keep {offer_text} as the hook." if offer_text else ""
            body = f"{owner}, if a competitor has opened near {place}, do not race them on price first. {snapshot}; for your {label}, lead with a sharper positioning line and one reply CTA.{offer_line} Want the counter-positioning draft?"
            return body, "binary_yes_no", "Competitor message chooses positioning over price matching and ties it to the merchant profile."
        if kind == "festival_upcoming":
            offer_line = offer_text or f"one {label}-specific offer"
            body = f"{owner}, for the upcoming festival, prepare now but hold the blast until the buying window. {snapshot} in {place}; pick {offer_line}, define the booking/use window, and save the launch draft. Want me to write it?"
            return body, "binary_yes_no", "Festival placeholder makes a timing-aware campaign decision without inventing the festival name."
        if kind == "renewal_due":
            body = f"{owner}, before you decide on renewal, check whether the profile is earning enough action. {snapshot} in {place}; I would fix the call/reply path for your {label} before adding spend. Want the 3-point renewal snapshot?"
            return body, "binary_yes_no", "Renewal nudge offers decision support tied to visible profile performance."
        if kind == "curious_ask_due":
            body = f"{owner}, quick operator question for {business} in {place}: which service or item do customers ask for most this week? With {snapshot}, I can turn your answer into one Google post and one WhatsApp reply. Send me the item name?"
            return body, "open_ended", "Curiosity-led placeholder asks for one merchant input and promises an immediate reusable asset."

        body = f"{owner}, {snapshot} in {place}. For your {label}, my recommendation is to {action}. Want the ready-to-use draft?"
        return body, "binary_yes_no", "Generated trigger has no event facts, so the message uses verified profile performance plus a category-specific decision."

        if trigger.get("scope") == "customer":
            customer_name = customer.get("identity", {}).get("name", "there")
            if kind == "appointment_tomorrow":
                body = f"Hi {customer_name}, {business} here. A quick reminder about your appointment tomorrow: reply CONFIRM if you are all set, or RESCHEDULE and we will help with another time."
                return body, "binary_confirm_cancel", "Appointment reminder stays useful without inventing a service or time that was not provided."
            body = f"Hi {customer_name}, {business} here. We would love to help with your {topic.replace('customer ', '')} plan. Reply YES and we will suggest the simplest next step."
            return body, "binary_yes_no", "Customer follow-up uses the known trigger purpose and a low-friction opt-in without inventing history."

        owner = _merchant_name(merchant)
        identity = merchant.get("identity", {})
        city = identity.get("city")
        category_slug = merchant.get("category_slug", "")
        performance = merchant.get("performance", {})
        window = performance.get("window_days")
        views = performance.get("views")
        calls = performance.get("calls")
        profile_fact = ""
        if window and views is not None and calls is not None:
            profile_fact = f" In the last {window} days, your profile generated {views} views and {calls} calls."
        city_fact = f" for {business} in {city}" if city else f" for {business}"
        category_actions = {
            "dentists": "ask recent patients to book their next recall or treatment visit",
            "gyms": "send recent trial members a direct membership follow-up",
            "pharmacies": "prompt repeat customers about their next refill or availability check",
            "restaurants": "promote one menu or delivery choice for the next demand window",
            "salons": "ask recent clients to rebook a specific service into the next available slot",
        }
        action = category_actions.get(category_slug, "turn the signal into one clear customer-facing next step")
        if kind == "review_theme_emerged":
            action = "respond to the next review calmly, then fix the recurring issue before it costs another repeat customer"
        elif kind == "milestone_reached":
            action = f"use the next { _category_label(merchant) } customer interaction to cross the milestone, then share the proof in a short local post"
        elif kind == "dormant_with_vera":
            action = f"restart with one fresh { _category_label(merchant) } growth idea instead of reopening the old conversation"
        elif kind == "competitor_opened":
            action = "lead with your strongest differentiator instead of matching a competitor's discount"
        elif kind == "festival_upcoming":
            action = "prepare one category-relevant offer now, then schedule the actual push closer to the festival"
        elif kind == "renewal_due":
            action = "review the profile actions that create calls before making the next plan decision"
        elif kind == "perf_dip":
            action = f"{action} rather than adding a blanket discount"
        elif kind == "perf_spike":
            action = f"{action} while the current momentum is still fresh"
        body = f"{owner}, I’m preparing a short {topic} check-in{city_fact}.{profile_fact} For your {_category_label(merchant)} business, my recommendation is to {action}. Want the ready-to-use draft?"
        return body, "binary_yes_no", "Generated trigger has no event facts, so the message uses verified profile performance plus a category-specific decision."

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
        body = f"{owner}, since you asked about {topic}, for your {_category_label(merchant)} business I’d start with one clear offer, the exact audience, and a single reply CTA rather than a broad announcement. I can turn that into a ready-to-send 3-line draft now. Want it?"
        rationale = "Merchant showed active planning intent, so the message gives a concrete campaign decision and moves directly to execution."
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
        body = f"{owner}, {festival} is {days} days away. It is too early to launch a discount, but this is the right time to choose {offer_text} for your {_category_label(merchant)} business and set a clear booking/use window. Want the campaign draft to save for the launch week?"
        rationale = "Festival message makes a timing-aware decision: prepare the specific offer now, but avoid spending the promotion too early."
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

    def _compose_customer_lapsed_soft(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        days = payload.get("days_since_last_visit", "a while")
        focus = _humanize_token(payload.get("previous_focus", "routine"))
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0].get("title") if active_offer else None
        offer_line = f" {offer_text} is available this week." if offer_text else ""
        body = (
            f"Hi {customer_name}, {_merchant_name(merchant)} from {_business_name(merchant)} here. "
            f"It has been {days} days since your last visit for {focus}."
            f"{offer_line} Want me to hold a convenient slot for you this week?"
        )
        rationale = "Gentle winback names the prior visit timing and focus, then asks for one low-pressure next step."
        return body, "binary_yes_no", rationale

    def _compose_appointment_tomorrow(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        appointment = payload.get("appointment", {})
        service = appointment.get("service") or payload.get("service") or "appointment"
        time_label = appointment.get("time_label") or appointment.get("time") or payload.get("time_label") or "your scheduled time"
        location = appointment.get("location") or payload.get("location")
        location_line = f" at {location}" if location else ""
        body = (
            f"Hi {customer_name}, reminder from {_business_name(merchant)}: your {service} is tomorrow "
            f"at {time_label}{location_line}. Reply CONFIRM to keep it, or RESCHEDULE if you need another time."
        )
        rationale = "Appointment reminder uses the scheduled service and time with an explicit confirm-or-reschedule choice."
        return body, "binary_confirm_cancel", rationale

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
