import json
import tempfile
import unittest
from pathlib import Path

from codex_cost_dashboard.monitor import PromptRun, SessionState, consume_event, read_events


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


if __name__ == "__main__":
    unittest.main()
