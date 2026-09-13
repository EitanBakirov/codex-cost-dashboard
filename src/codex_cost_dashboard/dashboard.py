#!/usr/bin/env python3
"""A global, local browser dashboard for Codex Desktop usage."""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .monitor import (
    DEFAULT_CODEX_HOME,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_USD_PER_CREDIT,
    MeteredUsage,
    PromptRun,
    SessionFollower,
    SessionState,
    consume_event,
    is_primary_session,
    logical_session_id,
    logical_session_paths,
    read_events,
)


DEFAULT_STATE_FILE = DEFAULT_CODEX_HOME / "cost-dashboard" / "dashboard-state.json"
DEFAULT_AUTH_FILE = DEFAULT_CODEX_HOME / "auth.json"

HTML_PATH = Path(__file__).with_name("dashboard.html")
HTML = HTML_PATH.read_text(encoding="utf-8")

# The bundled dashboard is intentionally a single portable HTML file.  Keep
# these small inspector enhancements here so the document remains readable
# despite its compact generated markup.
INSPECTOR_ENHANCEMENTS = """
<style>
.tool-category{display:inline-flex;margin-right:7px;padding:2px 6px;border:1px solid #7692b950;border-radius:5px;background:#6f8fc51a;color:#afc6ea;font-size:10px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;vertical-align:1px}
</style>
<script>
(() => {
  const openCommands = new Set();
  const intentFor = command => {
    const executable = (command.trim().split(/\\s+/, 1)[0] || '').split('/').pop();
    const lower = command.toLowerCase();
    const gitAction = command.trim().split(/\\s+/, 3)[1] || '';
    if (executable === 'git') {
      if (['status','diff','log','show','branch','remote','rev-parse'].includes(gitAction)) return ['Git'];
      if (['fetch','pull','push'].includes(gitAction)) return ['Git sync'];
      if (['add','commit','switch','checkout','merge','rebase','reset','restore'].includes(gitAction)) return ['Git change'];
      return ['Git'];
    }
    if (['pytest','tox'].includes(executable) || lower.includes('pytest') || lower.includes(' -m unittest') || /\\bnpm\\s+(?:run\\s+)?test\\b/.test(lower)) return ['Test'];
    if (['make','cmake'].includes(executable) || /\\b(?:npm\\s+run\\s+build|cargo\\s+build)\\b/.test(lower)) return ['Build'];
    if (/\\b(?:pip(?:3)?\\s+install|npm\\s+(?:install|ci)|brew\\s+install)\\b/.test(lower)) return ['Install'];
    if (['uvicorn','gunicorn'].includes(executable) || /\\b(?:npm\\s+run\\s+(?:dev|start)|flask\\s+run)\\b/.test(lower)) return ['Server'];
    const intents = {
      rg: ['Search', 'Search project files'], grep: ['Search', 'Search file contents'],
      find: ['Find', 'Find local files'], ls: ['Browse', 'List local files'],
      pwd: ['Browse', 'Check the working folder'], cat: ['Read', 'Read a local file'],
      sed: ['Read', 'Read or transform local text'], head: ['Read', 'Read the start of a local file'],
      tail: ['Read', 'Read the end of a local file'],
      python: ['Run', 'Run a local Python command'], python3: ['Run', 'Run a local Python command'],
      node: ['Run', 'Run a local Node command'], npm: ['Run', 'Run a local npm command']
    };
    return intents[executable] || ['Shell', 'Run a local shell command'];
  };
  const decorateTools = () => {
    const prompt = document.getElementById('inspectTitle')?.textContent || '';
    document.querySelectorAll('#pTools .tool-command').forEach((details, index) => {
      const command = details.querySelector('code')?.textContent || '';
      const key = `${prompt}|${index}|${command}`;
      details.dataset.commandKey = key;
      details.open = openCommands.has(key);
      const [category, explanation] = intentFor(command);
      const card = details.closest('.tool-call');
      const action = card?.querySelector('.tool-action');
      if (action && !action.querySelector('.tool-category')) {
        const badge = document.createElement('span');
        badge.className = 'tool-category';
        badge.textContent = category;
        action.prepend(badge);
      }
      const summary = details.querySelector('summary');
      const summaryText = details.open ? 'Hide shell command' : 'Show shell command';
      // MutationObserver watches this panel.  Avoid rewriting an identical
      // label, which would otherwise schedule another observer callback.
      if (summary && summary.textContent !== summaryText) summary.textContent = summaryText;
      if (!details.dataset.persistenceBound) {
        details.dataset.persistenceBound = 'true';
        details.addEventListener('toggle', () => {
          if (details.open) openCommands.add(key); else openCommands.delete(key);
          const label = details.querySelector('summary');
          const labelText = details.open ? 'Hide shell command' : 'Show shell command';
          if (label && label.textContent !== labelText) label.textContent = labelText;
        });
      }
    });
  };
  new MutationObserver(decorateTools).observe(document.getElementById('pTools'), {childList: true, subtree: true});
  decorateTools();
})();
</script>
"""
HTML = HTML.replace("</body>", INSPECTOR_ENHANCEMENTS + "</body>")


