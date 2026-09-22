"""Opt-in, privacy-preserving checks for official OpenAI documentation changes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


OFFICIAL_SOURCES = {
    "pricing": "https://learn.chatgpt.com/docs/pricing.md",
    "models": "https://learn.chatgpt.com/docs/models.md",
}
CHECK_INTERVAL = timedelta(days=1)
USER_AGENT = "codex-cost-dashboard-update-check/0.1 (+local-only)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_update_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_update_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def is_due(state: dict[str, Any], now: datetime | None = None) -> bool:
    checked = state.get("checked_at")
    if not checked:
        return True
    try:
        checked_at = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now or utc_now()) - checked_at >= CHECK_INTERVAL


def fetch_source(url: str) -> dict[str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/markdown,text/plain;q=0.9"})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed official URLs only
        content = response.read()
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "etag": str(response.headers.get("ETag") or ""),
            "last_modified": str(response.headers.get("Last-Modified") or ""),
        }


def check_official_updates(path: Path, *, force: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Check public docs only; no local usage/account/session data is sent."""
    now = now or utc_now()
    previous = load_update_state(path)
    if not force and not is_due(previous, now):
        return {**previous, "checked": False}

    sources: dict[str, dict[str, str]] = {}
    try:
        for name, url in OFFICIAL_SOURCES.items():
            sources[name] = {"url": url, **fetch_source(url)}
    except (OSError, URLError, TimeoutError) as error:
        result = {
            **previous,
            "checked": True,
            "error": "Could not reach official OpenAI documentation",
            "error_detail": str(error),
            "checked_at": now.isoformat(),
        }
        save_update_state(path, result)
        return result

    old_sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}
    baseline_exists = bool(old_sources)
    changed = baseline_exists and any(
        old_sources.get(name, {}).get("sha256") != source["sha256"] for name, source in sources.items()
    )
    result = {
        "checked": True,
        "checked_at": now.isoformat(),
        "sources": sources,
        "changed": changed,
        "baseline_established": not baseline_exists,
        "error": None,
    }
    save_update_state(path, result)
    return result
