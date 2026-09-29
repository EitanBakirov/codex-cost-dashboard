#!/usr/bin/env python3
"""Print aggregate, prompt-free statistics for two models in local Codex logs.

Run with PYTHONPATH=src python scripts/analyze_model_turns.py. This reads logs
but prints no prompt text, task names, account data, or session identifiers.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from codex_cost_dashboard.monitor import (
    DEFAULT_SESSIONS_DIR,
    SessionState,
    is_primary_session,
    logical_session_id,
    normalize_model,
    read_events,
)


MODELS = ("gpt-5.6-sol", "gpt-6-sol")


def quantile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    low = int(position)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low)


def duration(started: str | None, completed: str | None) -> float | None:
    try:
        first = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
        return max(0.0, (last - first).total_seconds())
    except (TypeError, ValueError):
        return None


def describe(label: str, rows: list[dict]) -> None:
    tasks = {row["task"] for row in rows}
    with_usage = [row for row in rows if row["calls"]]
    times = [row["duration"] for row in with_usage if row["duration"] is not None]
    print(f"{label}: {len(rows)} complete turns, {len(tasks)} tasks, {len(with_usage)} metered turns")
    if not with_usage:
        return
    for name, unit in (("credits", " cr"), ("calls", ""), ("tools", ""),
                       ("input", " tokens"), ("cached", " tokens"), ("output", " tokens")):
        values = [row[name] for row in with_usage]
        print(f"  {name}: median {quantile(values, .5):,.2f}{unit}, p75 {quantile(values, .75):,.2f}{unit}, mean {statistics.mean(values):,.2f}{unit}")
    print(f"  duration: median {quantile(times, .5):,.1f}s, p75 {quantile(times, .75):,.1f}s; {len(times)} timed turns")
    print(f"  total credits: {sum(row['credits'] for row in with_usage):,.2f}")
    concentration = Counter(row["task"] for row in with_usage)
    print(f"  largest task share: {max(concentration.values()) / len(with_usage):.1%}")
    per_task = defaultdict(list)
    for row in with_usage:
        per_task[row["task"]].append(row["credits"])
    print(f"  median of per-task median credits: {quantile([quantile(v, .5) for v in per_task.values()], .5):,.2f}")


def main() -> None:
    rows: list[dict] = []
    for path in DEFAULT_SESSIONS_DIR.rglob("*.jsonl"):
        if not is_primary_session(path):
            continue
        state = SessionState(path=path)
        try:
            with path.open(encoding="utf-8") as log:
                read_events(log, state)
        except OSError:
            continue
        task = logical_session_id(path)
        for prompt in state.prompts:
            model = normalize_model(prompt.model)
            if not prompt.completed or model not in MODELS:
                continue
            usage = prompt.meter.usage
            rows.append({
                "task": task,
                "model": model,
                "effort": prompt.effort or "unknown",
                "credits": prompt.meter.credits,
                "calls": prompt.meter.model_calls,
                "tools": prompt.meter.tool_calls,
                "input": usage.input_tokens,
                "cached": usage.cached_input_tokens,
                "output": usage.output_tokens,
                "fresh_credits": prompt.meter.fresh_input_credits,
                "cached_credits": prompt.meter.cached_input_credits,
                "output_credits": prompt.meter.output_credits,
                "duration": duration(prompt.started_at, prompt.completed_at),
                "month": str(prompt.started_at or "")[:7],
            })
    for model in MODELS:
        selected = [row for row in rows if row["model"] == model]
        print(f"{model} months: {dict(sorted(Counter(row['month'] for row in selected).items()))}")
        describe(model, selected)
        for effort in ("low", "medium", "high"):
            describe(f"  {effort}", [row for row in selected if row["effort"] == effort])
    by_task_model = defaultdict(list)
    for row in rows:
        if row["calls"]:
            by_task_model[(row["task"], row["model"])].append(row)
    paired = [task for task, _model in by_task_model if all((task, model) in by_task_model for model in MODELS)]
    paired = sorted(set(paired))
    print(f"tasks containing both models: {len(paired)}")
    for model in MODELS:
        describe(f"  {model} within paired tasks", [row for row in rows if row["task"] in paired and row["model"] == model])

    sol = [row for row in rows if row["model"] == "gpt-6-sol" and row["calls"]]
    print("GPT-6 Sol usage drivers:")
    for label, selected in (
        ("1-2 calls", [row for row in sol if row["calls"] <= 2]),
        ("3-9 calls", [row for row in sol if 3 <= row["calls"] <= 9]),
        ("10+ calls", [row for row in sol if row["calls"] >= 10]),
    ):
        describe(label, selected)
    if sol:
        total_credits = sum(row["credits"] for row in sol)
        heavy = [row for row in sol if row["calls"] >= 10]
        print(f"  10+ call share of turns: {len(heavy) / len(sol):.1%}")
        print(f"  10+ call share of credits: {sum(row['credits'] for row in heavy) / total_credits:.1%}")
        for name in ("fresh", "cached", "output"):
            tokens = sum(row["input"] - row["cached"] if name == "fresh" else row[name] for row in sol)
            credits = sum(row[name + "_credits"] for row in sol)
            print(f"  {name}: {tokens:,} tokens, {credits:,.2f} credits")


if __name__ == "__main__":
    main()