def usage_dict(meter: MeteredUsage, usd_per_credit: float) -> dict[str, Any]:
    usage = meter.usage
    return {
        "credits": meter.credits,
        "usd_equivalent": meter.credits * usd_per_credit,
        "costs": {
            "fresh_input_usd_equivalent": meter.fresh_input_credits * usd_per_credit,
            "cached_input_usd_equivalent": meter.cached_input_credits * usd_per_credit,
            "output_usd_equivalent": meter.output_credits * usd_per_credit,
        },
        "model_calls": meter.model_calls,
        "tool_calls": meter.tool_calls,
        "usage": {
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_percent": usage.cache_percent,
        },
    }


def prompt_duration_seconds(prompt: PromptRun) -> int | None:
    if not prompt.started_at:
        return None
    try:
        started = datetime.fromisoformat(prompt.started_at.replace("Z", "+00:00"))
        finished = (
            datetime.fromisoformat(prompt.completed_at.replace("Z", "+00:00"))
            if prompt.completed_at
            else datetime.now(started.tzinfo)
        )
        return max(0, round((finished - started).total_seconds()))
    except (TypeError, ValueError):
        return None


def short_local_path(path: str) -> str:
    """Return enough of a local path to identify it without dominating UI."""
    clean = path.rstrip("/\\").replace("\\", "/")
    parts = [part for part in clean.split("/") if part]
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else clean


def prompt_history_preview(text: str, limit: int = 220) -> str:
    """Make injected skill/path markup scannable in the history list only."""
    source = html.unescape(text).strip()
    full_skill = re.fullmatch(
        r"<skill>\s*<name>\s*([^<]+?)\s*</name>[\s\S]*?</skill>", source, re.I
    )
    if full_skill:
        return f"Skill loaded: {full_skill.group(1).strip()}"

    # The compact skill marker is part of a user prompt. Keep its identity,
    # but remove the implementation path so the person's message remains legible.
    source = re.sub(r"<image\s+name=\[[^>]+>", "", source, flags=re.I)
    source = re.sub(
        r"\[\$([^\]]+)\]\((?:[^)]*[\\/])?SKILL\.md\)",
        "",
        source,
        flags=re.I,
    )
    source = re.sub(
        r"\[([^\]]+)\]\(((?:/|[A-Za-z]:[\\/])[^)\s]+)\)",
        lambda match: f"{match.group(1)} ({short_local_path(match.group(2))})",
        source,
    )
    # The linked resource is shown once as a short clickable chip.  Leaving
    # Markdown's [label](URL) in the preview would show the same URL twice
    # and crowd out the person's actual words.
    source = re.sub(r"\[([^\]]*)\]\(https?://[^)\s]+\)", "", source, flags=re.I)
    source = re.sub(r"(?<![\w(\[])(https?://[^\s<>()\]]+)", "", source, flags=re.I)
    source = re.sub(
        r"(?<!\w)((?:/(?:Users|private|var)/|[A-Za-z]:[\\/])[^\s`'\"<>]+)",
        lambda match: short_local_path(match.group(1)),
        source,
    )
    source = " ".join(source.split())
    return source[:limit] or "(empty prompt)"


