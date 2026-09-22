#!/usr/bin/env python3
"""Live, local cost estimate for the active Codex Desktop task.

The monitor reads Codex's append-only JSONL session log. It does not contact
OpenAI and does not modify the session. Dollar values are usage equivalents,
not an invoice: actual billing can differ because of included allowances,
workspace-specific credit pricing, fast mode, and server-side adjustments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .rate_card import MODEL_ALIASES, MODEL_RATES


def codex_home() -> Path:
    """Return Codex's user-level data directory on every supported OS."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


DEFAULT_CODEX_HOME = codex_home()
DEFAULT_SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
DEFAULT_USD_PER_CREDIT = 0.04


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_percent(self) -> float:
        if not self.input_tokens:
            return 0.0
        return 100.0 * self.cached_input_tokens / self.input_tokens

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class MeteredUsage:
    usage: Usage = field(default_factory=Usage)
    credits: float = 0.0
    fresh_input_credits: float = 0.0
    cached_input_credits: float = 0.0
    output_credits: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    unknown_pricing_calls: int = 0

    def add_model_call(self, call: Usage, model: str | None) -> None:
        self.usage.add(call)
        self.model_calls += 1
        rates = MODEL_RATES.get(normalize_model(model))
        if rates is None:
            self.unknown_pricing_calls += 1
            return
        uncached_rate, cached_rate, output_rate = rates
        fresh = call.uncached_input_tokens * uncached_rate / 1_000_000
        cached = call.cached_input_tokens * cached_rate / 1_000_000
        output = call.output_tokens * output_rate / 1_000_000
        self.fresh_input_credits += fresh
        self.cached_input_credits += cached
        self.output_credits += output
        self.credits += fresh + cached + output


@dataclass
class PromptRun:
    number: int
    text: str
    started_at: str | None
    model: str | None = None
    effort: str | None = None
    completed: bool = False
    completed_at: str | None = None
    turn_id: str | None = None
    input_images: int = 0
    meter: MeteredUsage = field(default_factory=MeteredUsage)
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionState:
    path: Path
    session_id: str | None = None
    project: str | None = None
    originator: str | None = None
    started_at: str | None = None
    current_model: str | None = None
    current_effort: str | None = None
    current_context_tokens: int = 0
    context_window: int = 0
    plan_used_percent: float | None = None
    plan_reset_at: int | None = None
    plan_observed_at: str | None = None
    task: MeteredUsage = field(default_factory=MeteredUsage)
    prompts: list[PromptRun] = field(default_factory=list)
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    malformed_lines: int = 0

    @property
    def current_prompt(self) -> PromptRun | None:
        return self.prompts[-1] if self.prompts else None

    @property
    def last_completed_prompt(self) -> PromptRun | None:
        for prompt in reversed(self.prompts):
            if prompt.completed:
                return prompt
        return None


def normalize_model(model: str | None) -> str:
    if not model:
        return ""
    normalized = model.lower().replace("_", "-")
    return MODEL_ALIASES.get(normalized, normalized)


def usage_from_dict(value: dict[str, Any] | None) -> Usage:
    value = value or {}
    return Usage(
        input_tokens=int(value.get("input_tokens") or 0),
        cached_input_tokens=int(value.get("cached_input_tokens") or 0),
        output_tokens=int(value.get("output_tokens") or 0),
    )


def is_tool_call(payload: dict[str, Any]) -> bool:
    return payload.get("type") in {
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "web_search_call",
        "computer_call",
    }


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def humanize_tool_name(name: str) -> str:
    """Turn a stable tool identifier into a compact, readable label."""
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    return re.sub(r"[_-]+", " ", name).strip().title() or "Tool"


def nested_tool_names(source: str) -> list[str]:
    """Extract directly invoked tools from an orchestrator script without executing it."""
    names = re.findall(r"\btools\.([A-Za-z][A-Za-z0-9_]*)\s*\(", source)
    return list(dict.fromkeys(names))


