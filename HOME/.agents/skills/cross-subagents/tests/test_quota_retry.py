"""Provider retry deadlines must survive quota observation and expiry."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import quota
import common
import run


class QuotaRetryTests(unittest.TestCase):
    def record(self, text, reset_seconds=None):
        with tempfile.TemporaryDirectory() as cache, patch.dict(
            os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": cache}
        ), patch.object(quota.time, "time", return_value=1000):
            return quota.record_quota_observation("opencode:opencode-go", text, reset_seconds)

    def test_go_error_preserves_provider_deadline(self):
        error = {"type": "provider.go-limit", "status": 429, "retryAfterMs": 495000}
        # The adapter combines stderr and serialized error objects on separate lines.
        entry = self.record("CLI diagnostic\n" + json.dumps(error))
        self.assertEqual(entry["provider_resets_at_epoch"], 1495)
        self.assertEqual(entry["backoff_until_epoch"], 1495)
        self.assertEqual(entry["resets_at_epoch"], 1495)

    def test_wrapped_error_rounds_up_fractional_seconds(self):
        entry = self.record(json.dumps({"type": "error", "error": {"retryAfterMs": 1501}}))
        self.assertEqual(entry["provider_resets_at_epoch"], 1002)

    def test_subscription_becomes_available_at_provider_retry_deadline(self):
        with tempfile.TemporaryDirectory() as cache, patch.dict(
            os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": cache}
        ), patch.object(quota.time, "time", return_value=1000) as clock:
            quota.record_quota_observation("opencode:opencode-go", '{"retryAfterMs":495000}')
            clock.return_value = 1494
            limited = quota.overlay_observations({"buckets": {}, "agents": {}})
            self.assertTrue(limited["buckets"]["opencode:opencode-go"]["exhausted"])
            clock.return_value = 1495
            available = quota.overlay_observations(limited)
            self.assertFalse(available["buckets"]["opencode:opencode-go"].get("exhausted", False))

    def test_explicit_reset_overrides_structured_delay(self):
        entry = self.record('{"retryAfterMs":495000}', 60)
        self.assertEqual(entry["provider_resets_at_epoch"], 1060)

    def test_unknown_or_invalid_delay_keeps_synthetic_backoff_separate(self):
        for value in (None, True, -1, "495000", float("inf"), float("nan")):
            with self.subTest(value=value):
                entry = self.record(json.dumps({"retryAfterMs": value}))
                self.assertIsNone(entry["provider_resets_at_epoch"])
                self.assertEqual(entry["backoff_until_epoch"], 1900)

    def test_zero_delay_gets_minimum_backoff(self):
        entry = self.record('{"retryAfterMs":0}')
        self.assertEqual(entry["provider_resets_at_epoch"], 1001)

    def test_execute_task_retains_retry_after_beyond_display_error_limit(self):
        route = common.normalize_model_route("opencode", "opencode-go", "gemini-4-argon")
        provider_error = json.dumps({
            "type": "provider.go-limit", "status": 429,
            "message": "OpenCode Go usage limit reached " + "x" * 600,
            "retryAfterMs": 495000,
        })
        self.assertNotIn("retryAfterMs", provider_error[:500])
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")}
        ), patch.object(run, "select_routes", return_value={
            "status": "ok", "selected": route, "fallbacks": [], "rationale": "test selection",
        }), patch.object(run, "probe_all", return_value={"agents": {"opencode": {
            "usable": True, "binary": "true", "version": "2.0.0", "recipe_flags": {},
        }}}), patch.object(run, "invoke_opencode", return_value={
            "status": "quota", "exit_code": 1, "error_class": "quota",
            "error": provider_error[:500], "warnings": [provider_error],
            "session_id": None, "usage": {}, "cleanup_verified": True,
            "session_cleaned_up": True,
        }), patch.object(quota.time, "time", return_value=1000):
            result = run.execute_task(
                run_id="structured-quota", task_id="review", prompt_text="review", cwd=td,
                sandbox_mode="workspace-write", no_fallback=True,
            )
            self.assertNotEqual(result["status"], "ok")
            persisted = json.loads(quota.get_observations_file().read_text())
            self.assertEqual(
                persisted["buckets"][route["quota_bucket"]]["provider_resets_at_epoch"], 1495
            )


if __name__ == "__main__":
    unittest.main()