def prompt_input_tags(prompt: PromptRun) -> list[dict[str, str]]:
    """Return compact, reliable input-context labels for a history row."""
    source = html.unescape(prompt.text)
    tags: list[dict[str, str]] = []

    skills = list(re.finditer(r"\[\$([^\]]+)\]\((?:[^)]*[\\/])?SKILL\.md\)", source, re.I))
    for match in skills[:2]:
        name = match.group(1).strip()
        if name:
            tags.append({"kind": "skill", "label": f"Skill · {name}"})
    if len(skills) > 2:
        tags.append({"kind": "skill", "label": f"+{len(skills) - 2} skills"})

    image_markers = re.findall(r"<image\s+name=\[[^>]+>", source, re.I)
    image_count = max(prompt.input_images, len(image_markers))
    if image_count:
        tags.append({"kind": "image", "label": f"{image_count} image" + ("s" if image_count != 1 else "")})

    # Remove constructs already represented above before looking for ordinary
    # local files, otherwise a skill/image's transport path would be duplicated.
    remaining = re.sub(r"\[\$[^\]]+\]\((?:[^)]*[\\/])?SKILL\.md\)", "", source, flags=re.I)
    remaining = re.sub(r"<image\s+name=\[[^>]+>", "", remaining, flags=re.I)
    local_paths = {
        match.rstrip(".,;:!?)]")
        for match in re.findall(
            r"(?<!\w)(?:/(?:Users|private|var)/|[A-Za-z]:[\\/])[^\s`'\"<>]+",
            remaining,
        )
    }
    if local_paths:
        tags.append({"kind": "file", "label": f"{len(local_paths)} local file" + ("s" if len(local_paths) != 1 else "")})

    markdown_links = re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", remaining, re.I)
    plain_links = re.findall(r"(?<![\w(\[])(https?://[^\s<>()\]]+)", remaining, re.I)
    links = list(dict.fromkeys(markdown_links + plain_links))
    if links:
        first = links[0]
        host = re.sub(r"^www\.", "", urlparse(first).netloc, flags=re.I) or "link"
        label = host if len(links) == 1 else f"{host} +{len(links) - 1}"
        tags.append({"kind": "link", "label": label, "href": first})
    return tags


def prompt_dict(prompt: PromptRun | None, usd_per_credit: float) -> dict[str, Any] | None:
    if prompt is None:
        return None
    return {
        "number": prompt.number,
        "preview": " ".join(prompt.text.split())[:220] or "(empty prompt)",
        "history_preview": prompt_history_preview(prompt.text),
        "input_tags": prompt_input_tags(prompt),
        "full_text": prompt.text,
        "model": prompt.model or "model not recorded",
        "effort": prompt.effort,
        "started_at": prompt.started_at,
        "duration_seconds": prompt_duration_seconds(prompt),
        "completed": prompt.completed,
        "meter": usage_dict(prompt.meter, usd_per_credit),
        "tools": prompt.tools,
    }


def format_plan_snapshot(
    used_percent: float | None, reset_at_seconds: int | None, observed_at: str | None
) -> dict[str, Any]:
    reset_at = None
    reset_at_iso = None
    if reset_at_seconds:
        reset = datetime.fromtimestamp(reset_at_seconds).astimezone()
        reset_at = f"resets {reset:%b} {reset.day}, {reset:%H:%M}"
        reset_at_iso = reset.isoformat()
    return {
        "used_percent": used_percent,
        "reset_at": reset_at,
        "reset_at_iso": reset_at_iso,
        "observed_at_iso": observed_at,
    }


