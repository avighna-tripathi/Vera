from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from vera_bot.services.context_resolver import ResolvedContexts


@dataclass(slots=True)
class LLMRefinement:
    body: str
    cta: str
    rationale: str


class OpenRouterRefiner:
    def __init__(self, api_key: str, model: str, referer: str, title: str) -> None:
        self.api_key = api_key
        self.model = model
        self.referer = referer
        self.title = title

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def refine(self, resolved: ResolvedContexts, draft_body: str, draft_cta: str, draft_rationale: str) -> LLMRefinement:
        if not self.enabled:
            raise RuntimeError("OpenRouter API key missing")

        category = resolved.category
        merchant = resolved.merchant
        trigger = resolved.trigger
        customer = resolved.customer

        prompt = {
            "task": "Improve a WhatsApp message for magicpin's merchant assistant without inventing any facts.",
            "rules": [
                "Use only facts present in the provided context or draft.",
                "Do not fabricate numbers, names, citations, or offers.",
                "No URLs.",
                "Keep the message concise and natural.",
                "Return valid JSON only with keys: body, cta, rationale.",
                "Allowed cta values: none, open_ended, binary_yes_no, binary_confirm_cancel, multi_choice_slot.",
                "Preserve merchant-facing vs customer-facing tone based on trigger.scope.",
                "Prefer specificity, category fit, merchant fit, trigger relevance, and low-friction CTA.",
            ],
            "context": {
                "category_slug": category.get("slug"),
                "category_voice": category.get("voice", {}),
                "merchant_identity": merchant.get("identity", {}),
                "merchant_signals": merchant.get("signals", []),
                "merchant_offers": [offer for offer in merchant.get("offers", []) if offer.get("status") == "active"],
                "trigger": trigger,
                "customer": customer,
            },
            "draft": {
                "body": draft_body,
                "cta": draft_cta,
                "rationale": draft_rationale,
            },
        }

        request_body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a careful marketing-and-engagement writing assistant. You never invent facts and you return strict JSON only.",
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            }
        ).encode("utf-8")

        request = urlrequest.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=request_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": self.referer,
                "X-Title": self.title,
            },
        )

        try:
            with urlrequest.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urlerror.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return LLMRefinement(
            body=str(parsed.get("body", draft_body)).strip(),
            cta=str(parsed.get("cta", draft_cta)).strip(),
            rationale=str(parsed.get("rationale", draft_rationale)).strip(),
        )
