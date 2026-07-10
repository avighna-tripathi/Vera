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
REVISION_PATTERNS = [
    "shorter",
    "shorten",
    "stronger cta",
    "booking cta",
    "call to action",
    "hindi-english",
    "hinglish",
    "casual",
    "patient-friendly",
    "patient friendly",
    "rewrite",
    "revise",
    "refine",
    "improve",
    "change",
    "make it",
    "make this",
    "add a",
    "add an",
    "add ",
    "remove ",
    "target ",
    "audience",
    "version",
]


class ReplyPolicy:
    def __init__(self, suppression_store: SuppressionStore) -> None:
        self.suppression_store = suppression_store

    def decide(self, record: ConversationRecord, resolved: ResolvedContexts, merchant_message: str) -> dict:
        lowered = merchant_message.strip().lower()

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
            return {"action": "wait", "wait_seconds": 14400, "rationale": "Detected a likely WhatsApp Business auto-reply; backing off for 4 hours."}

        if any(pattern in lowered for pattern in OUT_OF_SCOPE_PATTERNS):
            body = "I'll leave GST and tax work to your CA. On this thread, I can help with the business message or draft we were discussing. Want the draft first or the short summary?"
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Politely declining an out-of-scope request and redirecting to the active business task."}

        if self._is_revision_request(record, lowered):
            body = self._revise_existing_draft(record, resolved, lowered)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Merchant asked to refine the existing draft, so this turn edits the current deliverable instead of restarting the summary flow."}

        if any(pattern in lowered for pattern in ARTIFACT_PATTERNS):
            body = self._deliver_artifact(resolved, lowered)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Merchant asked for the final asset, so this turn should deliver a send-ready draft instead of repeating the summary."}

        if any(pattern in lowered for pattern in INTENT_PATTERNS):
            body = self._action_body(resolved)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "binary_confirm_cancel", "rationale": "Merchant signaled commitment, so the conversation switches from qualifying to execution."}

        if any(pattern in lowered for pattern in YES_PATTERNS):
            body = self._helpful_follow_up(resolved)
            return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Merchant accepted the value exchange, so the next turn delivers the promised asset and advances one step."}

        body = self._clarify_or_nudge(resolved)
        return {"action": "send", "body": avoid_repetition(body, record.sent_bodies), "cta": "open_ended", "rationale": "Keeping the thread moving with a concise, context-aware clarification."}

    def _looks_like_auto_reply(self, lowered: str) -> bool:
        return any(pattern in lowered for pattern in AUTO_REPLY_PATTERNS)

    def _is_revision_request(self, record: ConversationRecord, lowered: str) -> bool:
        return bool(self._latest_bot_body(record)) and any(pattern in lowered for pattern in REVISION_PATTERNS)

    def _action_body(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        trigger = resolved.trigger
        kind = trigger.get("kind")
        if kind == "active_planning_intent":
            return f"Great. I'm drafting the first version now for {trigger.get('payload', {}).get('intent_topic', 'this plan')}. I'll keep it tight: offer, price point, and CTA. Reply with 'final text' when you want the send-ready version."
        if kind in {"research_digest", "cde_opportunity", "regulation_change"}:
            return f"Great. I'll package the key points into a short, usable draft for {_business_name(merchant)} next. Reply with 'patient WhatsApp' or 'Google post' and I'll generate the final text."
        return f"Great. I'm moving this into draft mode now for {_business_name(merchant)}. Reply with 'final text' and I'll shape the next send-ready version."

    def _helpful_follow_up(self, resolved: ResolvedContexts) -> str:
        merchant = resolved.merchant
        kind = resolved.trigger.get("kind")
        if kind == "research_digest":
            return "The digest point is strong because it ties to a real cohort and gives you a credible opener. If you want the finished asset now, reply with 'patient WhatsApp' or 'Google post'."
        if kind == "recall_due":
            return f"Perfect. I can hold the current slot options under {_business_name(merchant)} and turn this into the final reminder text now. Reply with 'final text' if you want the send-ready version."
        return f"Understood. I can draft the next usable message for {_business_name(merchant)} now and keep it short enough to send as-is."

    def _deliver_artifact(self, resolved: ResolvedContexts, lowered: str) -> str:
        options = self._parse_options(lowered)
        kind = resolved.trigger.get("kind")
        if kind == "research_digest":
            channel = self._channel_from_text(lowered, "patient_whatsapp")
            if channel == "google_post":
                return self._research_digest_google_post(resolved, options)
            return self._research_digest_patient_whatsapp(resolved, options)
        if kind == "recall_due":
            return self._recall_due_whatsapp(resolved, options)
        return self._planning_draft(resolved, options)

    def _revise_existing_draft(self, record: ConversationRecord, resolved: ResolvedContexts, lowered: str) -> str:
        options = self._parse_options(lowered)
        latest_body = self._latest_bot_body(record)
        channel = self._channel_from_text(lowered, self._channel_from_existing_body(latest_body))
        kind = resolved.trigger.get("kind")
        if kind == "research_digest":
            if channel == "google_post":
                return self._research_digest_google_post(resolved, options)
            return self._research_digest_patient_whatsapp(resolved, options)
        if kind == "recall_due":
            return self._recall_due_whatsapp(resolved, options)
        return self._planning_draft(resolved, options)

    def _research_digest_patient_whatsapp(self, resolved: ResolvedContexts, options: dict[str, bool]) -> str:
        clinic_name = _business_name(resolved.merchant)
        offer = self._first_offer_title(resolved.merchant)

        opener = "Hi! If it has been a while since your last dental visit, this is a good time to restart preventive care."
        if options["hinglish"]:
            opener = "Hi! Agar aapke last dental visit ko kaafi time ho gaya hai, ab preventive care restart karne ka sahi time hai."
        elif options["casual"]:
            opener = "Hi! If your last dental visit has been pending for a while, this is a good time to get back on track."

        audience = []
        if options["lapsed"]:
            audience.append("This is especially relevant for patients whose routine visit has been overdue for months.")
        if options["high_risk"]:
            audience.append("It is particularly relevant for higher-risk adults who benefit from steady preventive follow-up.")
        if options["whitening"]:
            audience.append("It can also work well for patients who are already thinking about smile-improvement options.")
        audience_text = " ".join(audience)

        value_line = f"At {clinic_name}, we are currently offering {offer}."
        if options["whitening"]:
            value_line = f"At {clinic_name}, we are currently offering {offer}, and we can also guide patients who are interested in brighter-looking smiles."

        proof_line = "A recent high-risk adult cohort also showed better outcomes with closer preventive follow-up."
        if options["patient_friendly"]:
            proof_line = "Recent preventive-care evidence also supports staying consistent with follow-up visits, especially for higher-risk adults."
        if options["hinglish"]:
            proof_line = "Recent evidence bhi yahi suggest karti hai ki regular preventive follow-up se outcomes better hote hain, especially higher-risk adults ke liye."

        cta_line = "Reply here and we can help you book a convenient slot."
        if options["stronger_cta"]:
            cta_line = "Reply BOOK today and we will help you lock the next available slot."
        if options["tomorrow_morning"]:
            cta_line = "Reply BOOK and we can help you reserve a tomorrow morning slot, subject to availability."
        if options["hinglish"] and options["stronger_cta"]:
            cta_line = "Reply BOOK today and hum aapko next available slot jaldi confirm kar denge."
        elif options["hinglish"]:
            cta_line = "Reply kijiye and hum aapko convenient slot book karne mein help kar denge."

        if options["shorter"]:
            return sanitize_text(f"Patient WhatsApp draft for {clinic_name}:\n\n{opener} {value_line} {cta_line}")

        return sanitize_text(f"Patient WhatsApp draft for {clinic_name}:\n\n{opener} {audience_text} {value_line} {proof_line} {cta_line}")

    def _research_digest_google_post(self, resolved: ResolvedContexts, options: dict[str, bool]) -> str:
        clinic_name = _business_name(resolved.merchant)
        offer = self._first_offer_title(resolved.merchant)
        line_one = "Prevention works best when patients come back before problems build up."
        line_two = f"At {clinic_name}, we are helping patients restart routine care with {offer}."
        line_three = "If your cleaning or preventive visit is overdue, message us to book your next slot."

        if options["shorter"]:
            line_one = f"{clinic_name} is helping patients restart preventive care with {offer}."
        if options["stronger_cta"]:
            line_three = "Message us today to reserve your next preventive visit."
        if options["hinglish"]:
            line_one = "Preventive care tab best kaam karti hai jab patients routine visits miss nahin karte."
            line_two = f"{clinic_name} mein abhi {offer} available hai for patients restarting routine care."
            line_three = "Message kijiye and next slot book kar lijiye."

        return sanitize_text(f"Google post draft for {clinic_name}:\n\n{line_one} {line_two} {line_three}")

    def _recall_due_whatsapp(self, resolved: ResolvedContexts, options: dict[str, bool]) -> str:
        clinic_name = _business_name(resolved.merchant)
        if options["stronger_cta"]:
            return sanitize_text(f"WhatsApp reminder draft for {clinic_name}:\n\nHi! This is a quick reminder from {clinic_name} that your follow-up is due. Reply BOOK today with your preferred time and we will help confirm the next slot.")
        return sanitize_text(f"WhatsApp reminder draft for {clinic_name}:\n\nHi! This is a quick reminder from {clinic_name} that your follow-up is due. Reply with your preferred time and we will help arrange the next visit.")

    def _planning_draft(self, resolved: ResolvedContexts, options: dict[str, bool]) -> str:
        topic = resolved.trigger.get("payload", {}).get("intent_topic", "your current campaign")
        business = _business_name(resolved.merchant)
        if options["shorter"]:
            return sanitize_text(f"Starter draft for {business}:\n\n{topic}: clear offer, right audience, one simple CTA.")
        return sanitize_text(f"Starter draft for {business}:\n\nWe can turn {topic} into a short campaign message with a clear offer, who it is for, and one simple CTA. If you want, send the audience or offer and I will tighten it further.")

    def _clarify_or_nudge(self, resolved: ResolvedContexts) -> str:
        return sanitize_text(f"Understood. I can keep this simple for {_business_name(resolved.merchant)}. If you want, send me just one preference and I'll tailor the draft around it: offer, audience, or timing.")

    def _parse_options(self, lowered: str) -> dict[str, bool]:
        return {
            "shorter": any(token in lowered for token in ["shorter", "short ", "short.", "concise", "crisp"]),
            "stronger_cta": any(token in lowered for token in ["stronger cta", "booking cta", "call to action", "book now", "stronger booking"]),
            "hinglish": any(token in lowered for token in ["hindi-english", "hinglish", "hindi english"]),
            "patient_friendly": any(token in lowered for token in ["patient-friendly", "patient friendly", "simple", "easy"]),
            "casual": any(token in lowered for token in ["casual", "friendly tone"]),
            "whitening": "whitening" in lowered,
            "lapsed": "6 months" in lowered or "six months" in lowered or "lapsed" in lowered or "overdue" in lowered,
            "high_risk": "high-risk" in lowered or "high risk" in lowered,
            "tomorrow_morning": "tomorrow morning" in lowered,
        }

    def _latest_bot_body(self, record: ConversationRecord) -> str:
        for turn in reversed(record.turns):
            if turn.get("from") == "bot" and turn.get("body"):
                return str(turn["body"])
        return ""

    def _channel_from_existing_body(self, body: str) -> str:
        lowered = body.lower()
        if "google post draft" in lowered:
            return "google_post"
        if "patient whatsapp draft" in lowered or "whatsapp reminder draft" in lowered:
            return "patient_whatsapp"
        return "patient_whatsapp"

    def _channel_from_text(self, lowered: str, fallback: str) -> str:
        if "google post" in lowered:
            return "google_post"
        if "patient whatsapp" in lowered or "whatsapp" in lowered:
            return "patient_whatsapp"
        return fallback

    def _first_offer_title(self, merchant: dict) -> str:
        offers = merchant.get("offers") or []
        if offers:
            return offers[0].get("title", "your active offer")
        return "your active offer"


def _business_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "your business")