def local_account(auth_file: Path = DEFAULT_AUTH_FILE) -> dict[str, str | None]:
    """Return non-secret identity metadata from Codex's currently active local login."""
    unknown = {"email": None, "name": None, "plan": None}
    try:
        auth = json.loads(auth_file.read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        token = str(tokens.get("id_token") or "")
        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_part).decode("utf-8"))
        details = claims.get("https://api.openai.com/auth") or {}
        return {
            "email": claims.get("email"),
            "name": claims.get("name"),
            "plan": details.get("chatgpt_plan_type"),
        }
    except (IndexError, OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return unknown


def latest_plan_snapshot(sessions_dir: Path) -> dict[str, Any]:
    """Find the newest local rate-limit observation, independent of the selected task."""
    latest: SessionState | None = None
    latest_key = ""
    try:
        # The active account's most recent Codex work is necessarily among the
        # recently modified logs. Limiting this keeps dashboard refreshes fast
        # even when the shared local archive contains many old accounts/tasks.
        paths = sorted(
            sessions_dir.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
        )[:32]
        for path in paths:
            if not is_primary_session(path):
                continue
            state = recent_plan_in_log(path)
            if state.plan_used_percent is None:
                continue
            observed_key = state.plan_observed_at or ""
            if latest is None or observed_key > latest_key:
                latest, latest_key = state, observed_key
    except OSError:
        pass
    if latest is None:
        return format_plan_snapshot(None, None, None)
    return format_plan_snapshot(latest.plan_used_percent, latest.plan_reset_at, latest.plan_observed_at)


def recent_plan_in_log(path: Path) -> SessionState:
    """Read only the tail of a log: rate-limit events live at the newest turns."""
    state = SessionState(path=path)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            buffer = b""
            remaining = 512 * 1024
            while position and remaining:
                size = min(64 * 1024, position, remaining)
                position -= size
                remaining -= size
                handle.seek(position)
                buffer = handle.read(size) + buffer
                lines = buffer.splitlines()
                buffer = lines.pop(0) if position else b""
                for raw in reversed(lines):
                    try:
                        event = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    consume_event(state, event)
                    if state.plan_used_percent is not None:
                        return state
    except OSError:
        pass
    return state


def state_dict(state: SessionState, usd_per_credit: float, selection: dict[str, Any]) -> dict[str, Any]:
    context_percent = None
    if state.context_window:
        context_percent = 100 * state.current_context_tokens / state.context_window
    return {
        "session_id": state.session_id or state.path.stem,
        "project": state.project,
        "model": state.current_model,
        "effort": state.current_effort,
        "prompt_count": len(state.prompts),
        "current_prompt": prompt_dict(state.current_prompt, usd_per_credit),
        "prompts": [prompt_dict(prompt, usd_per_credit) for prompt in state.prompts],
        "task": usage_dict(state.task, usd_per_credit),
        "context": {"tokens": state.current_context_tokens, "window": state.context_window, "percent": context_percent},
        "plan": format_plan_snapshot(state.plan_used_percent, state.plan_reset_at, state.plan_observed_at),
        "selection": selection,
    }


def prompt_started_at(prompt: PromptRun) -> datetime | None:
    try:
        return datetime.fromisoformat(str(prompt.started_at).replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return None


def aggregate_sessions(sessions_dir: Path, usd_per_credit: float, range_name: str) -> dict[str, Any]:
    """Build a local-only prompt-level aggregate over primary Codex task logs."""
    now = datetime.now().astimezone()
    if range_name == "today":
        window_start = now - timedelta(hours=24)
        label = "Last 24 hours"
    elif range_name == "workday":
        today_at_seven = now.replace(hour=7, minute=0, second=0, microsecond=0)
        window_start = today_at_seven if now >= today_at_seven else today_at_seven - timedelta(days=1)
        label = "Since 7 AM"
    elif range_name == "7d":
        window_start = now - timedelta(days=7)
        label = "Last 7 days"
    else:
        range_name = "all"
        window_start = None
        label = "All local history"

    meter = MeteredUsage()
    prompt_count = 0
    task_count = 0
    model_counts: dict[str, int] = {}
    try:
        paths = list(sessions_dir.rglob("*.jsonl"))
    except OSError:
        paths = []
    seen_tasks: set[str] = set()
    for path in paths:
        try:
            if not is_primary_session(path):
                continue
            task_id = logical_session_id(path)
            if task_id in seen_tasks:
                continue
            seen_tasks.add(task_id)
            state = SessionState(path=path)
            for fragment in logical_session_paths(sessions_dir, path):
                with fragment.open("r", encoding="utf-8") as log:
                    read_events(log, state)
        except OSError:
            continue
        included_prompts: list[PromptRun] = []
        for prompt in state.prompts:
            started_at = prompt_started_at(prompt)
            if window_start is None or (started_at is not None and started_at >= window_start):
                included_prompts.append(prompt)
        if not included_prompts:
            continue
        task_count += 1
        for prompt in included_prompts:
            meter.usage.add(prompt.meter.usage)
            meter.credits += prompt.meter.credits
            meter.model_calls += prompt.meter.model_calls
            meter.tool_calls += prompt.meter.tool_calls
            meter.unknown_pricing_calls += prompt.meter.unknown_pricing_calls
            prompt_count += 1
            # Local transcript fragments occasionally omit the turn's model.
            # Keep the prompt visible without implying that "unknown" is a
            # model the person selected.
            model = (prompt.model or "model not recorded").strip() or "model not recorded"
            model_counts[model] = model_counts.get(model, 0) + 1
    return {
        **usage_dict(meter, usd_per_credit),
        "prompt_count": prompt_count,
        "task_count": task_count,
        "model_mix": [
            {"model": model, "prompt_count": count, "percent": round(count * 100 / prompt_count)}
            for model, count in sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))
        ] if prompt_count else [],
        "range": range_name,
        "range_label": label,
        "range_start": window_start.isoformat() if window_start else None,
        "range_end": now.isoformat(),
    }


