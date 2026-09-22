import base64
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from codex_cost_dashboard.dashboard import (
    DashboardServer,
    HTML,
    local_account,
    prompt_history_preview,
    prompt_input_tags,
    short_local_path,
)
from codex_cost_dashboard.monitor import PromptRun
from codex_cost_dashboard.openai_updates import check_official_updates


def encode_segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def fixture_log(path):
    records = [
        {
            "timestamp": "2026-01-02T08:00:00Z",
            "type": "session_meta",
            "payload": {"id": "fixture-session", "cwd": "/example/project", "thread_source": "user"},
        },
        {
            "timestamp": "2026-01-02T08:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Review the sample"},
        },
        {
            "timestamp": "2026-01-02T08:00:02Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra", "effort": "medium"},
        },
        {
            "timestamp": "2026-01-02T08:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                    },
                    "model_context_window": 1000,
                },
                "rate_limits": {"primary": {"used_percent": 25, "resets_at": 1_800_000_000}},
            },
        },
        {
            "timestamp": "2026-01-02T08:00:04Z",
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


class PreviewTests(unittest.TestCase):
    def test_link_appears_once_as_short_clickable_tag(self):
        url = "https://example.test/very/long/path"
        text = f"[{url}]({url}) please review the result"
        prompt = PromptRun(number=1, text=text, started_at="2026-01-02T08:00:00Z")
        self.assertEqual(prompt_history_preview(text), "please review the result")
        self.assertEqual(
            prompt_input_tags(prompt),
            [{"kind": "link", "label": "example.test", "href": url}],
        )

    def test_windows_paths_are_compacted_and_tagged(self):
        text = r"[$notes](C:\Users\person\.codex\skills\notes\SKILL.md) Check C:\Users\person\work\report.pdf"
        prompt = PromptRun(number=1, text=text, started_at="2026-01-02T08:00:00Z")
        preview = prompt_history_preview(text)
        tags = prompt_input_tags(prompt)
        self.assertIn("…/work/report.pdf", preview)
        self.assertEqual(tags[0], {"kind": "skill", "label": "Skill · notes"})
        self.assertEqual(tags[1], {"kind": "file", "label": "1 local file"})

    def test_short_local_path_accepts_both_separators(self):
        self.assertEqual(short_local_path("/Users/person/project/report.md"), "…/project/report.md")
        self.assertEqual(short_local_path(r"C:\Users\person\project\report.md"), "…/project/report.md")

    def test_html_keeps_expected_dashboard_views(self):
        self.assertIn("Global / Plan", HTML)
        self.assertIn("Task / Session", HTML)
        self.assertIn("Prompt history", HTML)
        self.assertIn("Current Codex account", HTML)
        self.assertIn("openCommands", HTML)
        self.assertIn("tool-category", HTML)
        self.assertIn("tool-outcome", HTML)
        self.assertIn("Check official updates", HTML)
        self.assertIn("/api/updates", HTML)


class AccountTests(unittest.TestCase):
    def test_account_claims_are_decoded_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            token = ".".join(
                [
                    encode_segment({"alg": "none"}),
                    encode_segment(
                        {
                            "email": "person@example.test",
                            "name": "Example Person",
                            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                        }
                    ),
                    "signature",
                ]
            )
            path.write_text(json.dumps({"tokens": {"id_token": token}}), encoding="utf-8")
            self.assertEqual(
                local_account(path),
                {"email": "person@example.test", "name": "Example Person", "plan": "plus"},
            )

    def test_missing_auth_is_safe(self):
        self.assertEqual(
            local_account(Path("definitely-missing-auth.json")),
            {"email": None, "name": None, "plan": None},
        )


class OfficialUpdateTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, content):
            self.content = content
            self.headers = {"ETag": '"fixture"', "Last-Modified": "Tue, 23 Sep 2026 00:00:00 GMT"}

        def read(self):
            return self.content

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def test_check_establishes_a_baseline_then_detects_a_document_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updates.json"
            with patch("codex_cost_dashboard.openai_updates.urlopen", side_effect=[self.FakeResponse(b"pricing v1"), self.FakeResponse(b"models v1")]):
                first = check_official_updates(path, force=True)
            self.assertTrue(first["baseline_established"])
            self.assertFalse(first["changed"])
            with patch("codex_cost_dashboard.openai_updates.urlopen", side_effect=[self.FakeResponse(b"pricing v2"), self.FakeResponse(b"models v1")]):
                second = check_official_updates(path, force=True)
            self.assertTrue(second["changed"])
            self.assertFalse(second["baseline_established"])


class ServerSmokeTests(unittest.TestCase):
    def test_local_server_serves_dashboard_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            log = sessions / "2026" / "01" / "02" / "fixture.jsonl"
            fixture_log(log)
            server = DashboardServer(("127.0.0.1", 0), sessions, root / "state.json", 0.04)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertIn(b"Prompt history", response.read())
                with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=3) as response:
                    payload = json.load(response)
                self.assertEqual(payload["session_id"], "fixture-session")
                self.assertEqual(payload["prompt_count"], 1)
                self.assertEqual(payload["plan"]["used_percent"], 25)
                self.assertEqual(payload["account"]["email"], None)
                self.assertEqual(payload["rate_card"]["version"], "2026-09-23")
                self.assertFalse(payload["updates"]["enabled"])
            finally:
                server.shutdown()
                server.server_close()
                server.close_monitor()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