def tool_action(name: str) -> str:
    canonical = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    actions = {
        "wait": "Wait for an asynchronous tool job",
        "exec_command": "Run a local shell command",
        "write_stdin": "Read or write an active tool job",
        "apply_patch": "Apply a local code patch",
        "headroom_stats": "Read Headroom compression metrics",
        "headroom_compress": "Compress a context payload",
    }
    return actions.get(canonical, f"Run {humanize_tool_name(canonical)}")


def shell_command(source: str, tool_names: list[str]) -> str | None:
    """Extract the command passed to exec_command from Codex's recorded call input."""
    if "exec_command" not in tool_names:
        return None
    match = re.search(r'"cmd"\s*:\s*"((?:\\.|[^"\\])*)"', source, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1)


def shell_command_target(tokens: list[str]) -> str:
    """Return one short, safe-to-display command target when it is obvious."""
    candidates = [token for token in tokens[1:] if token and not token.startswith(("-", "$"))]
    if not candidates:
        return ""
    target = candidates[-1].rstrip("/\\")
    if not target or target in {"|", "&&", ";"} or "=" in target:
        return ""
    target = target.replace("\\", "/")
    parts = [part for part in target.split("/") if part and part not in {".", ".."}]
    if not parts:
        return ""
    return "/".join(parts[-2:])


def shell_command_description(command: str | None) -> tuple[str, str]:
    """Return a concise, non-speculative intent label for a shell command."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        tokens = (command or "").lstrip().split()
    executable = tokens[0].rsplit("/", 1)[-1] if tokens else ""
    subcommand = tokens[1] if len(tokens) > 1 else ""
    lower = (command or "").lower()
    target = shell_command_target(tokens)
    suffix = f" · {target}" if target else ""

    if executable == "git":
        if subcommand in {"status", "diff", "log", "show", "branch", "remote", "rev-parse"}:
            return "Git", "Inspect Git state"
        if subcommand in {"fetch", "pull", "push"}:
            return "Git sync", "Synchronize Git with a remote"
        if subcommand in {"add", "commit", "switch", "checkout", "merge", "rebase", "reset", "restore"}:
            return "Git change", "Change local Git state"
        return "Git", "Run a Git command"

    if executable in {"pytest", "tox"} or "pytest" in lower or " -m unittest" in lower or re.search(r"\bnpm\s+(?:run\s+)?test\b", lower):
        return "Test", "Run project tests"
    if executable in {"make", "cmake"} or re.search(r"\b(?:npm\s+run\s+build|cargo\s+build)\b", lower):
        return "Build", "Build the project"
    if re.search(r"\b(?:pip(?:3)?\s+install|npm\s+(?:install|ci)|brew\s+install)\b", lower):
        return "Install", "Install local dependencies"
    if executable in {"uvicorn", "gunicorn"} or re.search(r"\b(?:npm\s+run\s+(?:dev|start)|flask\s+run)\b", lower):
        return "Server", "Start a local development server"

    descriptions = {
        "rg": ("Search", "Search project files" + suffix),
        "grep": ("Search", "Search file contents" + suffix),
        "find": ("Find", "Find local files" + suffix),
        "ls": ("Browse", "List local files" + suffix),
        "pwd": ("Browse", "Check the working folder"),
        "cat": ("Read", "Read a local file" + suffix),
        "sed": ("Read", "Read or transform local text" + suffix),
        "head": ("Read", "Read the start of a local file" + suffix),
        "tail": ("Read", "Read the end of a local file" + suffix),
        "python": ("Run", "Run a local Python command"),
        "python3": ("Run", "Run a local Python command"),
        "node": ("Run", "Run a local Node command"),
        "npm": ("Run", "Run a local npm command"),
    }
    return descriptions.get(executable, ("Shell", "Run a local shell command"))


def describe_tool_call(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, privacy-conscious dashboard record for a tool call."""
    call_type = str(payload.get("type") or "tool")
    name = str(payload.get("name") or payload.get("tool_name") or payload.get("function") or "Unnamed tool")
    source = str(payload.get("input") or payload.get("arguments") or "")
    nested = nested_tool_names(source) if call_type == "custom_tool_call" else []
    display_name = name
    action = f"Run {humanize_tool_name(name)}"

    command = shell_command(source, nested or [name])
    category = "Tool"
    if nested:
        display_name = ", ".join(humanize_tool_name(tool) for tool in nested[:3])
        if len(nested) > 3:
            display_name += f" +{len(nested) - 3} more"
        action = tool_action(nested[0]) if len(nested) == 1 else "Run multiple nested tools through the orchestrator"
        category = humanize_tool_name(nested[0]) if len(nested) == 1 else "Multiple tools"
    else:
        action = tool_action(name)
        category = humanize_tool_name(name)

    if command:
        category, action = shell_command_description(command)

    return {
        "id": str(payload.get("call_id") or payload.get("id") or ""),
        "type": call_type,
        "name": name,
        "display_name": display_name,
        "action": action,
        "nested_tools": nested,
        "command": command,
        "category": category,
        "status": "running",
        "started_at": None,
        "completed_at": None,
        "duration_seconds": None,
        "result": "Waiting for tool output",
    }