def thread_names(index_path: Path) -> dict[str, str]:
    """Return the newest user-customized Codex task name for each session."""
    names: dict[str, str] = {}
    try:
        with index_path.open("r", encoding="utf-8") as index:
            for line in index:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(record.get("id") or "")
                name = " ".join(str(record.get("thread_name") or "").split())
                if session_id and name:
                    names[session_id] = name
    except OSError:
        pass
    return names


def session_summary(path: Path, names: dict[str, str]) -> dict[str, str] | None:
    meta: dict[str, Any] = {}
    first_prompt = ""
    model = "Unknown model"
    try:
        with path.open("r", encoding="utf-8") as log:
            for line in log:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "session_meta":
                    meta = payload
                elif event.get("type") == "event_msg" and payload.get("type") == "user_message" and not first_prompt:
                    first_prompt = " ".join(str(payload.get("message") or "").split())
                elif event.get("type") == "turn_context" and payload.get("model"):
                    model = str(payload["model"])
                if meta and first_prompt and model != "Unknown model":
                    break
        updated_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        modified = f"{updated_at:%b} {updated_at.day}, {updated_at:%H:%M}"
    except OSError:
        return None
    project = Path(str(meta.get("cwd") or "Unknown project")).name
    session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
    return {
        "path": str(path),
        "project": project,
        "name": names.get(session_id, ""),
        "preview": (first_prompt or "(no user prompt recorded)")[:92],
        "model": model,
        "updated": modified,
    }


