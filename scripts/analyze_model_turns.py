#!/usr/bin/env python3
"""Print aggregate, prompt-free statistics for two models in local Codex logs.

Run with PYTHONPATH=src python scripts/analyze_model_turns.py. This reads logs
but prints no prompt text, task names, account data, or session identifiers.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

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
                "raw_duration": signed_duration(prompt.started_at, prompt.completed_at),
                "tool_timestamp_span": tool_timestamp_span(prompt.tools),
                "started": prompt.started_at,
                "month": str(prompt.started_at or "")[:7],
            })
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    print("Recent seven-day task rollup (metered, completed prompts only):")
    for model in MODELS:
        recent = []
        for row in rows:
            try:
                started = datetime.fromisoformat(str(row["started"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if row["model"] == model and row["calls"] and started >= cutoff:
                recent.append(row)
        tasks: dict[str, list[dict]] = defaultdict(list)
        for row in recent:
            tasks[row["task"]].append(row)
        print(f"  {model}: {len(recent)} prompts, {len(tasks)} tasks")
        if tasks:
            for name in ("credits", "calls", "tools", "input", "cached", "duration"):
                totals = [sum((row[name] or 0) for row in task_rows) for task_rows in tasks.values()]
                print(f"    {name}/task: median {quantile(totals, .5):,.1f}, p75 {quantile(totals, .75):,.1f}, max {max(totals):,.1f}, total {sum(totals):,.1f}")
            prompts_per_task = [len(task_rows) for task_rows in tasks.values()]
            print(f"    prompts/task: median {quantile(prompts_per_task, .5):,.1f}, p75 {quantile(prompts_per_task, .75):,.1f}, max {max(prompts_per_task)}")
            dominant = max(tasks.values(), key=lambda task_rows: sum(row["credits"] for row in task_rows))
            dominant_credits = sum(row["credits"] for row in dominant)
            print(f"    highest-credit task: {len(dominant)} prompts, {sum(row['calls'] for row in dominant)} model calls, {sum(row['tools'] for row in dominant)} tool calls, {dominant_credits:,.1f} credits ({dominant_credits / sum(row['credits'] for row in recent):.1%} of model credits)")
    print("All-history task rollup (metered, completed prompts only; timing excluded):")
    for model in MODELS:
        selected = [row for row in rows if row["model"] == model and row["calls"]]
        tasks: dict[str, list[dict]] = defaultdict(list)
        for row in selected:
            tasks[row["task"]].append(row)
        print(f"  {model}: {len(selected)} prompts, {len(tasks)} tasks")
        if tasks:
            for name in ("credits", "calls", "tools", "input", "cached"):
                totals = [sum(row[name] for row in task_rows) for task_rows in tasks.values()]
                print(f"    {name}/task: median {quantile(totals, .5):,.1f}, p75 {quantile(totals, .75):,.1f}, p90 {quantile(totals, .9):,.1f}, max {max(totals):,.1f}")
            turns_per_task = [len(task_rows) for task_rows in tasks.values()]
            print(f"    prompts/task: median {quantile(turns_per_task, .5):,.1f}, p75 {quantile(turns_per_task, .75):,.1f}, p90 {quantile(turns_per_task, .9):,.1f}, max {max(turns_per_task)}")
            call_totals = [sum(row["calls"] for row in task_rows) for task_rows in tasks.values()]
            credit_totals = [sum(row["credits"] for row in task_rows) for task_rows in tasks.values()]
            print(f"    tasks with 100+ calls: {sum(value >= 100 for value in call_totals)}/{len(tasks)}; 500+ credits: {sum(value >= 500 for value in credit_totals)}/{len(tasks)}")
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
            active_by_task: dict[str, float] = defaultdict(float)
            turns_by_task: Counter[str] = Counter()
            for row in september:
                active_by_task[row["task"]] += row["duration"]
                turns_by_task[row["task"]] += 1
            print(f"  September active time per task: median {quantile(list(active_by_task.values()), .5):.1f}s, p75 {quantile(list(active_by_task.values()), .75):.1f}s; median turns/task {quantile(list(turns_by_task.values()), .5):.1f}")
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
