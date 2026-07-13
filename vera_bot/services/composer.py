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


def _owner_name_raw(merchant: dict[str, Any]) -> str:
    """Return the raw owner name from profile, stripping leading 'Dr. ' if already present."""
    return (
        merchant.get("identity", {}).get("owner_first_name")
        or merchant.get("identity", {}).get("name", "there")
    )


def _merchant_name(merchant: dict[str, Any]) -> str:
    """Return the best addressable name for a merchant owner, without any prefix."""
    name = _owner_name_raw(merchant)
    # Strip existing prefix so callers that add their own prefix don't double it
    if name.startswith("Dr. "):
        name = name[4:]
    return name


def _dentist_prefix(merchant: dict[str, Any]) -> str:
    """Return 'Dr. <name>' only for dentist category, else just '<name>'."""
    name = _merchant_name(merchant)
    if merchant.get("category_slug") == "dentists":
        return f"Dr. {name}"
    return name


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


def _fmt_delta(value: Any) -> str:
    """Format a delta_pct float as a signed percentage string."""
    try:
        pct = float(value) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.0f}%"
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


def _perf_delta_summary(merchant: dict[str, Any]) -> str:
    """Return a human-readable 7-day trend line from merchant delta data."""
    perf = merchant.get("performance", {})
    delta = perf.get("delta_7d", {})
    views_pct = delta.get("views_pct")
    calls_pct = delta.get("calls_pct")
    parts = []
    if views_pct is not None:
        parts.append(f"views {_fmt_delta(views_pct)} vs last week")
    if calls_pct is not None:
        parts.append(f"calls {_fmt_delta(calls_pct)}")
    return ", ".join(parts) if parts else ""


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


def _category_cta(merchant: dict[str, Any], situation: str = "default") -> str:
    """Return a crisp, low-friction CTA tailored to the category and situation."""
    slug = merchant.get("category_slug", "")
    ctas = {
        "dentists": {
            "default": "Want the 3-line patient message?",
            "perf": "Want a ready-to-send patient recall message?",
            "review": "Want a calm reply template for your GBP?",
            "competitor": "Want a counter-positioning draft for your GBP?",
            "festival": "Want the campaign draft saved for launch week?",
        },
        "gyms": {
            "default": "Want the ready-to-use WhatsApp draft?",
            "perf": "Want a member re-engagement message now?",
            "review": "Want a response template plus one operational fix?",
            "competitor": "Want a positioning statement for your studio?",
            "festival": "Want the offer + booking window written up?",
        },
        "pharmacies": {
            "default": "Reply YES and I'll send the exact message.",
            "perf": "Want the 3-line refill-push draft?",
            "review": "Want a customer-facing reply template?",
            "competitor": "Want a trust-positioning note for your regulars?",
            "festival": "Want the seasonal health-offer draft?",
        },
        "restaurants": {
            "default": "Want the 3-line operator message?",
            "perf": "Want a delivery-window promotion draft?",
            "review": "Want a public reply template + one fix list?",
            "competitor": "Want a positioning line for your regular crowd?",
            "festival": "Want the festive offer + timing draft?",
        },
        "salons": {
            "default": "Reply YES and I'll send the slot-fill message.",
            "perf": "Want the rebooking WhatsApp draft now?",
            "review": "Want a friendly response template for that review?",
            "competitor": "Want a loyalty message for your regulars?",
            "festival": "Want the festive bridal/styling offer draft?",
        },
    }
    return ctas.get(slug, {}).get(situation, "Want the ready-to-use draft?")


def _signals_line(merchant: dict[str, Any]) -> str:
    """Convert merchant signals array into a human-readable phrase."""
    signals = merchant.get("signals", [])
    signal_map = {
        "high_volume": "high-volume profile",
        "stable_growth": "stable growth trend",
        "high_retention": "strong repeat-customer base",
        "engaged_in_last_24h": "recently engaged with Vera",
        "active_planning": "actively planning",
        "boutique_segment": "boutique positioning",
        "low_ctr": "low click-through rate",
        "rising_views": "rising visibility",
    }
    readable = [signal_map.get(s, s.replace("_", " ")) for s in signals[:2]]
    return f"({', '.join(readable)})" if readable else ""


