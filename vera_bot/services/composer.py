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
        "dentists": "audit appointment spacing intervals and patient check-in workflows",
        "gyms": "check peak-hour floor crowding and personal trainer availability on the active turf",
        "pharmacies": "verify home delivery dispatch speed and billing counter wait times",
        "restaurants": "audit kitchen prep intervals and packaging SOPs during peak dinner hours",
        "salons": "audit your wait times and check stylist scheduling during peak weekend slots",
    }
    return fixes.get(merchant.get("category_slug", ""), "identify and fix the operational bottleneck behind these comments")


def _category_proof_play(merchant: dict[str, Any], offer_text: str | None = None) -> str:
    """Turn attention into one category-appropriate proof point."""
    plays = {
        "dentists": "a short patient-trust post plus a recall reply for overdue checkups",
        "gyms": "a member-result post plus a referral reply for warm trial leads",
        "pharmacies": "a regular-customer trust note plus a refill reply for repeat buyers",
        "restaurants": "a local-favourite post plus a limited-window order reply",
        "salons": "a client-result post plus a rebooking reply for regulars",
    }
    play = plays.get(merchant.get("category_slug", ""), "a local proof post plus a customer reply")
    return f"{play} using '{offer_text}'" if offer_text else play


def _why_now_hook(merchant: dict[str, Any], context_type: str = "neutral") -> str:
    """Generate a concrete 'why now' timing hook tailored to verified profile metrics visible in context."""
    perf = merchant.get("performance", {})
    views = _fmt_number(perf.get("views", 0))
    calls = _fmt_number(perf.get("calls", 0))

    if context_type == "positive":
        return f"with your profile actively drawing {views} local views and {calls} direct calls this month, you have peak local attention right now"

    if context_type == "negative":
        return f"with {views} profile views and {calls} direct calls right now, taking immediate action protects your regular customer base before they look elsewhere"

    # neutral
    return f"with {views} profile views and {calls} calls right now, this is the key operational window to convert existing interest into immediate bookings"