def event_duration_seconds(started_at: str | None, completed_at: str | None) -> int | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return max(0, round((completed - started).total_seconds()))
    except (TypeError, ValueError):
        return None


def tool_output_summary(payload: dict[str, Any]) -> tuple[str, str]:
    """Return a deterministic status and terse outcome without exposing raw tool output."""
    if payload.get("error") or payload.get("is_error"):
        return "failed", "Tool returned an error"
    output = payload.get("output")
    if isinstance(output, str) and "Script running with cell ID" in output:
        return "running", "Tool job is still running"
    return "succeeded", "Tool completed successfully"


def consume_event(state: SessionState, event: dict[str, Any]) -> None:
    event_type = event.get("type")
    payload = event_payload(event)

    if event_type == "session_meta":
        state.session_id = payload.get("session_id") or payload.get("id")
        state.project = payload.get("cwd")
        state.originator = payload.get("originator")
        state.started_at = payload.get("timestamp") or event.get("timestamp")
        return

    if event_type == "turn_context":
        state.current_model = payload.get("model") or state.current_model
        state.current_effort = payload.get("effort") or state.current_effort
        prompt = state.current_prompt
        if prompt and not prompt.completed:
            prompt.model = state.current_model
            prompt.effort = state.current_effort
            prompt.turn_id = payload.get("turn_id") or prompt.turn_id
        return

    if event_type == "event_msg" and payload.get("type") == "task_started":
        # A new turn is definitive evidence that an older incomplete turn is
        # no longer active, even if its completion event was lost.
        prompt = state.current_prompt
        if prompt and not prompt.completed:
            prompt.completed = True
            prompt.completed_at = event.get("timestamp")
        return

    # Current Codex Desktop logs user prompts as response items. Older logs
    # use event_msg/user_message below, so keep both shapes for history.
    if event_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        content = payload.get("content") or []
        text = "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"input_text", "text"}
        ).strip()
        input_images = sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") == "input_image"
        )
        # Desktop prepends its runtime instructions as a role=user message at
        # the start of a task. It is context, not a prompt the person sent.
        if (
            text.startswith("<recommended_plugins>")
            or text.startswith("# AGENTS.md instructions")
            or text.startswith("<turn_aborted>")
        ):
            return
        # Skill contents are injected into the same turn immediately after the
        # user's compact skill marker. They are context, not another prompt:
        # retaining them split the real prompt from its work and its duration.
        if re.match(r"^<skill>\s*<name>\s*[^<]+\s*</name>[\s\S]*</skill>\s*$", text):
            return
        if text:
            prior = state.current_prompt
            if prior and not prior.completed:
                prior.completed = True
                prior.completed_at = event.get("timestamp")
            metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
            state.prompts.append(
                PromptRun(
                    number=len(state.prompts) + 1,
                    text=text,
                    started_at=event.get("timestamp"),
                    model=state.current_model,
                    effort=state.current_effort,
                    turn_id=metadata.get("turn_id"),
                    input_images=input_images,
                )
            )
        return

    if event_type == "response_item" and is_tool_call(payload):
        state.task.tool_calls += 1
        if state.current_prompt and not state.current_prompt.completed:
            state.current_prompt.meter.tool_calls += 1
            tool = describe_tool_call(payload)
            tool["started_at"] = event.get("timestamp")
            if payload.get("status") == "completed":
                tool["status"] = "succeeded"
                tool["result"] = "Tool completed successfully"
            state.current_prompt.tools.append(tool)
            if tool["id"]:
                state.pending_tool_calls[tool["id"]] = tool
        return

    if event_type == "response_item" and str(payload.get("type") or "").endswith("_output"):
        call_id = str(payload.get("call_id") or "")
        tool = state.pending_tool_calls.get(call_id)
        if tool:
            status, result = tool_output_summary(payload)
            tool["status"] = status
            tool["result"] = result
            tool["completed_at"] = event.get("timestamp")
            tool["duration_seconds"] = event_duration_seconds(tool.get("started_at"), tool.get("completed_at"))
            if status != "running":
                state.pending_tool_calls.pop(call_id, None)
        return

    if event_type != "event_msg":
        return

    payload_type = payload.get("type")
    if payload_type == "user_message":
        prior = state.current_prompt
        message = str(payload.get("message") or "")
        # Desktop can mirror one submitted user turn in both the modern
        # response_item format and this older event_msg format, at the same
        # instant.  It is one prompt, not a second zero-work prompt.  Compare
        # normalized text as the two encodings differ only in line wrapping.
        if (
            prior
            and prior.started_at == event.get("timestamp")
            and " ".join(prior.text.split()) == " ".join(message.split())
        ):
            return
        if prior and not prior.completed:
            # This is unusual, but retaining it as a closed run prevents later
            # events from being attributed to two prompts.
            prior.completed = True
        state.prompts.append(
            PromptRun(
                number=len(state.prompts) + 1,
                text=message,
                started_at=event.get("timestamp"),
                model=state.current_model,
                effort=state.current_effort,
            )
        )
        return

    if payload_type in {"task_complete", "turn_aborted"}:
        prompt = state.current_prompt
        if prompt:
            prompt.completed = True
            prompt.turn_id = payload.get("turn_id") or prompt.turn_id
            prompt.completed_at = event.get("timestamp")
        return

    if payload_type != "token_count":
        return

    info = payload.get("info") or {}
    last_usage = usage_from_dict(info.get("last_token_usage"))
    if last_usage.input_tokens or last_usage.output_tokens:
        state.task.add_model_call(last_usage, state.current_model)
        prompt = state.current_prompt
        if prompt and not prompt.completed:
            prompt.meter.add_model_call(last_usage, state.current_model)
            prompt.model = state.current_model or prompt.model

        state.current_context_tokens = last_usage.input_tokens
        state.context_window = int(info.get("model_context_window") or state.context_window)

    limits = payload.get("rate_limits") or {}
    primary = limits.get("primary") or {}
    if primary.get("used_percent") is not None:
        state.plan_used_percent = float(primary["used_percent"])
        state.plan_observed_at = event.get("timestamp") or state.plan_observed_at
    if primary.get("resets_at") is not None:
        state.plan_reset_at = int(primary["resets_at"])


