from __future__ import annotations

import re


SENTENCE_SPLIT_RE = re.compile(r"(?<=[!?])\s+|(?<!\bDr)(?<!\bMr)(?<!\bMrs)(?<!\bMs)\.\s+")


def template_name_for(trigger_kind: str, send_as: str) -> str:
    prefix = "merchant" if send_as == "merchant_on_behalf" else "vera"
    normalized = trigger_kind.lower().replace(" ", "_")
    return f"{prefix}_{normalized}_v1"


def template_params_for(body: str, merchant_name: str, customer_name: str | None = None) -> list[str]:
    sentences = [sentence.strip().rstrip(".") for sentence in SENTENCE_SPLIT_RE.split(body.replace("\n", " ")) if sentence.strip()]
    params: list[str] = []
    if customer_name:
        params.append(customer_name)
    params.append(merchant_name)
    params.extend(sentences[:3])
    return params[:5]
