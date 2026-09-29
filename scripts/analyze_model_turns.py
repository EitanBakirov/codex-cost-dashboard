#!/usr/bin/env python3
"""Print aggregate, prompt-free statistics for two models in local Codex logs.

Run with PYTHONPATH=src python scripts/analyze_model_turns.py. This reads logs
but prints no prompt text, task names, account data, or session identifiers.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime

from codex_cost_dashboard.monitor import (
    DEFAULT_SESSIONS_DIR,
    SessionState,
    is_primary_session,
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


def signed_duration(started: str | None, completed: str | None) -> float | None:
    try:
        first = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
        return (last - first).total_seconds()
    except (TypeError, ValueError):
        return None


def tool_timestamp_span(tools: list[dict]) -> float | None:
    values = []
    for tool in tools:
        for key in ("started_at", "completed_at"):
            if tool.get(key):
                try:
                    values.append(datetime.fromisoformat(str(tool[key]).replace("Z", "+00:00")))
                except ValueError:
                    pass
    return (max(values) - min(values)).total_seconds() if values else None


def describe(label: str, rows: list[dict]) -> None:
    with_usage = [row for row in rows if row["calls"]]
    times = [row["duration"] for row in with_usage if row["duration"] is not None]
    print(f"{label}: {len(rows)} completed prompts, {len(with_usage)} metered prompts")
    if not with_usage:
        return
    for name, unit in (("credits", " cr"), ("calls", ""), ("tools", ""),
                       ("input", " tokens"), ("cached", " tokens"), ("output", " tokens")):
        values = [row[name] for row in with_usage]
        print(f"  {name}: median {quantile(values, .5):,.2f}{unit}, p75 {quantile(values, .75):,.2f}{unit}, mean {statistics.mean(values):,.2f}{unit}")
    print(f"  duration: median {quantile(times, .5):,.1f}s, p75 {quantile(times, .75):,.1f}s; {len(times)} timed turns")
    print(f"  total credits: {sum(row['credits'] for row in with_usage):,.2f}")


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
        for prompt in state.prompts:
            model = normalize_model(prompt.model)
            if not prompt.completed or model not in MODELS:
                continue
            usage = prompt.meter.usage
            rows.append({
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
                "raw_duration": signed_duration(prompt.started_at, prompt.completed_at),
                "tool_timestamp_span": tool_timestamp_span(prompt.tools),
                "month": str(prompt.started_at or "")[:7],
            })
    for model in MODELS:
        selected = [row for row in rows if row["model"] == model]
        print(f"{model} months: {dict(sorted(Counter(row['month'] for row in selected).items()))}")
        describe(model, selected)
        for month in sorted({row["month"] for row in selected}):
            timed = [row for row in selected if row["month"] == month and row["calls"] and row["duration"] is not None]
            if timed:
                negative = [row["raw_duration"] for row in timed if row["raw_duration"] is not None and row["raw_duration"] < 0]
                zero = [row for row in timed if row["raw_duration"] == 0]
                print(f"  {month} timing: {len(timed)} turns, {len(zero)} identical start/end stamps, {len(negative)} end-before-start stamps, median {quantile([row['duration'] for row in timed], .5):.1f}s")
                if negative:
                    print(f"    negative raw duration: median {quantile(negative, .5):.1f}s, min {min(negative):.1f}s")
                print(f"    below 1s: {sum(row['duration'] < 1 for row in timed)}, below 5s: {sum(row['duration'] < 5 for row in timed)}, median exact {quantile([row['duration'] for row in timed], .5):.6f}s")
                implausible = [row for row in timed if row["duration"] < 1 and row["calls"] >= 10]
                print(f"    10+ model calls in under 1s: {len(implausible)}")
                with_tools = [row for row in implausible if row["tool_timestamp_span"] is not None]
                if with_tools:
                    print(f"    of these, tool timestamps span under 1s: {sum(row['tool_timestamp_span'] < 1 for row in with_tools)}/{len(with_tools)}")
        september = [row for row in selected if row["month"] == "2026-09" and row["calls"] and row["duration"] is not None]
        if september:
            describe(f" {model} September timed", september)
            print(f"  September response spans: >2m {sum(row['duration'] > 120 for row in september)}/{len(september)}, >5m {sum(row['duration'] > 300 for row in september)}/{len(september)}")
        for effort in ("low", "medium", "high"):
            describe(f"  {effort}", [row for row in selected if row["effort"] == effort])

    for model in MODELS:
        selected_model = [row for row in rows if row["model"] == model and row["calls"]]
        print(f"{model} model-call bands:")
        for label, selected in (
            ("1-2 calls", [row for row in selected_model if row["calls"] <= 2]),
            ("3-9 calls", [row for row in selected_model if 3 <= row["calls"] <= 9]),
            ("10+ calls", [row for row in selected_model if row["calls"] >= 10]),
        ):
            describe(label, selected)
        if selected_model:
            total_credits = sum(row["credits"] for row in selected_model)
            heavy = [row for row in selected_model if row["calls"] >= 10]
            print(f"  10+ call share of turns: {len(heavy) / len(selected_model):.1%}")
            print(f"  10+ call share of credits: {sum(row['credits'] for row in heavy) / total_credits:.1%}")
            for name in ("fresh", "cached", "output"):
                tokens = sum(row["input"] - row["cached"] if name == "fresh" else row[name] for row in selected_model)
                credits = sum(row[name + "_credits"] for row in selected_model)
                print(f"  {name}: {tokens:,} tokens, {credits:,.2f} credits")


if __name__ == "__main__":
    main()