def _customer_aggregate_line(merchant: dict[str, Any]) -> str:
    """Return a one-phrase summary of customer aggregate if useful."""
    agg = merchant.get("customer_aggregate", {})
    total = agg.get("total_unique_ytd")
    repeat_pct = agg.get("repeat_customer_pct")
    if total and repeat_pct:
        return f"{_fmt_number(total)} customers YTD, {int(repeat_pct * 100)}% repeat"
    if total:
        return f"{_fmt_number(total)} customers YTD"
    return ""


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

        if trigger.get("scope") == "customer":
            customer_name = customer.get("identity", {}).get("name", "there")
            business = _business_name(merchant)
            if kind == "appointment_tomorrow":
                body = (
                    f"Hi {customer_name}, {business} here. Reminder for your appointment tomorrow: "
                    f"reply CONFIRM if you are set, or RESCHEDULE and we will help with another time."
                )
                return body, "binary_confirm_cancel", "Appointment reminder stays useful without inventing service or time details."
            if kind == "recall_due":
                body = (
                    f"Hi {customer_name}, {business} here. You are due for a follow-up. "
                    f"Reply 1 for a slot this week, 2 for next week, or NO if you want us to pause reminders."
                )
                return body, "multi_choice_slot", "Recall reminder gives a clear booking choice without inventing a service."
            if kind == "customer_lapsed_soft":
                body = (
                    f"Hi {customer_name}, {business} here. It has been a while since your last visit. "
                    f"Want us to hold an easy comeback slot this week? Reply YES and we will suggest two options."
                )
                return body, "binary_yes_no", "Gentle lapsed-customer nudge keeps the ask low pressure and replyable."
            if kind == "chronic_refill_due":
                body = (
                    f"Hi {customer_name}, {business} here. Quick refill reminder: reply CONFIRM if you want us to "
                    f"check availability and pickup or delivery, or SKIP if you are stocked for now."
                )
                return body, "binary_confirm_cancel", "Refill reminder is operational and consent-led even when the exact molecule is absent."
            if kind == "trial_followup":
                body = (
                    f"Hi {customer_name}, {business} here. If the trial felt useful, reply YES and we will hold "
                    f"the next beginner-friendly slot; reply LATER if this week is packed."
                )
                return body, "binary_yes_no", "Trial follow-up asks for one simple commitment without inventing attendance details."
            body = (
                f"Hi {customer_name}, {business} here. Quick check-in: reply YES if you want the simplest "
                f"next step from us, or NO and we will leave it here."
            )
            return body, "binary_yes_no", "Customer follow-up uses a low-friction opt-in without inventing history."

        # Merchant-facing placeholder messages
        owner = _dentist_prefix(merchant)
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        action = _category_action(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else None
        business = _business_name(merchant)
        delta_line = _perf_delta_summary(merchant)
        cust_line = _customer_aggregate_line(merchant)
        signals_line = _signals_line(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))

        if kind == "research_digest":
            body = (
                f"{owner}, a category update worth acting on is better turned into patient content than just read. "
                f"{business} in {place}: {snapshot}{' — ' + delta_line if delta_line else ''}. "
                f"Want a WhatsApp plus Google post that connects the finding to one bookable next step?"
            )
            return body, "binary_yes_no", "Placeholder research digest becomes a concrete content asset using real profile performance."

        if kind == "perf_dip":
            offer_line = f" Push '{offer_text}' with one sharp line instead." if offer_text else f" For your {label}, {action}."
            body = (
                f"{owner}, {business} in {place}: {views} views and {calls} calls last 30 days "
                f"({ctr} CTR{', ' + delta_line if delta_line else ''}). "
                f"Before cutting price: {offer_line} Want the exact 3-line recovery message?"
            )
            return body, "binary_yes_no", "Performance dip message makes a recovery decision anchored to current profile metrics without adding spend."

        if kind == "perf_spike":
            offer_line = f" Ride the wave with '{offer_text}'." if offer_text else f" For your {label}, {action}."
            body = (
                f"{owner}, {business} in {place} is up: {views} views, {calls} calls, {ctr} CTR last 30 days"
                f"{' — ' + delta_line if delta_line else ''}. Capture this demand now."
                f"{offer_line} Want a same-day post and reply script?"
            )
            return body, "binary_yes_no", "Performance spike message pushes immediate follow-through while momentum is active."

        if kind == "milestone_reached":
            # Use performance data to frame the milestone moment
            delta = merchant.get("performance", {}).get("delta_7d", {})
            views_pct = delta.get("views_pct")
            calls_pct = delta.get("calls_pct")
            trend_note = ""
            if views_pct is not None and views_pct > 0:
                trend_note = f" Views are up {int(views_pct * 100)}% this week."
            elif calls_pct is not None and calls_pct > 0:
                trend_note = f" Calls are up {int(calls_pct * 100)}% this week."
            agg_desc = f"{cust_line}" if cust_line else f"{views} views and {calls} calls this month"
            body = (
                f"{owner}, {business} in {place} just hit a milestone worth making visible: {agg_desc}.{trend_note} "
                f"The smartest next step is not another offer — it is social proof. "
                f"One short local post plus one ready-to-send customer reply script. "
                f"Want both drafts now?"
            )
            return body, "binary_yes_no", "Milestone message uses real performance data to frame a 'prove it publicly' decision and drives the next concrete action."

        if kind == "dormant_with_vera":
            body = (
                f"{owner}, restarting with a generic idea would waste the moment. "
                f"{business} in {place}: {snapshot}{' (' + delta_line + ')' if delta_line else ''}. "
                f"The clean restart for your {label} is to {action}. Want the ready-to-use draft?"
            )
            return body, "binary_yes_no", "Dormant re-entry avoids stale follow-up and uses current profile numbers to pick one restart action."

        if kind == "review_theme_emerged":
            body = (
                f"{owner}, a review pattern is building at {business} in {place}. "
                f"{snapshot}. "
                f"Before replying publicly, {_category_review_fix(merchant)} — that is the fix that protects your next 10 customers. "
                f"Then use one calm reply template to close the loop. "
                f"{_category_cta(merchant, 'review')}"
            )
            return body, "binary_yes_no", "Review-theme placeholder prioritizes operational fix first, then public reply, using real performance data to frame urgency."

        if kind == "competitor_opened":
            offer_line = f" Keep '{offer_text}' as the hook." if offer_text else ""
            body = (
                f"{owner}, if a competitor has opened near {place}, do not race on price first. "
                f"{snapshot} at {business}: lead with a sharper positioning line and one reply CTA.{offer_line} "
                f"{_category_cta(merchant, 'competitor')}"
            )
            return body, "binary_yes_no", "Competitor message chooses positioning over price matching and ties it to the merchant profile."

        if kind == "festival_upcoming":
            offer_line = f"'{offer_text}'" if offer_text else f"one {label}-specific offer"
            body = (
                f"{owner}, for the upcoming festival at {business} in {place}: prepare now but hold the blast until the buying window. "
                f"{snapshot} — pick {offer_line}, define the booking/use window, and save the launch draft. "
                f"{_category_cta(merchant, 'festival')}"
            )
            return body, "binary_yes_no", "Festival placeholder makes a timing-aware campaign decision without inventing the festival name."

        if kind == "renewal_due":
            days_left = merchant.get("subscription", {}).get("days_remaining")
            days_str = f"in {days_left} days" if days_left else "soon"
            body = (
                f"{owner}, your plan at {business} renews {days_str}. Before deciding, check whether it is earning enough action: "
                f"{snapshot} in {place}. I would fix the call/reply path for your {label} first. "
                f"Want a 3-point renewal snapshot?"
            )
            return body, "binary_yes_no", "Renewal nudge offers decision support tied to visible profile performance."

        if kind == "curious_ask_due":
            body = (
                f"{owner}, quick operator question for {business} in {place}: which service or item do customers ask for most this week? "
                f"With {snapshot}, I can turn your answer into one Google post and one WhatsApp reply. "
                f"Send me the item name?"
            )
            return body, "open_ended", "Curiosity-led placeholder asks for one merchant input and promises an immediate reusable asset."

        # Generic placeholder fallback — category + performance aware
        body = (
            f"{owner}, {business} in {place}: {snapshot}{' (' + delta_line + ')' if delta_line else ''}. "
            f"For your {label}, the sharpest next step is to {action}. "
            f"Want the ready-to-use draft?"
        )
        return body, "binary_yes_no", "Generated trigger has no event facts, so the message uses verified profile performance plus a category-specific decision."

    def _compose_research_digest(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("top_item_id"))
        owner = _dentist_prefix(merchant)
        if digest:
            trial_n = digest.get("trial_n")
            segment = digest.get("patient_segment", "patient cohort").replace("_", " ")
            title = digest.get("title", "")
            summary = digest.get("summary", title).rstrip(".")
            source = digest.get("source", "this week's digest")
            body = (
                f"{owner}, {source} has one useful item: {title}. "
                f"{trial_n:,}-patient data on {segment} showed {summary.split('. ')[0]}. "
                f"Want me to draft a patient-friendly WhatsApp or a short Google post from it?"
                if isinstance(trial_n, int)
                else f"{owner}, this week's dentistry digest has one useful item: {digest.get('title', '')}. "
                f"Worth a quick look. Want me to turn it into a patient WhatsApp or a short Google post?"
            )
            rationale = "Research-led outreach using category digest evidence and a low-friction follow-up asset."
            return body, "open_ended", rationale
        return self._compose_generic(resolved)

    def _compose_regulation_change(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("top_item_id"))
        owner = _dentist_prefix(merchant)
        deadline = trigger.get("payload", {}).get("deadline_iso", "")
        date_text = deadline[:10] if deadline else "the deadline"
        title = digest.get("title") if digest else "a compliance update"
        action = digest.get("actionable") if digest else "review your SOPs"
        body = f"{owner}, quick compliance heads-up: {title}, effective by {date_text}. Suggested next step: {action}. Want a 3-point checklist for your clinic?"
        rationale = "Urgent merchant-facing compliance nudge with concrete next-step framing."
        return body, "binary_yes_no", rationale

    def _compose_cde_opportunity(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        trigger = resolved.trigger
        digest = _digest_by_id(resolved.category, trigger.get("payload", {}).get("digest_item_id"))
        owner = _dentist_prefix(merchant)
        credits = trigger.get("payload", {}).get("credits")
        fee = trigger.get("payload", {}).get("fee")
        title = digest.get("title") if digest else "a relevant CDE session"
        body = f"{owner}, one relevant CDE option this week: {title}. {credits} credits, {fee}. Want the session summary and a reminder draft?"
        rationale = "Professional-development message anchored to category-specific continuing education."
        return body, "binary_yes_no", rationale

    def _compose_competitor_opened(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        competitor = payload.get("competitor_name", "a nearby competitor")
        distance = payload.get("distance_km", "?")
        their_offer = payload.get("their_offer", "a low-entry offer")
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else None
        offer_line = f" Your active offer '{offer_text}' is the hook — lead with it." if offer_text else ""
        body = (
            f"{owner}, heads-up: {competitor} opened about {distance} km from {place} with {their_offer}. "
            f"You have credibility and local history on your side — do not race on price. "
            f"{snapshot} at your {label}.{offer_line} "
            f"{_category_cta(merchant, 'competitor')}"
        )
        rationale = "Competitive alert that turns proximity threat into a messaging opportunity using merchant profile data."
        return body, "binary_yes_no", rationale

    def _compose_perf_dip(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})\

        owner = _dentist_prefix(merchant)
        metric = payload.get("metric", "performance")
        delta_pct = payload.get("delta_pct", 0)
        delta_str = f"{abs(int(float(delta_pct) * 100))}%"
        window = payload.get("window", "7d")
        baseline = payload.get("vs_baseline")
        baseline_str = f" (baseline: {baseline})" if baseline else ""
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else None
        place = _place_text(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        recovery_line = (
            f"push '{offer_text}' with one sharp headline" if offer_text
            else _category_action(merchant)
        )
        body = (
            f"{owner}, your {metric} dipped {delta_str} in the last {window}{baseline_str}. "
            f"Profile in {place}: {views} views, {calls} calls, {ctr} CTR. "
            f"Rather than a blanket discount, {recovery_line}. "
            f"Want me to draft it now?"
        )
        rationale = "Performance dip message uses merchant metrics and a concrete recovery asset."
        return body, "binary_yes_no", rationale

    def _compose_perf_spike(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        metric = payload.get("metric", "performance")
        delta_pct = payload.get("delta_pct", 0)
        delta_str = f"{int(float(delta_pct) * 100)}%"
        driver = payload.get("likely_driver", "")
        driver_line = f" Likely driven by {driver.replace('_', ' ')}." if driver else ""
        place = _place_text(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        active_offer = _active_offers(merchant)
        follow_up = (
            f"push '{active_offer[0]['title']}' while the signal is hot" if active_offer
            else _category_action(merchant)
        )
        body = (
            f"{owner}, your {metric} jumped {delta_str} this week in {place}.{driver_line} "
            f"Profile: {views} views, {calls} calls, {ctr} CTR. "
            f"Capture the demand now: {follow_up}. "
            f"Want a same-day post and reply script?"
        )
        rationale = "Positive performance nudge encourages fast follow-through while momentum is fresh."
        return body, "open_ended", rationale

    def _compose_milestone_reached(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        metric = payload.get("metric", "")
        value_now = payload.get("value_now")
        milestone_value = payload.get("milestone_value")
        place = _place_text(merchant)
        business = _business_name(merchant)
        label = _category_label(merchant)
        cust_agg = _customer_aggregate_line(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))

        # If we have real milestone values, use them
        if value_now is not None and milestone_value is not None and metric:
            body = (
                f"{owner}, {business} is at {_fmt_number(value_now)} {metric.replace('_', ' ')} and closing in on {_fmt_number(milestone_value)}. "
                f"Profile in {place}: {views} views and {calls} calls this month. "
                f"Turn this into proof: a short local post plus one customer reply script. "
                f"Want both drafts?"
            )
        else:
            # Fallback using customer aggregate
            agg_line = f"You have served {cust_agg}" if cust_agg else f"Your {label} in {place} is hitting a milestone"
            body = (
                f"{owner}, {agg_line}. "
                f"Profile: {views} views and {calls} calls this month at {business}. "
                f"Turn this win into social proof — one local post and one reply script. "
                f"Want both ready-to-use drafts?"
            )
        rationale = "Milestone framing uses real performance data to build proof and drive the next concrete action."
        return body, "binary_yes_no", rationale

    def _compose_active_planning_intent(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        topic = str(payload.get("intent_topic", "this plan")).replace("_", " ")
        label = _category_label(merchant)
        place = _place_text(merchant)
        business = _business_name(merchant)
        # Include last merchant message if available
        last_msg = payload.get("merchant_last_message", "")
        # Also check conversation_history
        history = merchant.get("conversation_history", [])
        merchant_msgs = [h for h in history if h.get("from") == "merchant"]
        if not last_msg and merchant_msgs:
            last_msg = merchant_msgs[-1].get("body", "")
        active_offer = _active_offers(merchant)
        offer_line = f" Build it around your active offer '{active_offer[0]['title']}'." if active_offer else ""
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        body = (
            f"{owner}, since you asked about {topic}, here is the structure for {business} in {place}: "
            f"one specific offer with a clear audience, a tight booking window, and a single reply CTA — "
            f"not a broad announcement.{offer_line} "
            f"Your profile is already pulling {views} views and {calls} calls this month. "
            f"I can turn that into a ready-to-send 3-line draft now. Want it?"
        )
        rationale = "Merchant showed active planning intent, so the message gives a concrete campaign decision using their real performance data and moves directly to execution."
        return body, "binary_yes_no", rationale

    def _compose_seasonal_perf_dip(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        delta_pct = abs(int(float(payload.get("delta_pct", 0)) * 100))
        metric = payload.get("metric", "views")
        place = _place_text(merchant)
        perf = merchant.get("performance", {})
        calls = _fmt_number(perf.get("calls", 0))
        label = _category_label(merchant)
        body = (
            f"{owner}, your {metric} are down {delta_pct}% this week in {place}, but this looks seasonal rather than broken. "
            f"Your profile still produced {calls} calls this month. "
            f"Better move: protect retention now and save heavy spend for the stronger window. "
            f"Want a retention message for your current {label} base?"
        )
        rationale = "Seasonal framing reduces panic and shifts toward a smarter next step using real call data."
        return body, "binary_yes_no", rationale

    def _compose_review_theme_emerged(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        theme = str(payload.get("theme", "service issue")).replace("_", " ")
        count = payload.get("occurrences_30d", 0)
        business = _business_name(merchant)
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        fix_approach = _category_review_fix(merchant)
        body = (
            f"{owner}, {count} recent reviews at {business} in {place} point to {theme}. "
            f"{snapshot}. "
            f"Fixing the issue matters, but so does the public reply — first {fix_approach}. "
            f"Want me to draft a calm response template you can reuse?"
        )
        rationale = "Review-pattern outreach pairs specific operational signal with a category-specific fix and a ready-to-use response artifact."
        return body, "binary_yes_no", rationale

    def _compose_renewal_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        plan = payload.get("plan", "plan")
        days_remaining = payload.get("days_remaining")
        # Fallback to subscription data if not in payload
        if days_remaining is None:
            days_remaining = merchant.get("subscription", {}).get("days_remaining")
        days_str = f"in {days_remaining} days" if days_remaining else "soon"
        amount = payload.get("renewal_amount")
        amount_str = f" at {_money(amount)}" if amount else ""
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        label = _category_label(merchant)
        body = (
            f"{owner}, your {plan} renewal is due {days_str}{amount_str}. "
            f"Before deciding, check whether it is earning enough action: {snapshot} in {place}. "
            f"For your {label}, I would fix the call/reply path before adding spend. "
            f"Want the 3-point renewal snapshot?"
        )
        rationale = "Renewal nudge avoids pressure and offers a decision-support summary tied to real profile performance."
        return body, "binary_yes_no", rationale

    def _compose_curious_ask_due(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        owner = _dentist_prefix(merchant)
        business = _business_name(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        body = (
            f"{owner}, quick operator question for {business} in {place}: "
            f"which service or item do customers ask for most this week? "
            f"With {snapshot}, I can turn your answer into a Google post plus a short WhatsApp reply you can reuse. "
            f"Send me the item name?"
        )
        rationale = "Curiosity-led message uses the asking-the-merchant lever and promises immediate reciprocation."
        return body, "open_ended", rationale

    def _compose_festival_upcoming(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        festival = payload.get("festival", "the upcoming festival")
        days = payload.get("days_until")
        days_str = f"{days} days" if days else "a few weeks"
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "one specific festive offer"
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        body = (
            f"{owner}, {festival} is {days_str} away. "
            f"It is too early to launch a discount, but this is the right time to choose '{offer_text}' for your {label} "
            f"and set a clear booking/use window. "
            f"{snapshot} in {place}. "
            f"{_category_cta(merchant, 'festival')}"
        )
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
            f"For this slot, I'd back delivery over dine-in and push '{offer_text}'. "
            f"Want a 3-line match-night banner draft?"
        )
        rationale = "Trigger-aware restaurant recommendation adds judgment instead of merely restating the event."
        return body, "binary_yes_no", rationale

    def _compose_winback_eligible(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        days = payload.get("days_since_expiry", "")
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        body = (
            f"{owner}, it has been {days} days since your plan expired and performance has softened since then. "
            f"{snapshot} in {place}. "
            f"I can draft a sharp reactivation note for your {label} that focuses on one concrete business win — not a hard sell. "
            f"Want it?"
        )
        rationale = "Winback framing acknowledges lapse while using real performance data to keep the re-entry ask credible."
        return body, "binary_yes_no", rationale

    def _compose_gbp_unverified(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        uplift = int(float(payload.get("estimated_uplift_pct", 0)) * 100)
        business = _business_name(merchant)
        place = _place_text(merchant)
        label = _category_label(merchant)
        snapshot = _performance_snapshot(merchant)
        body = (
            f"{owner}, your Google profile for {business} in {place} is still unverified. "
            f"That alone may be costing you roughly {uplift}% visibility for your {label}. "
            f"{snapshot}. "
            f"Want the fastest verification path broken into 3 steps?"
        )
        rationale = "GBP verification message uses a concrete upside and real performance context, then offers a simple checklist."
        return body, "binary_yes_no", rationale

    def _compose_supply_alert(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        batches = ", ".join(payload.get("affected_batches", []))
        molecule = payload.get("molecule", "the affected medication")
        manufacturer = payload.get("manufacturer", "the manufacturer")
        body = (
            f"{owner}, urgent: {molecule} batches {batches} from {manufacturer} were flagged. "
            f"Want me to draft the customer note and replacement-pickup workflow?"
        )
        rationale = "Compliance-sensitive pharmacy alert grounded in precise batch data."
        return body, "binary_yes_no", rationale

    def _compose_category_seasonal(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _merchant_name(merchant)
        trend = ", ".join(payload.get("trends", [])[:3])
        place = _place_text(merchant)
        business = _business_name(merchant)
        snapshot = _performance_snapshot(merchant)
        body = (
            f"{owner}, summer demand is shifting for {business} in {place}: {trend}. "
            f"{snapshot}. "
            f"Worth adjusting what stays front-and-center this week. "
            f"Want a shelf/display suggestion list in one message?"
        )
        rationale = "Seasonal category digest translated into a merchant-operational action with real performance context."
        return body, "binary_yes_no", rationale

    def _compose_dormant_with_vera(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        days = payload.get("days_since_last_merchant_message", "some")
        last_topic = payload.get("last_topic", "")
        label = _category_label(merchant)
        place = _place_text(merchant)
        business = _business_name(merchant)
        snapshot = _performance_snapshot(merchant)
        action = _category_action(merchant)
        topic_line = f"Rather than revisit {last_topic.replace('_', ' ')}, " if last_topic else ""
        body = (
            f"{owner}, it has been {days} days since we last spoke. "
            f"{topic_line}here is what is happening at {business} in {place}: {snapshot}. "
            f"The clean restart for your {label}: {action}. "
            f"Want the ready-to-use draft?"
        )
        rationale = "Dormancy re-entry deliberately avoids stale follow-up and uses current profile data to recommend one sharp restart action."
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
        body = f"Hi {customer_name}, {_business_name(merchant)} here. It's been a while since your last visit and your {service_due} is due. Apke liye {slot_text} ready hain. {offer_text}. Reply 1 or 2, or share a better time."
        rationale = "Customer recall message uses timing, slot specificity, and the merchant's active offer."
        return body, "multi_choice_slot", rationale

    def _compose_customer_lapsed_hard(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        customer = resolved.customer or {}
        payload = resolved.trigger.get("payload", {})
        customer_name = customer.get("identity", {}).get("name", "there")
        focus = payload.get("previous_focus", "your earlier goal").replace("_", " ")
        days = payload.get("days_since_last_visit", "a while")
        body = (
            f"Hi {customer_name}, {_merchant_name(merchant)} from {_business_name(merchant)} here. "
            f"It has been about {days} days. No pressure, but we have something that fits your earlier {focus} goal. "
            f"Want me to hold a trial slot for you this week?"
        )
        rationale = "Winback message uses warm, low-shame framing and refers back to the customer's prior goal."
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
        body = f"Hi {customer_name}, thanks again for trying {_business_name(merchant)}. If you want to continue, I can hold {slot} for the next session. Reply YES and I'll block it."
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
        """Improved generic fallback that still uses available profile data."""
        merchant = resolved.merchant
        trigger = resolved.trigger
        owner = _dentist_prefix(merchant)
        label = _category_label(merchant)
        place = _place_text(merchant)
        business = _business_name(merchant)
        snapshot = _performance_snapshot(merchant)
        action = _category_action(merchant)
        kind = trigger.get("kind", "")
        kind_readable = kind.replace("_", " ") if kind else "this week's signal"
        delta_line = _perf_delta_summary(merchant)
        active_offer = _active_offers(merchant)
        offer_line = f" Your active offer '{active_offer[0]['title']}' is ready to use as the hook." if active_offer else ""
        body = (
            f"{owner}, {business} in {place}: {snapshot}"
            f"{' — ' + delta_line if delta_line else ''}. "
            f"Based on {kind_readable}, for your {label} the sharpest move is to {action}."
            f"{offer_line} "
            f"Want the ready-to-use draft?"
        )
        rationale = "Fallback grounded in real profile performance, category voice, and a specific recommended action."
        return body, "binary_yes_no", rationale
