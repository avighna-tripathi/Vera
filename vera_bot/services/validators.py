from __future__ import annotations

import re


URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    text = re.sub(URL_RE, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def validate_action_shape(body: str, rationale: str) -> tuple[bool, str | None]:
    if not body.strip():
        return False, "empty_body"
    if URL_RE.search(body):
        return False, "url_disallowed"
    if not rationale.strip():
        return False, "missing_rationale"
    return True, None


def avoid_repetition(body: str, prior_bodies: list[str]) -> str:
    normalized = body.strip().lower()
    if normalized and any(prev.strip().lower() == normalized for prev in prior_bodies):
        return f"{body} If useful, I can tailor this to your current priority."
    return body