def read_events(lines: Iterable[str], state: SessionState) -> None:
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            state.malformed_lines += 1
            continue
        if isinstance(event, dict):
            consume_event(state, event)


def is_primary_session(path: Path) -> bool:
    """Exclude internal subagent logs from the user-facing task picker."""
    try:
        with path.open("r", encoding="utf-8") as log:
            for _ in range(12):
                line = log.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "session_meta":
                    continue
                payload = event_payload(event)
                source = payload.get("source")
                return payload.get("thread_source") != "subagent" and not (
                    isinstance(source, dict) and "subagent" in source
                )
    except OSError:
        return False
    return True


def session_metadata(path: Path) -> dict[str, Any]:
    """Read the small session header used to identify a logical Codex task."""
    try:
        with path.open("r", encoding="utf-8") as log:
            for _ in range(12):
                line = log.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "session_meta":
                    return event_payload(event)
    except OSError:
        pass
    return {}


def logical_session_id(path: Path) -> str:
    """Return Codex's stable task ID, falling back to the physical log name."""
    metadata = session_metadata(path)
    return str(metadata.get("session_id") or metadata.get("id") or path.stem)


def logical_session_paths(sessions_dir: Path, path: Path) -> list[Path]:
    """Find every primary JSONL fragment belonging to one Codex task.

    Desktop can write a continuation to a new JSONL file (for example after a
    model change) while retaining the same ``session_id``.  The dashboard must
    present that as one task rather than losing its earlier prompt history.
    """
    task_id = logical_session_id(path)
    matches: list[tuple[str, Path]] = []
    try:
        candidates = sessions_dir.rglob("*.jsonl")
        for candidate in candidates:
            if not is_primary_session(candidate) or logical_session_id(candidate) != task_id:
                continue
            metadata = session_metadata(candidate)
            matches.append((str(metadata.get("timestamp") or ""), candidate))
    except OSError:
        return [path]
    return [candidate for _timestamp, candidate in sorted(matches, key=lambda item: (item[0], str(item[1])))] or [path]


