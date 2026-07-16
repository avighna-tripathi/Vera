from __future__ import annotations

import re


URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    text = re.sub(URL_RE, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_to_limit(text: str, max_chars: int = 310) -> str:
    if len(text) <= max_chars:
        return text
    # Try cutting at the last sentence end before max_chars
    for p in [". ", "! ", "? "]:
        idx = text.rfind(p, 0, max_chars)
        if idx > int(max_chars * 0.5):
            return text[: idx + 1].strip()
    # Otherwise cut at last space
    idx = text.rfind(" ", 0, max_chars - 3)
    if idx > int(max_chars * 0.5):
        return text[:idx].strip() + "..."
    return text[: max_chars - 3].strip() + "..."


def validate_action_shape(body: str, rationale: str) -> tuple[bool, str | None]:
    if not body.strip():
        return False, "empty_body"
    if URL_RE.search(body):
        return False, "url_disallowed"
    if not rationale.strip():
        return False, "missing_rationale"
    if len(body) > 320:
        return False, "body_too_long"
    return True, None


def avoid_repetition(body: str, prior_bodies: list[str]) -> str:
    normalized = body.strip().lower()
    if normalized and any(prev.strip().lower() == normalized for prev in prior_bodies):
        tailored = f"{body} If useful, I can tailor this to your current priority."
        return truncate_to_limit(tailored, max_chars=315)
    return truncate_to_limit(body, max_chars=315)


