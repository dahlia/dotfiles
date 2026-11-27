"""Regression coverage for selection and dispatch without live model calls."""
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import quota
import run
import select_route


class QuotaRecoveryTests(unittest.TestCase):
    def test_codex_quota_ignores_null_secondary_window(self):
        route = common.normalize_model_route("codex", "openai", "gpt-6.2-sol")
        bucket_id = route["quota_bucket"]
        primary = {"used_percent": 100, "exhausted": True, "resets_at_epoch": 2000}
        buckets = {bucket_id: {"windows": {"primary": primary, "secondary": None}}}

        with patch.object(select_route.time, "time", return_value=1500) as clock:
            active = select_route.evaluate_route_quota(route, buckets, True)
            self.assertEqual(active["exhausted_buckets"], [bucket_id])
            self.assertEqual(active["used_percent"], 100)
            clock.return_value = 2000
            expired = select_route.evaluate_route_quota(route, buckets, False)
            self.assertFalse(expired["exhausted"])
            self.assertEqual(expired["status"], "unknown")

    def test_quota_uses_bucket_summary_when_windows_are_null(self):
        route = common.normalize_model_route("codex", "openai", "gpt-6.2-sol")
        bucket_id = route["quota_bucket"]
        buckets = {bucket_id: {
            "status": "ample", "used_percent": 25,
            "windows": {"primary": None, "secondary": None},
        }}

        result = select_route.evaluate_route_quota(route, buckets, False)
        self.assertEqual(result["status"], "ample")
        self.assertEqual(result["used_percent"], 25)

    def test_stale_fable_limit_applies_to_both_entrypoints_only_until_reset(self):
        fable = common.normalize_model_route("claude-code", "anthropic", "claude-fable-5-1")
        bridge = common.normalize_model_route("opencode", "claude-agent", "claude-fable-5-1")
        bridge["applicable_buckets"] = ["opencode:claude-agent"]  # old catalog
        opus = common.normalize_model_route("claude-code", "anthropic", "claude-opus-5-5")
        independent = common.normalize_model_route("opencode", "opencode", "claude-fable-5-1")
        output = json.dumps({"result": "Current week (all models): 40% used\n"
                            "Current week (Claude Fable 5.1): 100% used — resets Nov 21, 10pm (Asia/Seoul)"})
        with patch.object(quota, "run_cmd", return_value=CompletedProcess([], 0, output, "")), patch.object(
            quota, "parse_claude_reset_time", side_effect=lambda text, tz: 2000 if text else None
        ):
            data = quota.query_claude_quota("claude")
        self.assertEqual(select_route.quota_agents_for_routes({
            "opencode": {"usable": True, "routes": [bridge]}
        }), ["claude-code"])
        with patch.object(select_route.time, "time", return_value=1500) as clock:
            for route, blocked in [(fable, True), (bridge, True), (opus, False), (independent, False)]:
                self.assertEqual(select_route.evaluate_route_quota(route, data["buckets"], True)["exhausted"], blocked)
            clock.return_value = 2000
            result = select_route.evaluate_route_quota(fable, data["buckets"], False)
            self.assertFalse(result["exhausted"])
            self.assertEqual(result["effective_headroom"], 0.72)

    def test_fable_error_from_bridge_preserves_absolute_reset_and_scope(self):
        route = common.normalize_model_route("opencode", "claude-agent", "claude-fable-5-1")
        error = json.dumps({"type": "provider.unknown", "message":
                            "You've hit your Fable limit · resets Nov 21, 10pm (Asia/Seoul). "
                            "Resets at 2026-11-21T13:00:00.000Z."})
        self.assertEqual(run.classify_failure(1, "", error), "quota")
        self.assertEqual(quota.quota_observation_bucket(route, error), "claude-code:fable")
        self.assertEqual(quota._provider_reset_epoch("claude-code:fable", error, 1795000000), 1795266000)
        self.assertEqual(quota.quota_observation_bucket(route, "five_hour"), "claude-code:session")
        self.assertEqual(quota.quota_observation_bucket(route, "seven_day"), "claude-code:all-models")

    def test_antigravity_legacy_and_new_observations_hold_until_provider_reset(self):
        text = "Individual quota reached. Please upgrade your subscription. Resets in 37h56m4s."
        route = common.normalize_model_route("antigravity", "google", "gemini-4-argon-high")
        bucket = quota.quota_observation_bucket(route, text)
        self.assertEqual(bucket, "antigravity:model:gemini-4-argon")
        self.assertEqual(run.classify_failure(1, "", text + " Please wait."), "quota")
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(
            quota.time, "time", return_value=1000
        ) as clock:
            entry = quota.record_quota_observation(bucket, text)
            deadline = 1000 + 37 * 3600 + 56 * 60 + 4
            self.assertEqual(entry["backoff_until_epoch"], deadline)
            legacy = {**entry, "backoff_until_epoch": 1900, "provider_resets_at_epoch": None}
            del legacy["reset_parse_version"]
            quota.get_observations_file().write_text(json.dumps({
                "schema_version": common.SCHEMA_VERSION, "buckets": {bucket: legacy},
            }))
            clock.return_value = deadline - 1
            state = quota.overlay_observations({"buckets": {}, "agents": {}})
            self.assertTrue(select_route.evaluate_route_quota(route, state["buckets"], False)["exhausted"])
            clock.return_value = deadline
            self.assertNotIn(bucket, quota.overlay_observations({})["buckets"])

    def test_runtime_reselection_can_find_replacement_missing_from_original_chain(self):
        primary = common.normalize_model_route("opencode", "claude-agent", "claude-fable-5-1")
        replacement = common.normalize_model_route("opencode", "claude-agent", "claude-opus-5-5")
        snapshot = {"buckets": {}, "agents": {}}
        selection = [
            {"status": "ok", "selected": primary, "fallbacks": [], "rationale": "initial"},
            {"status": "ok", "selected": replacement, "fallbacks": [], "rationale": "reselected"},
        ]
        error = dict(status="quota", exit_code=1, error_class="quota", error="You've hit your Fable limit.")
        success = dict(status="ok", exit_code=0, error_class=None)
        def invoke(*args, **kwargs):
            if args[1] == replacement:
                (args[3] / "result.md").write_text("Done")
            return {**(error if args[1] == primary else success), "session_id": None, "usage": {},
                    "cleanup_verified": True, "session_cleaned_up": True}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(
            run, "select_routes", side_effect=selection
        ) as select, patch.object(run, "probe_all", return_value={"agents": {"opencode": {
            "usable": True, "binary": "true", "version": "2.0.0", "recipe_flags": {},
        }}}), patch.object(run, "invoke_opencode", side_effect=invoke):
            result = run.execute_task(run_id="recovery", task_id="review", prompt_text="review", cwd=td,
                                      quota_state=snapshot, quota_cache_only=True, require_vision=True,
                                      max_cost_index=0.2, sandbox_mode="read-only")
            self.assertEqual(result["route_id"], replacement["route_id"])
            self.assertEqual(result["status"], "ok")
            args = select.call_args.kwargs
            self.assertEqual(args["exclude_buckets"], ["claude-code:fable"])
            self.assertIn(primary["route_id"], args["exclude_route_ids"])
            self.assertIs(args["quota_state"], snapshot)
            self.assertTrue(args["quota_cache_only"])
            self.assertTrue(args["require_vision"])
            self.assertEqual(args["max_cost_index"], 0.2)
            self.assertEqual(args["sandbox_mode"], "read-only")
            self.assertNotIn("api_metered", args["allowed_billing_modes"])
            self.assertEqual(run.build_run_report("recovery")["handovers"][0]["replacement_status"], "ok")

    def test_local_auth_access_failure_falls_back_without_rejecting_account(self):
        primary = common.normalize_model_route("opencode", "deepseek", "deepseek-v4")
        backup = common.normalize_model_route("opencode", "opencode-go", "deepseek-v4")
        error = "Not logged in: could not open keyring (D-Bus permission denied)"
        error_class = run.classify_failure(1, "", error)
        self.assertEqual(error_class, "auth_access")
        self.assertEqual(run.classify_failure(1, "", "HTTP 401: token expired"), "auth")
        self.assertEqual(run.classify_failure(1, "", "sandbox: permission denied writing report"), "permission")

        def invoke(*args, **kwargs):
            route = args[1]
            metadata = {"cleanup_verified": True, "session_id": None, "usage": {},
                        "session_cleaned_up": True}
            if route == backup:
                (args[3] / "result.md").write_text("Done")
                return {"status": "ok", "exit_code": 0, **metadata}
            return {"status": "failed", "exit_code": 1, "error_class": error_class,
                    "error": error, **metadata}

        selections = [
            {"status": "ok", "selected": primary, "fallbacks": [], "rationale": "test selection"},
            {"status": "ok", "selected": backup, "fallbacks": [], "rationale": "test selection"},
        ]
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(
            run, "select_routes", side_effect=selections
        ) as select, patch.object(run, "probe_all", return_value={"agents": {"opencode": {
            "usable": True, "binary": "opencode", "version": "2.0.0",
        }}}), patch.object(run, "invoke_opencode", side_effect=invoke) as dispatch, patch.object(
            run, "record_auth_rejection"
        ) as reject:
            result = run.execute_task(run_id="worker", task_id="review", cwd=td, prompt_text="review",
                                      sandbox_mode="read-only", quota_state={"buckets": {}, "agents": {}})
            self.assertEqual(result["route_id"], backup["route_id"])
            self.assertEqual(result["status"], "ok", result.get("error"))
            self.assertEqual(select.call_args.kwargs["exclude_buckets"], [primary["quota_bucket"]])
            self.assertTrue(all(call.args[7] == "read-only" for call in dispatch.call_args_list))
            reject.assert_not_called()

    def test_explicit_route_blocked_at_preflight_is_never_invoked(self):
        primary = common.normalize_model_route("antigravity", "google", "gemini-4-argon-high", auth_plan="subscription")
        replacement = common.normalize_model_route("opencode", "google", "gemini-4-argon", auth_plan="subscription")
        bucket = "antigravity:model:gemini-4-argon"
        snapshot = {"buckets": {bucket: {"exhausted": True}}, "agents": {}}
        models = {r["agent"]: {"usable": True, "routes": [r]} for r in (primary, replacement)}

        def invoke(*args, **kwargs):
            (args[3] / "result.md").write_text("Done")
            return dict(status="ok", exit_code=0, session_id=None, usage={},
                        cleanup_verified=True, session_cleaned_up=True)

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(
            run, "resolve_explicit_route", return_value=primary
        ), patch.object(select_route, "collect_all_models", return_value=models), patch.object(
            select_route, "route_unavailability_reason", return_value=None
        ), patch.object(select_route, "get_survey", return_value=None), patch.object(
            run, "probe_all", return_value={"agents": {"opencode": {
                "usable": True, "binary": "true", "version": "2.0.0", "recipe_flags": {},
            }}}
        ), patch.object(run, "invoke_antigravity") as agy, patch.object(run, "invoke_opencode", side_effect=invoke):
            result = run.execute_task(run_id="preflight-recovery", task_id="review", prompt_text="review",
                                      route_str=primary["route_id"], cwd=td, quota_state=snapshot)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["route_id"], replacement["route_id"])
            agy.assert_not_called()
            report = run.build_run_report("preflight-recovery")
            self.assertEqual(report["quota_failures"][0]["route_id"], primary["route_id"])
            self.assertEqual(report["handovers"][0]["replacement_status"], "ok")


if __name__ == "__main__":
    unittest.main()