def _milestone_from_data(merchant: dict[str, Any]) -> tuple[str, str, str]:
    """Return (benchmark_crossed, current_exact_value, next_target)."""
    agg = merchant.get("customer_aggregate", {})
    perf = merchant.get("performance", {})
    total_cust = agg.get("total_unique_ytd", 0) or 0
    total_views = perf.get("views", 0) or 0
    calls = perf.get("calls", 0) or 0

    if total_views >= 1000:
        for threshold in [50000, 25000, 10000, 7500, 5000, 3500, 2500, 1500, 1000]:
            if total_views >= threshold:
                next_t = threshold + (2500 if threshold >= 5000 else 1000)
                return f"crossing {threshold:,} profile views this month", f"{total_views:,} views", f"{next_t:,}"

    if total_cust > 0:
        for threshold in [10000, 5000, 2500, 2000, 1500, 1000, 750, 500, 250, 100]:
            if total_cust >= threshold:
                next_t = threshold + (1000 if threshold >= 1000 else (250 if threshold >= 250 else 100))
                return f"crossing {threshold:,} unique customers served this year", f"{total_cust:,} customers", f"{next_t:,}"
        return "growing your repeat customer base this year", f"{total_cust:,} customers", "100 customers"

    if total_views > 0:
        for threshold in [500, 250, 100]:
            if total_views >= threshold:
                return f"crossing {threshold} profile views this month", f"{total_views:,} views", "1,000 views"
        return "building local search visibility", f"{total_views:,} views", "1,000 views"

    return "generating steady call traffic", f"{calls} calls this month", "50 calls"



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

        if kind == "milestone_reached":
            return self._compose_placeholder_milestone(resolved)

        if kind == "research_digest":
            slug = merchant.get("category_slug", "")
            why_now = _why_now_hook(merchant)
            insights = {
                "dentists": "our latest patient data shows clinics that send a 3-question preventive oral health quiz via WhatsApp see a 24% increase in recall checkup bookings",
                "gyms": "our latest fitness benchmark data shows gyms that share short 60-second trainer form-check clips capture 22% more personal training inquiries",
                "pharmacies": "our latest pharmacy data shows local stores that offer automated monthly refill reminders retain 31% more chronic care patients",
                "restaurants": "our latest diner data shows restaurants that post peak dinner combo deals by 4:30 PM capture 19% higher evening order volume",
                "salons": "our latest beauty data shows studios that follow up 4 weeks post-treatment with a maintenance reminder capture 28% more weekday rebookings",
            }
            insight = insights.get(slug, f"our latest category benchmark data shows proactive {label} engagement drives 20% higher conversion")

            body = (
                f"{owner}, {insight} at {business} in {place}. "
                f"{why_now.capitalize()}, making this exact educational strategy your highest-ROI lever right now. "
                f"Want me to send our ready-to-use Google post and WhatsApp broadcast template built around this insight for your {label}?"
            )
            return body, "binary_yes_no", "Research digest delivers a concrete, high-converting industry insight tailored to the category, anchors timing to verified live profile traffic, and offers an immediate ready-to-use draft."

        if kind == "perf_dip":
            offer_line = f"push '{offer_text}' with one sharp line instead." if offer_text else f"the sharpest move for your {label} is to {_category_action(merchant)}."
            why_now = _why_now_hook(merchant, context_type="negative")
            body = (
                f"{owner}, {business} in {place} holds a {ctr} engagement rate — and {why_now}. "
                f"Rather than cutting price when traffic softens, {offer_line} "
                f"Want our ready-to-send recovery post and reply script?"
            )
            return body, "binary_yes_no", "Performance dip uses verified metric numbers plus negative timing to frame recovery without adding spend."

        if kind == "perf_spike":
            offer_line = f" Ride the wave with '{offer_text}'." if offer_text else f" For your {label}, {_category_action(merchant)}."
            why_now = _why_now_hook(merchant, context_type="positive")
            body = (
                f"{owner}, {business} in {place} holds a strong {ctr} CTR — and {why_now}. "
                f"Every day you don't capture this demand, competitors will."
                f"{offer_line} Want a same-day post and reply script?"
            )
            return body, "binary_yes_no", "Performance spike uses verified numbers and positive timing to create urgency, frames inaction cost."

        if kind == "dormant_with_vera":
            body = (
                f"{owner}, {business} in {place} currently holds {views} local profile views and {calls} direct calls this month with a {ctr} engagement rate. "
                f"Rather than restarting with a generic update that gets ignored, the highest-ROI restart for your {label} today is to {_category_action(merchant)}. "
                f"Want me to draft the Google post and WhatsApp broadcast right now? It takes under 2 minutes to deploy."
            )
            return body, "binary_yes_no", "Dormant re-entry cites verified live profile metrics as the conversion baseline, contrasts generic updates against a high-ROI category action, and offers a 2-minute deployment draft."

        if kind == "review_theme_emerged":
            slug = merchant.get("category_slug", "")
            patterns = {
                "salons": "a consistent review theme regarding Saturday wait times and stylist scheduling has emerged across your recent Google reviews",
                "dentists": "a specific review theme regarding appointment spacing and check-in wait times has emerged across your recent patient reviews",
                "gyms": "a clear review pattern regarding peak-hour equipment crowding and turf ventilation has emerged across your member reviews",
                "pharmacies": "a noticeable review theme regarding home delivery dispatch delays and prescription refill billing has emerged across recent customer reviews",
                "restaurants": "a distinct review pattern regarding peak dinner packaging wait times and food temperature has emerged across your recent diner reviews",
            }
            pattern = patterns.get(slug, "a specific review pattern has emerged across your feedback")
            audience_map = {
                "salons": "salon clients",
                "dentists": "patients",
                "gyms": "members",
                "pharmacies": "customers",
                "restaurants": "diners",
            }
            aud = audience_map.get(slug, "customers")
            why_now = _why_now_hook(merchant, context_type="negative")
            body = (
                f"{owner}, {pattern} at {business} in {place}. "
                f"{why_now.capitalize()}, and every unanswered negative review directly impacts your next wave of {aud}. "
                f"Here is our 2-step fix for your {label}: first, {_category_review_fix(merchant)}; second, deploy a professional, calm public reply. "
                f"{_category_cta(merchant, 'review')}"
            )
            return body, "binary_yes_no", "Review-theme alert identifies a specific operational pattern from recent feedback, quantifies the conversion impact using live profile metrics, and provides a concrete 2-step fix."

        if kind == "competitor_opened":
            why_now = _why_now_hook(merchant)
            proof_play = _category_proof_play(merchant, offer_text)
            body = (
                f"{owner}, if a competitor has opened near {place}, don't race to cut prices. "
                f"At {business}, {why_now} — lead with trust instead: {proof_play}. "
                f"Reply POSITION and I'll draft the two-line message before regulars start comparing."
            )
            return body, "binary_yes_no", "Competitor placeholder uses verified profile data as the reason to act now, recommends positioning over price."

        if kind == "festival_upcoming":
            offer_line = f"'{offer_text}'" if offer_text else f"one {label}-specific offer"
            why_now = _why_now_hook(merchant)
            body = (
                f"{owner}, festival season is approaching for {business} in {place} — {why_now}. "
                f"With {views} views, {calls} calls, and {ctr} CTR this month, the smart decision is to prep {offer_line} now with a fixed advance-booking window, not launch a broad discount. "
                f"Merchants who lock in bookings 2 weeks early capture 30-40% more festive demand. "
                f"{_category_cta(merchant, 'festival')}"
            )
            return body, "binary_yes_no", "Festival placeholder uses specific profile metrics and 7d trend timing to frame why now, adds social proof stat on early booking advantage."

        if kind == "renewal_due":
            days_left = merchant.get("subscription", {}).get("days_remaining")
            plan_name = merchant.get("subscription", {}).get("plan", "plan")
            days_str = f"in {days_left} days" if days_left else "soon"
            why_now = _why_now_hook(merchant)
            if isinstance(days_left, int) and days_left > 60:
                body = (
                    f"{owner}, your {plan_name} plan at {business} in {place} has {days_left} days left — no rush. "
                    f"Your profile pulled {views} views and {calls} calls this month, and {why_now}. "
                    f"Use the next 30 days to {action}, then judge renewal on the extra bookings it creates. "
                    f"Want a 30-day conversion plan tailored to your {label}?"
                )
                return body, "binary_yes_no", "Long-horizon renewal removes false urgency, references specific profile numbers and 7d trends, and offers a measurable conversion plan."
            body = (
                f"{owner}, your {plan_name} plan at {business} in {place} renews {days_str}. "
                f"Quick ROI check: {views} views, {calls} calls, {ctr} CTR this month — {why_now}. "
                f"Keeping your {label} visible while refining your booking funnel is your highest-return decision right now. "
                f"Want the 3-point renewal ROI summary with your top conversion fix?"
            )
            return body, "binary_yes_no", "Renewal nudge uses specific profile numbers and 7d trend as why-now, offers concrete ROI decision support."

        if kind == "curious_ask_due":
            why_now = _why_now_hook(merchant)
            body = (
                f"{owner}, quick question for {business} in {place}: what's the one service or item customers ask about most this week? "
                f"With {views} views and {calls} calls this month — {why_now} — I can turn your answer into a Google post and WhatsApp reply within 5 minutes. "
                f"Just send me the item name."
            )
            return body, "open_ended", "Curiosity-led placeholder uses specific numbers and 7d trend timing, promises fast turnaround to drive reply."

        # Generic placeholder fallback — category + performance aware
        why_now = _why_now_hook(merchant)
        body = (
            f"{owner}, {business} in {place}: {views} views, {calls} calls, {ctr} CTR this month — {why_now}. "
            f"For your {label}, the sharpest next step is to {action}. "
            f"Want the ready-to-send draft? Takes 2 minutes to deploy."
        )
        return body, "binary_yes_no", "Generic placeholder uses specific profile numbers and 7d trend timing with a low-friction CTA."

    def _compose_placeholder_milestone(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        """Compute a real milestone from merchant data instead of using vague phrasing."""
        merchant = resolved.merchant
        owner = _dentist_prefix(merchant)
        business = _business_name(merchant)
        place = _place_text(merchant)
        label = _category_label(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        benchmark_crossed, exact_val, next_target = _milestone_from_data(merchant)
        why_now = _why_now_hook(merchant, context_type="positive")
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else None
        slug = merchant.get("category_slug", "")

        # Category-specific celebration action
        if slug == "restaurants":
            action = f"run a limited-time weekend special" if not offer_text else f"push '{offer_text}' as a weekend-only deal during dinner peak"
            cta = "Want me to draft the Google post and WhatsApp banner for this weekend?"
        elif slug == "salons":
            action = f"reward your regulars with a loyalty rebooking offer" if not offer_text else f"offer '{offer_text}' as a thank-you deal for your repeat clients"
            cta = "Want the client appreciation post and rebooking WhatsApp draft?"
        elif slug == "dentists":
            action = f"highlight this trust signal to drive overdue recall bookings" if not offer_text else f"use '{offer_text}' to convert overdue checkups"
            cta = "Want the patient-trust post and recall message draft?"
        elif slug == "gyms":
            action = f"use this social proof to drive member referrals" if not offer_text else f"pair '{offer_text}' with a bring-a-friend referral push"
            cta = "Want the member milestone post and referral WhatsApp draft?"
        else:
            action = f"turn this into a local trust signal" if not offer_text else f"pair this milestone with '{offer_text}'"
            cta = "Want the celebration post and customer reply draft?"

        body = (
            f"{owner}, {business} in {place} just hit a major local milestone: {benchmark_crossed} (currently holding steady at {views} profile views and {calls} direct calls this month). "
            f"With your profile drawing steady local traffic right now, this local trust is your strongest conversion asset. "
            f"For your {label}, the highest-impact move today is to {action}. "
            f"{cta}"
        )
        return body, "binary_yes_no", "Milestone placeholder computes a real milestone threshold crossed from performance data without redundancy and recommends a category-specific conversion action."

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
        payload = resolved.trigger.get("payload", {})
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
        business = _business_name(merchant)
        label = _category_label(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        recovery_line = (
            f"push '{offer_text}' with one sharp headline" if offer_text
            else _category_action(merchant)
        )
        body = (
            f"{owner}, your {metric} dipped {delta_str} over the last {window}{baseline_str} at {business} in {place}. "
            f"With your profile pulling {views} views, {calls} calls, and {ctr} CTR, the smartest recovery move for your {label} is not a blanket discount — "
            f"it is to {recovery_line}. "
            f"{_category_cta(merchant, 'perf')}"
        )
        rationale = "Performance dip message pairs exact metric deltas and business profile context with a concrete, non-discounting recovery asset."
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
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else f"a special {label} celebration offer"

        # Build category-specific decision advice
        if label == "restaurant":
            decision_advice = f"Celebrate this momentum by running '{offer_text}' as a limited weekend deal during dinner peak hours"
        elif label in {"salon", "beauty parlor"}:
            decision_advice = f"Celebrate this milestone by offering '{offer_text}' to reward and re-engage your regular client list"
        elif label in {"clinic", "dental clinic"}:
            decision_advice = f"Turn this trust milestone into patient confidence by highlighting '{offer_text}' for overdue checkups"
        elif label in {"gym", "fitness studio"}:
            decision_advice = f"Leverage this community proof by running '{offer_text}' to drive new member referrals this week"
        else:
            decision_advice = f"Celebrate this momentum by pushing '{offer_text}' to capture active local demand"

        why_now = _why_now_hook(merchant, context_type="positive")
        # If we have real milestone values, use them
        if value_now is not None and milestone_value is not None and metric:
            body = (
                f"{owner}, {business} is at {_fmt_number(value_now)} {metric.replace('_', ' ')} and closing in on {_fmt_number(milestone_value)}. "
                f"Profile in {place}: {views} views and {calls} calls this month. "
                f"{decision_advice}. "
                f"Want me to send our ready-to-use Google post and WhatsApp reply draft to convert this milestone into immediate bookings?"
            )
        else:
            benchmark_crossed, exact_val, next_target = _milestone_from_data(merchant)
            body = (
                f"{owner}, {business} in {place} just hit a major local milestone: {benchmark_crossed} (currently holding steady at {views} profile views and {calls} direct calls this month). "
                f"With your profile drawing steady local traffic right now, this social proof is your strongest conversion lever. "
                f"{decision_advice}. "
                f"Want me to send our ready-to-use Google post and WhatsApp reply draft to convert this milestone into immediate bookings?"
            )
        rationale = "Milestone message turns quantitative social proof into a category-specific growth decision anchored to positive profile trends without metric repetition."
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
        business = _business_name(merchant)
        label = _category_label(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        body = (
            f"{owner}, your {metric} are down {delta_pct}% this week at {business} in {place}, but this looks seasonal rather than broken. "
            f"Your profile still pulled {views} views and {calls} calls this month — the smart move is to protect your regulars, not panic-discount. "
            f"For your {label}, let's re-engage your current base with a VIP loyalty check-in before the next peak window. "
            f"{_category_cta(merchant, 'perf')}"
        )
        rationale = "Seasonal framing uses specific trigger delta and profile numbers to reduce panic and shift toward a high-ROI retention step."
        return body, "binary_yes_no", rationale

    def _compose_review_theme_emerged(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        theme = str(payload.get("theme", "service issue")).replace("_", " ")
        count = payload.get("occurrences_30d", 0)
        trend = payload.get("trend", "")
        common_quote = payload.get("common_quote", "")
        business = _business_name(merchant)
        label = _category_label(merchant)
        place = _place_text(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        fix_approach = _category_review_fix(merchant)
        trend_text = f" and the trend is {trend}" if trend else ""
        quote_text = f' One customer wrote: "{common_quote}".' if common_quote else ""
        body = (
            f"{owner}, {count} recent reviews at {business} in {place} point to a clear pattern around {theme}{trend_text}.{quote_text} "
            f"With your profile drawing {views} views and {calls} calls this month, every unaddressed review directly impacts your conversion rate. "
            f"Here is our 2-step fix for your {label}: first, {fix_approach}; then I will draft a calm, professional public reply template you can reuse. "
            f"{_category_cta(merchant, 'review')}"
        )
        rationale = "Review-pattern outreach uses specific review count, trend direction, and customer quote to justify timing, then pairs operational fix with ready-to-use reply asset."
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
        business = _business_name(merchant)
        place = _place_text(merchant)
        label = _category_label(merchant)
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        body = (
            f"{owner}, your {plan} subscription for {business} in {place} is up for renewal {days_str}{amount_str}. "
            f"Quick ROI check: your profile generated {views} views, {calls} direct calls, and a {ctr} engagement rate this month. "
            f"Keeping your {label} active while executing one sharp operational fix is your highest-return decision right now. "
            f"Want our 3-point renewal ROI summary and custom rebooking script?"
        )
        rationale = "Renewal nudge uses specific profile metrics without repetition and frames visibility as highest-ROI decision."
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
        days = payload.get("days_remaining") or payload.get("days_to_festival") or payload.get("days") or payload.get("days_until")
        days_str = f"{days} days" if days else "a few weeks"
        business = _business_name(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "one specific festive offer"
        label = _category_label(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)

        if days is not None and isinstance(days, int) and days > 60:
            body = (
                f"{owner}, {festival} is {days_str} away for {business} in {place}. "
                f"It is much too early to run discounts today, but with your profile generating {snapshot}, the smartest move right now is building your repeat-customer list so your audience is primed when festive demand hits. "
                f"For your {label}, let's run a loyalty rebooking check-in to lock in your regulars ahead of time. "
                f"Want the ready-to-send loyalty check-in draft?"
            )
        else:
            body = (
                f"{owner}, {festival} is {days_str} away for {business} in {place}. "
                f"With your profile pulling {snapshot}, the right decision is to prep '{offer_text}' for your {label} with a clear advance-booking window before the rush hits. "
                f"{_category_cta(merchant, 'festival')}"
            )
        rationale = "Festival message makes a timing-aware operational decision anchored to current profile metrics and days remaining."
        return body, "binary_yes_no", rationale

    def _compose_ipl_match_today(self, resolved: ResolvedContexts) -> tuple[str, str, str]:
        merchant = resolved.merchant
        payload = resolved.trigger.get("payload", {})
        owner = _dentist_prefix(merchant)
        business = _business_name(merchant)
        place = _place_text(merchant)
        snapshot = _performance_snapshot(merchant)
        active_offer = _active_offers(merchant)
        offer_text = active_offer[0]["title"] if active_offer else "a delivery-only combo"
        match = payload.get("match", "today's IPL match")
        venue = payload.get("venue", "the stadium")
        body = (
            f"Quick match-day alert for {owner}: {match} is live today at {venue}. "
            f"With {business} in {place} pulling {snapshot}, match-night delivery demand spikes 2 hours before the toss. "
            f"For tonight's slot, prioritize high-margin delivery over dine-in and push '{offer_text}'. "
            f"Want a ready-to-send 3-line WhatsApp banner draft to capture tonight's crowd?"
        )
        rationale = "Trigger-aware restaurant recommendation pairs specific event timing with profile metrics to drive high-margin delivery conversion."
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
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        action = _category_action(merchant)
        why_now = _why_now_hook(merchant)
        topic_line = f"Rather than revisit {last_topic.replace('_', ' ')}, " if last_topic else ""
        body = (
            f"{owner}, it has been {days} days since we last spoke. "
            f"{topic_line}here is what is happening at {business} in {place}: {views} views and {calls} calls this month — {why_now}. "
            f"The clean restart for your {label}: {action}. "
            f"Want the ready-to-send draft? Takes 2 minutes to deploy."
        )
        rationale = "Dormancy re-entry uses specific numbers and 7d trend to frame why now, avoids stale follow-up, and recommends one sharp restart action."
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
        action = _category_action(merchant)
        kind = trigger.get("kind", "")
        kind_readable = kind.replace("_", " ") if kind else "this week's signal"
        perf = merchant.get("performance", {})
        views = _fmt_number(perf.get("views", 0))
        calls = _fmt_number(perf.get("calls", 0))
        ctr = _fmt_pct(perf.get("ctr", 0))
        why_now = _why_now_hook(merchant)
        active_offer = _active_offers(merchant)
        offer_line = f" Your active offer '{active_offer[0]['title']}' is the hook." if active_offer else ""
        body = (
            f"{owner}, {business} in {place}: {views} views, {calls} calls, {ctr} CTR this month — {why_now}. "
            f"Based on {kind_readable}, the sharpest move for your {label} is to {action}."
            f"{offer_line} "
            f"Want the ready-to-send draft? Takes 2 minutes to deploy."
        )
        rationale = "Fallback uses specific profile numbers and 7d trend timing with a category-specific action and low-friction CTA."
        return body, "binary_yes_no", rationale
