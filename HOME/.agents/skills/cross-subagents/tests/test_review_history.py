"""Learn from recent, comparable review receipts without invoking any CLI."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import review_history
import select_route


class ReviewHistoryTests(unittest.TestCase):
    def test_recent_receipts_exclude_resumes_and_other_failures(self):
        stamp = datetime.now(timezone.utc).isoformat()
        base = dict(event="task_finished", task_category="code_review", ended_at=stamp,
                    route_id="a/b/c", task_id="initial", status="ok", duration_ms=160000)
        records = [
            base, {**base, "duration_ms": 1},
            {**base, "task_id": "timeout", "status": "timeout", "duration_ms": 600000},
            {**base, "task_id": "quota", "status": "quota"},
            {**base, "task_id": "resumed", "resumed": True},
            {**base, "task_id": "unsafe", "cleanup_verified": False},
            {**base, "task_id": "blocked", "denial_count": 1},
            {**base, "task_id": "general", "task_category": "general"},
            {**base, "task_id": "old", "ended_at": "2000-01-01T00:00:00Z"},
            {**base, "task_id": "step1-r1", "task_category": None, "task_summary": "Legacy code review"},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            path.mkdir()
            (path / "manifest.jsonl").write_text("broken line\n" + "\n".join(map(json.dumps, records)))
            self.assertEqual(review_history.load_review_history(Path(td)), {
                "a/b/c": {"samples": 3, "timeouts": 1, "median_completion_seconds": 160.0},
            })

    def test_slow_timeout_history_changes_ranking_not_pins_or_other_tasks(self):
        a, b = [common.normalize_model_route("opencode", provider, "gemini-4-argon")
                for provider in ("provider-a", "provider-b")]
        models = {"opencode": {"usable": True, "routes": [a, b]}}
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "route_unavailability_reason", return_value=None
        ):
            def choose(task, history, **kwargs):
                return select_route.select_routes(task, all_models=models, quota_state={}, use_saved_pins=False,
                                                  review_history=history, speed="fast", **kwargs)["selected"]
            baseline = choose("code_review", {})
            other = next(r for r in (a, b) if r["route_id"] != baseline["route_id"])
            history = {
                baseline["route_id"]: {"samples": 20, "timeouts": 10, "median_completion_seconds": 590},
                other["route_id"]: {"samples": 20, "timeouts": 0, "median_completion_seconds": 100},
            }
            result = choose("code_review", history)
            self.assertEqual(result["route_id"], other["route_id"])
            self.assertIn("0/20 timeouts", result["rationale"])
            self.assertEqual(choose("code_review", history, pin_provider=baseline["provider"])["route_id"],
                             baseline["route_id"])
            self.assertEqual(choose("general", history)["route_id"], choose("general", {})["route_id"])
