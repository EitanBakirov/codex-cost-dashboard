"""Versioned ChatGPT-credit rates used by the local estimator.

This file is deliberately bundled with a release instead of being replaced by
data downloaded at runtime.  It makes estimates reproducible and keeps the
dashboard local-only by default.  The optional update checker merely tells the
user when the official source changed; it never edits this rate card.
"""

from __future__ import annotations


RATE_CARD_VERSION = "2026-09-23"
RATE_CARD_SOURCE = "https://learn.chatgpt.com/docs/pricing"
RATE_CARD_DESCRIPTION = "ChatGPT Standard-speed credits per 1M tokens"

# fresh input, cached input, output — all credits per one million tokens.
# Source checked 2026-09-23.  Codex credit billing has no separate cache-write
# charge.  API-key sessions have separate USD prices and are not estimated by
# this ChatGPT-credit card.
MODEL_RATES: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (250.0, 25.0, 1250.0),
    "gpt-6-sol": (50.0, 5.0, 250.0),
    "gpt-6-luna": (2.5, 0.25, 12.5),
    "gpt-5.6-sol": (100.0, 10.0, 500.0),
    "gpt-5.6-terra": (50.0, 5.0, 300.0),
    "gpt-5.6-luna": (5.0, 0.5, 30.0),
    "gpt-5.6-cyber": (312.5, 31.25, 1875.0),
    "gpt-5.5": (125.0, 12.5, 750.0),
    "gpt-5.5-cyber": (312.5, 31.25, 1875.0),
    # Retained for locally recorded history. These models are no longer
    # generally available with ChatGPT sign-in.
    "gpt-5.4": (62.5, 6.25, 375.0),
    "gpt-5.4-mini": (18.75, 1.875, 113.0),
    "gpt-5.3-codex": (43.75, 4.375, 350.0),
    "gpt-5.2": (43.75, 4.375, 350.0),
}

MODEL_ALIASES = {
    "gpt-5.4-mini-codex": "gpt-5.4-mini",
    "gpt-5.5-codex": "gpt-5.5",
    "gpt-daybreak-blue-latest": "gpt-5.6-sol",
    "gpt-daybreak-red-latest": "gpt-5.6-cyber",
    "daybreak-blue": "gpt-5.6-sol",
    "daybreak-red": "gpt-5.6-cyber",
}
