from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_model_name() -> str:
    explicit = os.getenv("MODEL_NAME")
    if explicit:
        return explicit
    if os.getenv("OPENROUTER_API_KEY"):
        return f"openrouter:{os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini')}"
    return "deterministic-rule-engine-v1"


@dataclass(slots=True)
class Settings:
    team_name: str = field(default_factory=lambda: os.getenv("TEAM_NAME", "magicpin-challenge"))
    team_members: list[str] = field(
        default_factory=lambda: [member.strip() for member in os.getenv("TEAM_MEMBERS", "Codex").split(",") if member.strip()]
    )
    model_name: str = field(default_factory=default_model_name)
    approach: str = field(
        default_factory=lambda: os.getenv(
            "APPROACH",
            "stateful FastAPI bot with versioned context store, trigger routing, grounded deterministic composition, optional OpenRouter refinement, and multi-turn reply policies",
        )
    )
    contact_email: str = field(default_factory=lambda: os.getenv("CONTACT_EMAIL", "team@example.com"))
    version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "0.1.0"))
    submitted_at: str = field(default_factory=lambda: os.getenv("SUBMITTED_AT", utc_now_iso()))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_model: str = field(default_factory=lambda: os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"))
    openrouter_referer: str = field(default_factory=lambda: os.getenv("OPENROUTER_REFERER", "https://magicpin.local"))
    openrouter_title: str = field(default_factory=lambda: os.getenv("OPENROUTER_TITLE", "magicpin-ai-challenge"))


SETTINGS = Settings()
