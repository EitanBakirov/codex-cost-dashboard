import json
import tempfile
import unittest
from pathlib import Path

from codex_cost_dashboard.monitor import (
    MODEL_RATES,
    PromptRun,
    SessionFollower,
    SessionState,
    consume_event,
    describe_tool_call,
    is_primary_session,
    read_events,
    normalize_model,
)


def event(timestamp, event_type, payload):
    return {"timestamp": timestamp, "type": event_type, "payload": payload}


class MonitorParserTests(unittest.TestCase):
    def setUp(self):
        self.state = SessionState(path=Path("example.jsonl"))

    def test_modern_prompt_collects_usage_and_completion(self):
        consume_event(
            self.state,
            event(
                "2026-01-02T08:00:00Z",
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Explain this code"},
                        {"type": "input_image", "image_url": "data:image/png;base64,fixture"},
                    ],
                },
            ),
        )
        consume_event(
            self.state,
            event(
                "2026-01-02T08:00:01Z",
                "turn_context",
                {"model": "gpt-5.6-terra", "effort": "medium", "turn_id": "turn-1"},
            ),
        )
        consume_event(
            self.state,
            event(
                "2026-01-02T08:00:02Z",
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 20,
                        },
                        "model_context_window": 1000,
                    },
                    "rate_limits": {"primary": {"used_percent": 12, "resets_at": 1_800_000_000}},
                },
            ),
        )
        consume_event(
            self.state,
            event("2026-01-02T08:00:03Z", "event_msg", {"type": "task_complete"}),
        )

        prompt = self.state.prompts[0]
        self.assertEqual(prompt.text, "Explain this code")
        self.assertEqual(prompt.input_images, 1)
        self.assertEqual(prompt.model, "gpt-5.6-terra")
        self.assertEqual(prompt.effort, "medium")
        self.assertTrue(prompt.completed)
        self.assertEqual(prompt.meter.usage.input_tokens, 100)
        self.assertEqual(prompt.meter.usage.cached_input_tokens, 80)
        self.assertEqual(prompt.meter.usage.output_tokens, 20)
        self.assertEqual(self.state.plan_used_percent, 12)

    def test_record_and_legacy_mirror_count_once_with_repeated_snapshot(self):
        consume_event(self.state, event("2026-01-02T08:00:00Z", "event_msg", {"type": "user_message", "message": "Check"}))
        usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}
        consume_event(self.state, event("2026-01-02T08:00:01Z", "token_usage_record", {"response_id": "r1", "usage": usage}))
        count = {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}
        consume_event(self.state, event("2026-01-02T08:00:02Z", "event_msg", count))
        consume_event(self.state, event("2026-01-02T08:00:03Z", "event_msg", count))
        consume_event(self.state, event("2026-01-02T08:00:04Z", "token_usage_record", {"response_id": "r1", "usage": usage}))
        self.assertEqual(self.state.task.model_calls, 1)
        self.assertEqual(self.state.prompts[0].meter.usage.input_tokens, 100)

    def test_automatic_usage_is_separate_and_fragment_reset_is_valid(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}
        for minute in (0, 1):
            consume_event(self.state, event(f"2026-01-02T08:0{minute}:00Z", "session_meta", {"id": "same-task"}))
            consume_event(self.state, event(f"2026-01-02T08:0{minute}:01Z", "event_msg", {
                "type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage},
            }))
        self.assertEqual(self.state.task.model_calls, 2)
        self.assertEqual(self.state.background.model_calls, 2)

    def test_mirrored_legacy_message_is_not_a_second_prompt(self):
        timestamp = "2026-01-02T08:00:00Z"
        consume_event(
            self.state,
            event(
                timestamp,
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Read this\n"},
                        {"type": "input_text", "text": "carefully"},
                    ],
                },
            ),
        )
        consume_event(
            self.state,
            event(timestamp, "event_msg", {"type": "user_message", "message": "Read this carefully"}),
        )
        self.assertEqual(len(self.state.prompts), 1)

    def test_repeated_prompt_at_a_later_time_is_preserved(self):
        for timestamp in ("2026-01-02T08:00:00Z", "2026-01-02T08:01:00Z"):
            consume_event(
                self.state,
                event(timestamp, "event_msg", {"type": "user_message", "message": "Try again"}),
            )
        self.assertEqual(len(self.state.prompts), 2)

    def test_injected_skill_and_internal_context_are_not_prompts(self):
        records = [
            event(
                "2026-01-02T08:00:00Z",
                "response_item",
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<recommended_plugins>none</recommended_plugins>"}]},
            ),
            event(
                "2026-01-02T08:00:01Z",
                "response_item",
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[$notes](/tmp/notes/SKILL.md) Save this idea"}]},
            ),
            event(
                "2026-01-02T08:00:02Z",
                "response_item",
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<skill><name>notes</name><instructions>private implementation</instructions></skill>"}]},
            ),
        ]
        read_events((json.dumps(record) for record in records), self.state)
        self.assertEqual([prompt.text for prompt in self.state.prompts], ["[$notes](/tmp/notes/SKILL.md) Save this idea"])

    def test_aborted_prompt_gets_a_fixed_end_time(self):
        consume_event(
            self.state,
            event("2026-01-02T08:00:00Z", "event_msg", {"type": "user_message", "message": "Stop quickly"}),
        )
        consume_event(
            self.state,
            event("2026-01-02T08:00:04Z", "event_msg", {"type": "turn_aborted"}),
        )
        prompt = self.state.prompts[0]
        self.assertTrue(prompt.completed)
        self.assertEqual(prompt.completed_at, "2026-01-02T08:00:04Z")

    def test_continuation_files_with_one_session_id_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            session_id = "one-logical-task"
            first.write_text("\n".join(json.dumps(record) for record in [
                event("2026-01-02T08:00:00Z", "session_meta", {"session_id": session_id, "timestamp": "2026-01-02T08:00:00Z"}),
                event("2026-01-02T08:00:01Z", "event_msg", {"type": "user_message", "message": "Earlier prompt"}),
                event("2026-01-02T08:00:02Z", "event_msg", {"type": "task_complete"}),
            ]), encoding="utf-8")
            second.write_text("\n".join(json.dumps(record) for record in [
                event("2026-01-02T09:00:00Z", "session_meta", {"session_id": session_id, "timestamp": "2026-01-02T09:00:00Z"}),
                event("2026-01-02T09:00:01Z", "event_msg", {"type": "user_message", "message": "Later prompt"}),
            ]), encoding="utf-8")
            follower = SessionFollower(root, second)
            try:
                self.assertEqual([prompt.text for prompt in follower.refresh().prompts], ["Earlier prompt", "Later prompt"])
            finally:
                follower.close()

    def test_subagent_source_is_not_a_primary_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.jsonl"
            path.write_text(json.dumps(event("2026-01-02T08:00:00Z", "session_meta", {"source": {"subagent": {"other": "guardian"}}})), encoding="utf-8")
            self.assertFalse(is_primary_session(path))

    def test_shell_tool_call_has_a_scannable_intent(self):
        tool = describe_tool_call(
            {
                "type": "custom_tool_call",
                "name": "exec_command",
                "input": 'await tools.exec_command({"cmd":"rg -n TODO src"})',
            }
        )
        self.assertEqual(tool["category"], "Search")
        self.assertEqual(tool["action"], "Search project files · src")
        self.assertEqual(tool["command"], "rg -n TODO src")

    def test_shell_intents_distinguish_tests_git_changes_and_installation(self):
        cases = [
            ("pytest tests", "Test", "Run project tests"),
            ("git status", "Git", "Inspect Git state"),
            ("git commit -m fix", "Git change", "Change local Git state"),
            ("python -m pip install -e .", "Install", "Install local dependencies"),
        ]
        for command, category, action in cases:
            with self.subTest(command=command):
                tool = describe_tool_call(
                    {
                        "type": "custom_tool_call",
                        "name": "exec_command",
                        "input": f'await tools.exec_command({{"cmd":{json.dumps(command)}}})',
                    }
                )
                self.assertEqual(tool["category"], category)
                self.assertEqual(tool["action"], action)

    def test_current_credit_rate_card_and_daybreak_aliases_are_known(self):
        self.assertEqual(MODEL_RATES["gpt-6-sol"], (50.0, 5.0, 250.0))
        self.assertEqual(MODEL_RATES["gpt-5.6-terra"], (50.0, 5.0, 300.0))
        self.assertEqual(normalize_model("gpt-daybreak-blue-latest"), "gpt-5.6-sol")
        self.assertEqual(normalize_model("gpt-daybreak-red-latest"), "gpt-5.6-cyber")


if __name__ == "__main__":
    unittest.main()