def newest_session(sessions_dir: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    try:
        for path in sessions_dir.rglob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    except OSError:
        return None
    for _modified, path in sorted(candidates, reverse=True):
        if is_primary_session(path):
            return path
    return None


def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_money(credits: float, usd_per_credit: float) -> str:
    return f"${credits * usd_per_credit:,.2f} equiv"


def clean_preview(text: str, width: int = 86) -> str:
    compact = " ".join(text.split())
    if not compact:
        return "(empty prompt)"
    if len(compact) <= width:
        return compact
    return compact[: width - 1] + "…"


def meter_lines(meter: MeteredUsage, usd_per_credit: float) -> list[str]:
    usage = meter.usage
    lines = [
        f"  {format_money(meter.credits, usd_per_credit)}  |  "
        f"{meter.credits:,.2f} credits  |  {meter.model_calls} model calls  |  "
        f"{meter.tool_calls} tool calls",
        f"  input {format_tokens(usage.input_tokens)}  |  "
        f"cached {format_tokens(usage.cached_input_tokens)} ({usage.cache_percent:.1f}%)  |  "
        f"uncached {format_tokens(usage.uncached_input_tokens)}  |  "
        f"output {format_tokens(usage.output_tokens)}",
    ]
    if meter.unknown_pricing_calls:
        lines.append(
            f"  WARNING: {meter.unknown_pricing_calls} model calls have no configured price"
        )
    return lines


def render(state: SessionState, usd_per_credit: float) -> str:
    model = state.current_model or "unknown"
    effort = f"/{state.current_effort}" if state.current_effort else ""
    context = "unknown"
    if state.context_window:
        percent = 100.0 * state.current_context_tokens / state.context_window
        context = (
            f"{format_tokens(state.current_context_tokens)} / "
            f"{format_tokens(state.context_window)} ({percent:.1f}%)"
        )

    lines = [
        "CODEX DESKTOP COST MONITOR",
        "=" * 78,
        f"Task: {state.session_id or state.path.stem}",
        f"Model: {model}{effort}  |  Active context: {context}",
    ]

    if state.plan_used_percent is not None:
        limit_text = f"Plan meter: {state.plan_used_percent:.1f}% used"
        if state.plan_reset_at:
            reset = datetime.fromtimestamp(state.plan_reset_at).astimezone()
            limit_text += f"  |  resets {reset:%Y-%m-%d %H:%M}"
        lines.append(limit_text)

    prompt = state.current_prompt
    lines.extend(["", "CURRENT / MOST RECENT PROMPT"])
    if prompt is None:
        lines.append("  Waiting for the first user prompt...")
    else:
        status = "complete" if prompt.completed else "RUNNING"
        lines.append(f"  #{prompt.number} {status}  |  {clean_preview(prompt.text)}")
        lines.extend(meter_lines(prompt.meter, usd_per_credit))

    last = state.last_completed_prompt
    if last is not None and last is not prompt:
        lines.extend(["", "LAST COMPLETED PROMPT"])
        lines.append(f"  #{last.number}  |  {clean_preview(last.text)}")
        lines.extend(meter_lines(last.meter, usd_per_credit))

    lines.extend(["", "TASK TOTAL"])
    lines.extend(meter_lines(state.task, usd_per_credit))
    lines.extend(
        [
            "",
            f"Watching: {state.path}",
            "Estimate only; included allowance and actual invoice adjustments are not known locally.",
            "Press Ctrl-C to stop. Restarting reconstructs the totals from the saved task log.",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch the active Codex Desktop task and estimate usage per prompt."
    )
    parser.add_argument(
        "--session",
        type=Path,
        help="Watch one JSONL session file instead of following the newest task.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help=f"Codex sessions directory (default: {DEFAULT_SESSIONS_DIR}).",
    )
    parser.add_argument(
        "--usd-per-credit",
        type=float,
        default=DEFAULT_USD_PER_CREDIT,
        help=(
            "Dollar-equivalent conversion for one credit (default: 0.04). "
            "Set this to your workspace's purchase price for a tailored estimate."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (default: 1).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot and exit.",
    )
    return parser.parse_args()


def load_state(path: Path) -> tuple[SessionState, Any, int]:
    state = SessionState(path=path)
    handle = path.open("r", encoding="utf-8")
    read_events(handle, state)
    return state, handle, handle.tell()


def load_logical_session(sessions_dir: Path, path: Path) -> tuple[SessionState, tuple[tuple[str, int], ...]]:
    """Rebuild one task from all of its chronological on-disk log fragments."""
    paths = logical_session_paths(sessions_dir, path)
    state = SessionState(path=paths[-1])
    signature: list[tuple[str, int]] = []
    for fragment in paths:
        try:
            with fragment.open("r", encoding="utf-8") as log:
                read_events(log, state)
            signature.append((str(fragment), fragment.stat().st_mtime_ns))
        except OSError:
            continue
    if paths:
        state.path = paths[-1]
    return state, tuple(signature)


class SessionFollower:
    """Follow a logical Desktop task, including any continuation log files."""

    def __init__(self, sessions_dir: Path, fixed_session: Path | None = None) -> None:
        self.sessions_dir = sessions_dir
        self.fixed_session = fixed_session
        path = fixed_session or newest_session(sessions_dir)
        if path is None or not path.is_file():
            target = fixed_session or sessions_dir
            raise FileNotFoundError(f"No Codex JSONL session found at {target}")
        self.state, self.signature = load_logical_session(sessions_dir, path)
        self.last_discovery = 0.0

    def refresh(self) -> SessionState:
        now = time.monotonic()
        if self.fixed_session is None and now - self.last_discovery >= 3.0:
            self.last_discovery = now
            latest = newest_session(self.sessions_dir)
            if latest is not None:
                self.state, self.signature = load_logical_session(self.sessions_dir, latest)
                return self.state

        # A selected task may receive a continuation fragment or append to an
        # existing fragment. Rebuilding keeps prompt attribution correct across
        # the file boundary and is small compared with the dashboard refresh.
        self.state, self.signature = load_logical_session(self.sessions_dir, self.fixed_session or self.state.path)
        return self.state

    def close(self) -> None:
        pass


def print_screen(content: str, clear: bool) -> None:
    if clear and sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(content + "\n")
    sys.stdout.flush()


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        print("--interval must be greater than zero", file=sys.stderr)
        return 2
    if args.usd_per_credit < 0:
        print("--usd-per-credit cannot be negative", file=sys.stderr)
        return 2
    if not args.once and not sys.stdout.isatty():
        print(
            "This live terminal view needs an interactive terminal. "
            "Use codex-cost-dashboard for a browser page that refreshes in place.",
            file=sys.stderr,
        )
        return 2

    fixed_session = args.session.expanduser().resolve() if args.session else None
    sessions_dir = args.sessions_dir.expanduser().resolve()
    try:
        follower = SessionFollower(sessions_dir, fixed_session)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    try:
        while True:
            state = follower.refresh()
            print_screen(render(state, args.usd_per_credit), clear=not args.once)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        follower.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
