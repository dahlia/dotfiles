"""Account windows must exclude exhausted Antigravity routes before dispatch."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common
import quota
import select_route


class AntigravityQuotaTests(unittest.TestCase):
    def response(self):
        return [{"provider": "antigravity", "rateWindowLabels": {
            "primary": "Gemini Models", "secondary": "Claude and GPT",
        }, "usage": {
            "updatedAt": "2026-11-12T15:36:52Z",
            "primary": {"usedPercent": 6.7, "resetsAt": "2026-11-14T05:27:45Z"},
            "secondary": {"usedPercent": 100, "resetsAt": "2026-11-18T15:26:20Z"},
            "extraRateWindows": [
                {"id": "gemini-5h", "title": "Gemini 5-hour", "window": {"usedPercent": 0}},
                {"id": "third-party-5h", "title": "Claude/GPT 5-hour", "usageKnown": False,
                 "window": {"usedPercent": 0}},
                {"id": "argon-weekly", "title": "Gemini 4 Argon weekly", "window": {
                    "usedPercent": 100, "resetsAt": "2026-11-14T05:26:57Z"}},
            ],
        }}]

    def parse(self, response=None):
        with patch.object(quota, "parse_iso_age_seconds", return_value=0), patch.object(quota.time, "time", return_value=1794497812):
            return quota.parse_antigravity_quota(response or self.response())

    def test_model_and_group_limits_preserve_independent_opencode_route(self):
        state = self.parse()
        for agent, provider, model, expected in [
            ("antigravity", "google", "gemini-4-argon-high", True),
            ("antigravity", "google", "gemini-4-argon-low", True),
            ("antigravity", "google", "gemini-3.8-flash-high", False),
            ("antigravity", "anthropic", "claude-opus-5-5-high", True),
            ("antigravity", "openai", "gpt-oss-120b", True),
            ("opencode", "google", "gemini-4-argon", False),
        ]:
            with self.subTest(agent=agent, model=model):
                route = common.normalize_model_route(agent, provider, model)
                route["applicable_buckets"] = [route["quota_bucket"]]  # legacy catalog
                self.assertEqual(select_route.evaluate_route_quota(route, state["buckets"], False)["exhausted"], expected)
        self.assertEqual(state["buckets"]["antigravity:group:gemini"]["used_percent"], 6.7)
        self.assertNotIn("third-party-5h", state["buckets"]["antigravity:group:third-party"]["windows"])

    def test_selection_uses_opencode_for_exhausted_argon(self):
        antigravity = common.normalize_model_route("antigravity", "google", "gemini-4-argon-high")
        opencode = common.normalize_model_route("opencode", "google", "gemini-4-argon")
        catalog = {agent: {"usable": True, "routes": [route]} for agent, route in [
            ("antigravity", antigravity), ("opencode", opencode),
        ]}
        state = self.parse()
        with patch.object(select_route, "get_survey", return_value=None), patch.object(select_route, "route_unavailability_reason", return_value=None):
            selection = select_route.select_routes(
                task="general", pin_model="gemini-4-argon", all_models=catalog,
                quota_state={"agents": {"antigravity": state}, "buckets": state["buckets"]},
                use_saved_pins=False,
            )
        self.assertEqual(selection["status"], "ok")
        self.assertEqual(selection["selected"]["route_id"], opencode["route_id"])

    def test_exhausted_short_window_survives_ample_weekly(self):
        response = self.response()
        response[0]["usage"]["extraRateWindows"][0]["window"]["usedPercent"] = 100
        state = self.parse(response)
        self.assertTrue(state["buckets"]["antigravity:group:gemini"]["exhausted"])

    def test_stale_missing_and_ambiguous_source_are_not_fresh_readings(self):
        for age in (None, 900, 3600, -120):
            with self.subTest(age=age), patch.object(quota, "parse_iso_age_seconds", return_value=age):
                with self.assertRaises(RuntimeError):
                    quota.parse_antigravity_quota(self.response())
        with self.assertRaises(RuntimeError):
            quota.parse_antigravity_quota(self.response() * 2)

    def test_missing_percent_is_unknown(self):
        response = self.response()
        del response[0]["usage"]["secondary"]["usedPercent"]
        state = self.parse(response)
        self.assertEqual(state["buckets"]["antigravity:group:third-party"]["status"], "unknown")

    def test_refresh_failure_does_not_replenish_before_known_reset(self):
        state = self.parse()
        route = common.normalize_model_route("antigravity", "google", "gemini-4-argon-high")
        with patch.object(select_route.time, "time", return_value=1794497812):
            self.assertTrue(select_route.evaluate_route_quota(route, state["buckets"], True)["exhausted"])
        with patch.object(select_route.time, "time", return_value=1900000000):
            self.assertFalse(select_route.evaluate_route_quota(route, state["buckets"], True)["exhausted"])

    def test_observation_does_not_erase_provider_reset(self):
        baseline = self.parse()["buckets"]["antigravity:model:gemini-4-argon"]
        observed = quota._apply_observation_to_bucket(baseline, {
            "backoff_until_epoch": 1794498800, "provider_resets_at_epoch": None,
        }, 1794497812)
        self.assertEqual(observed["resets_at_epoch"], baseline["resets_at_epoch"])

    def test_recent_collection_does_not_extend_source_ttl(self):
        entry = {"method": "codexbar:antigravity", "collected_at": "recent", "source_updated_at": "old"}
        with patch.object(quota, "parse_iso_age_seconds", side_effect=lambda value: 901 if value == "old" else 0):
            self.assertFalse(quota._agent_quota_is_fresh("antigravity", entry))

    def test_collect_refreshes_legacy_baseline_and_retains_failure_as_stale(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(quota, "compute_agent_config_fingerprint", return_value="test"), patch.object(quota, "probe_all", return_value={"agents": {"antigravity": {"binary": "agy", "version": "1", "usable": True}}}), patch.object(quota, "query_antigravity_quota", return_value=self.parse()) as query:
            initial = quota.collect_quota(agents=["antigravity"], refresh=True)
            self.assertEqual(initial["agents"]["antigravity"]["method"], "codexbar:antigravity")
            query.side_effect = RuntimeError("quota unavailable")
            failed = quota.collect_quota(agents=["antigravity"], refresh=True)
            self.assertTrue(failed["agents"]["antigravity"]["stale"])
            self.assertTrue(failed["buckets"]["antigravity:model:gemini-4-argon"]["exhausted"])
            legacy = {"method": "observation-fallback", "collected_at": common.now_iso()}
            self.assertFalse(quota._agent_quota_is_fresh("antigravity", legacy))


if __name__ == "__main__":
    unittest.main()
