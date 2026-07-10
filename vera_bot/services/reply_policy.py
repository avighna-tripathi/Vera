from __future__ import annotations

from vera_bot.services.context_resolver import ResolvedContexts
from vera_bot.services.validators import avoid_repetition, sanitize_text
from vera_bot.store import ConversationRecord, SuppressionStore


AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "our team will respond shortly",
    "automated assistant",
    "auto-reply",
]
OPT_OUT_PATTERNS = ["stop messaging", "not interested", "stop", "unsubscribe", "don't message", "do not message"]
HOSTILE_PATTERNS = ["useless", "spam", "bothering me", "idiot", "stupid"]
INTENT_PATTERNS = ["let's do it", "lets do it", "go ahead", "proceed", "what's next", "whats next", "confirm", "yes do it"]
OUT_OF_SCOPE_PATTERNS = ["gst", "tax filing", "ca", "income tax"]
YES_PATTERNS = ["yes", "send", "draft", "please share", "please send", "ok", "okay"]
ARTIFACT_PATTERNS = [
    "final text",
    "text only",
    "exact whatsapp",
    "patient whatsapp",
    "google post",
    "write it now",
    "show me the whatsapp",
    "draft it now",
]


class ReplyPolicy:
    def __init__(self, suppression_store: SuppressionStore) -> None:
        self.suppression_store = suppression_store

    def decide(self, record: ConversationRecord, resolved: ResolvedContexts, merchant_message: str) -> dict:
        text = merchant_message.strip()
        lowered = text.lower()

        if any(pattern in lowered for pattern in OPT_OUT_PATTERNS):
            record.ended = True
            record.opt_out = True
            self.suppression_store.opt_out_merchant(record.merchant_id, days=30)
            return {"action": "end", "rationale": "Merchant explicitly opted out. Closing conversation and suppressing future outreach."}

        if any(pattern in lowered for pattern in HOSTILE_PATTERNS):
            record.ended = True
            self.suppression_store.opt_out_merchant(record.merchant_id, days=30)
            return {"action": "end", "rationale": "Merchant frustration is explicit; ending cleanly and backing off for 30 days."}

        if self._looks_like_auto_reply(lowered):
            record.auto_reply_count += 1
            if record.auto_reply_count >= 3:
                record.ended = True
                return {"action": "end", "rationale": "Auto-reply repeated three times with no real engagement; closing the conversation."}
            if record.auto_reply_count == 2:
                return {"action": "wait", "wait_seconds": 86400, "rationale": "Same auto-reply repeated twice; waiting 24 hours for the owner."}
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Detected a likely WhatsApp Business auto-reply; backing off for 4 hours.",
            }

        if any(pattern in lowered for pattern in OUT_OF_SCOPE_PATTERNS):
            body = "I’ll leave GST and tax work to your CA. On this thread, I can help with the business message or draft we were discussing. Want the draft first or the short summary?"
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Politely declining an out-of-scope request and redirecting to the active business task."}

        if any(pattern in lowered for pattern in ARTIFACT_PATTERNS):
            body = self._deliver_artifact(resolved, lowered)
            return {
                "action": "send",
                "body": avoid_repetition(body, record.sent_bodies),
                "cta": "open_ended",
                "rationale": "Merchant asked for the final asset, so this turn should deliver a send-ready draft instead of repeating the summary.",
            }

        if any(pattern in lowered for pattern in INTENT_PATTERNS):
            body = self._action_body(resolved)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "binary_confirm_cancel", "rationale": "Merchant signaled commitment, so the conversation switches from qualifying to execution."}

        if any(pattern in lowered for pattern in YES_PATTERNS):
            body = self._helpful_follow_up(resolved)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Merchant accepted the value exchange, so the next turn delivers the promised asset and advances one step."}

        body = self._clarify_or_nudge(resolved, text)
        return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Keeping the thread moving with a concise, context-aware clarification."}

    def _looks_like_auto_reply(self, lowered: str) -> bool:
        return any(pattern in lowered for pattern in AUTO_REPLY_PATTERNS)

    def _action_body(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        trigger = resolved.trigger
        kind = trigger.get("kind")
        if kind == "active_planning_intent":
            return f"Great. I’m drafting the first version now for {trigger.get('payload', {}).get('intent_topic', 'this plan')}. I’ll keep it tight: offer, price point, and CTA. Reply with 'final text' when you want the send-ready version."
        if kind in {"research_digest", "cde_opportunity", "regulation_change"}:
            return f"Great. I’ll package the key points into a short, usable draft for {_business_name(merchant)} next. Reply with 'patient WhatsApp' or 'Google post' and I’ll generate the final text."
        return f"Great. I’m moving this into draft mode now for {_business_name(merchant)}. Reply with 'final text' and I’ll shape the next send-ready version."

    def _helpful_follow_up(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        trigger = resolved.trigger
        kind = trigger.get("kind")
        if kind == "research_digest":
            return "The digest point is strong because it ties to a real cohort and gives you a credible opener. If you want the finished asset now, reply with 'patient WhatsApp' or 'Google post'."
        if kind == "recall_due":
            return f"Perfect. I can hold the current slot options under {_business_name(merchant)} and turn this into the final reminder text now. Reply with 'final text' if you want the send-ready version."
        return f"Understood. I can draft the next usable message for {_business_name(merchant)} now and keep it short enough to send as-is."

    def _deliver_artifact(self, resolved: ResolvedContexts, lowered: str) -> str:
        kind = resolved.trigger.get("kind")
        if kind == "research_digest":
            if "google post" in lowered:
                return self._research_digest_google_post(resolved)
            return self._research_digest_patient_whatsapp(resolved)
        if kind == "recall_due":
            return self._recall_due_whatsapp(resolved)
        return self._planning_draft(resolved)

    def _research_digest_patient_whatsapp(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        offer = self._first_offer_title(merchant)
        return sanitize_text(
            f"Patient WhatsApp draft for {_business_name(merchant)}:\n\n"
            f"Hi! If it’s been a while since your last dental visit, this is a good time to restart preventive care. "
            f"At {_business_name(merchant)}, we’re currently offering {offer}. "
            "A recent high-risk adult cohort also showed better outcomes with closer preventive follow-up. "
            "If you’d like, reply here and we can help you book a convenient slot."
        )

    def _research_digest_google_post(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        offer = self._first_offer_title(merchant)
        return sanitize_text(
            f"Google post draft for {_business_name(merchant)}:\n\n"
            "Prevention works best when patients come back before problems build up. "
            f"At {_business_name(merchant)}, we’re helping patients restart routine care with {offer}. "
            "If your cleaning or preventive visit is overdue, message us to book your next slot."
        )

    def _recall_due_whatsapp(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        return sanitize_text(
            f"WhatsApp reminder draft for {_business_name(merchant)}:\n\n"
            f"Hi! This is a quick reminder from {_business_name(merchant)} that your follow-up is due. "
            "If you want, reply with your preferred time and we’ll help arrange the next visit."
        )

    def _planning_draft(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        topic = resolved.trigger.get("payload", {}).get("intent_topic", "your current campaign")
        return sanitize_text(
            f"Starter draft for {_business_name(merchant)}:\n\n"
            f"We can turn {topic} into a short campaign message with a clear offer, who it is for, and one simple CTA. "
            "If you want, send the audience or offer and I’ll tighten it further."
        )

    def _clarify_or_nudge(self, resolved: ResolvedContexts, original_text: str) -> str:
        merchant = resolved.merchant
        return sanitize_text(
            f"Understood. I can keep this simple for {_business_name(merchant)}. "
            f"If you want, send me just one preference and I’ll tailor the draft around it: offer, audience, or timing."
        )

    def _first_offer_title(self, merchant: dict) -> str:
        offers = merchant.get("offers") or []
        if offers:
            return offers[0].get("title", "your active offer")
        return "your active offer"


def _business_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "your business")