def recent_sessions(sessions_dir: Path, limit: int = 24) -> list[dict[str, str]]:
    try:
        paths = sorted(sessions_dir.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []
    names = thread_names(sessions_dir.parent / "session_index.jsonl")
    summaries = [session_summary(path, names) for path in paths[: limit * 3]]
    seen_tasks: set[str] = set()
    user_tasks = []
    for summary in summaries:
        if summary is None or summary["model"] == "codex-auto-review":
            continue
        path = Path(summary["path"])
        task_id = logical_session_id(path)
        if not is_primary_session(path) or task_id in seen_tasks:
            continue
        seen_tasks.add(task_id)
        user_tasks.append(summary)
    return user_tasks[:limit]


class DashboardHandler(BaseHTTPRequestHandler):
    server: "DashboardServer"

    def send_body(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/":
            self.send_body(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif route == "/api/state":
            self.send_body(200, "application/json; charset=utf-8", json.dumps(self.server.snapshot()).encode("utf-8"))
        elif route == "/api/tasks":
            self.send_body(200, "application/json; charset=utf-8", json.dumps({"tasks": recent_sessions(self.server.sessions_dir)}).encode("utf-8"))
        elif route == "/api/global":
            query = urlparse(self.path).query
            range_name = query.partition("range=")[2].split("&", 1)[0]
            self.send_body(
                200,
                "application/json; charset=utf-8",
                json.dumps(self.server.global_snapshot(range_name, force="refresh=1" in query)).encode("utf-8"),
            )
        else:
            self.send_body(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/select":
            self.send_body(404, "text/plain; charset=utf-8", b"Not found")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10_000)
            selection = json.loads(self.rfile.read(length))
            self.server.select(selection)
        except (json.JSONDecodeError, OSError, ValueError) as error:
            self.send_body(400, "application/json; charset=utf-8", json.dumps({"error": str(error)}).encode("utf-8"))
            return
        self.send_body(204, "text/plain; charset=utf-8", b"")

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], sessions_dir: Path, state_file: Path, usd_per_credit: float) -> None:
        self.sessions_dir = sessions_dir.resolve()
        self.state_file = state_file
        self.usd_per_credit = usd_per_credit
        self.lock = threading.Lock()
        self.selection = self.load_selection()
        self.follower = self.new_follower(self.selection)
        self.global_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.plan_cache: tuple[float, dict[str, Any]] | None = None
        try:
            super().__init__(address, DashboardHandler)
        except BaseException:
            self.follower.close()
            raise

    def load_selection(self) -> dict[str, Any]:
        try:
            selection = json.loads(self.state_file.read_text(encoding="utf-8"))
            if selection.get("mode") == "pinned" and self.valid_session_path(selection.get("path")):
                return {"mode": "pinned", "path": str(Path(selection["path"]).resolve())}
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return {"mode": "latest", "path": ""}

    def valid_session_path(self, raw_path: Any) -> bool:
        try:
            path = Path(str(raw_path)).expanduser().resolve()
            path.relative_to(self.sessions_dir)
            return path.is_file() and path.suffix == ".jsonl"
        except (OSError, ValueError):
            return False

    def new_follower(self, selection: dict[str, Any]) -> SessionFollower:
        fixed = Path(selection["path"]) if selection["mode"] == "pinned" else None
        return SessionFollower(self.sessions_dir, fixed)

    def save_selection(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.selection), encoding="utf-8")

    def select(self, requested: dict[str, Any]) -> None:
        mode = requested.get("mode")
        if mode == "latest":
            next_selection = {"mode": "latest", "path": ""}
        elif mode == "pinned" and self.valid_session_path(requested.get("path")):
            next_selection = {"mode": "pinned", "path": str(Path(requested["path"]).resolve())}
        else:
            raise ValueError("Choose a valid Codex session")
        with self.lock:
            next_follower = self.new_follower(next_selection)
            self.follower.close()
            self.follower = next_follower
            self.selection = next_selection
            self.save_selection()

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now().timestamp()
        with self.lock:
            state = self.follower.refresh()
            selection = dict(self.selection)
            if selection["mode"] == "latest":
                selection["path"] = str(state.path)
            result = state_dict(state, self.usd_per_credit, selection)
            cached_plan = self.plan_cache
        if cached_plan is None or now - cached_plan[0] >= 15:
            plan = latest_plan_snapshot(self.sessions_dir)
            with self.lock:
                self.plan_cache = (now, plan)
        else:
            plan = cached_plan[1]
        result["plan"] = plan
        result["account"] = local_account(self.sessions_dir.parent / "auth.json")
        return result

    def global_snapshot(self, range_name: str, force: bool = False) -> dict[str, Any]:
        range_name = range_name if range_name in {"today", "workday", "7d", "all"} else "all"
        now = datetime.now().timestamp()
        with self.lock:
            cached = self.global_cache.get(range_name)
            if not force and cached and now - cached[0] < 10:
                return cached[1]
        result = aggregate_sessions(self.sessions_dir, self.usd_per_credit, range_name)
        with self.lock:
            self.global_cache[range_name] = (now, result)
        return result

    def close_monitor(self) -> None:
        with self.lock:
            self.follower.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a global local Codex usage dashboard.")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--usd-per-credit", type=float, default=DEFAULT_USD_PER_CREDIT)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        print("--port must be between 1 and 65535", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = DashboardServer(("127.0.0.1", args.port), args.sessions_dir.expanduser(), args.state_file.expanduser(), args.usd_per_credit)
    except (FileNotFoundError, OSError) as error:
        print(f"Could not start dashboard at {url}: {error}", file=sys.stderr)
        return 1
    print(f"Codex cost dashboard: {url}")
    print("This page is local to this computer. Press Ctrl-C here to stop it.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        server.close_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
