"""Quota recovery remains visible after replacement metadata wins."""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import run


class HandoverTests(unittest.TestCase):
    def test_automatic_quota_fallback_survives_final_metadata_and_significance_filter(self):
        primary = common.normalize_model_route("opencode", "opencode-go", "gemini-4-argon")
        replacement = common.normalize_model_route("opencode", "google", "gemini-4-argon")
        replacement_prompts = []
        def invoke(*args, **kwargs):
            route, attempt_dir = args[1], args[3]
            failed = route["provider"] == "opencode-go"
            if failed:
                (attempt_dir / "partial_result.md").write_text("Inspected input.py; patch remains.", encoding="utf-8")
            else:
                replacement_prompts.append(args[2])
                (attempt_dir / "result.md").write_text("Completed review", encoding="utf-8")
            return dict(status="quota" if failed else "ok", exit_code=1 if failed else 0,
                        error_class="quota" if failed else None,
                        error="quota exceeded" if failed else None,
                        session_id=None, usage={}, cleanup_verified=True,
                        session_cleaned_up=True)
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }), patch.object(run, "select_routes", return_value={
            "status": "ok", "selected": primary, "fallbacks": [replacement], "rationale": "test"
        }), patch.object(run, "probe_all", return_value={"agents": {"opencode": {
            "usable": True, "binary": "true", "version": "2.0.0", "recipe_flags": {}
        }}}), patch.object(run, "invoke_opencode", side_effect=invoke):
            result = run.execute_task(run_id="quota-handover", task_id="review",
                                      prompt_text="review", cwd=td)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["route_id"], replacement["route_id"])
            for filtered in (False, True):
                report = run.build_run_report("quota-handover", only_significant=filtered)
                self.assertEqual(len(report["quota_failures"]), 1)
                self.assertEqual(report["quota_failures"][0]["route_id"], primary["route_id"])
                self.assertEqual(len(report["handovers"]), 1)
                self.assertEqual(report["handovers"][0]["from_route"], primary["route_id"])
                self.assertEqual(report["handovers"][0]["to_route"], replacement["route_id"])
                self.assertIn("automatic", run.format_human_run_report(report))
                handover = report["handovers"][0]
                self.assertEqual(handover["replacement_status"], "ok")
                context = Path(handover["context_file"]).read_text()
                self.assertIn("Inspected input.py; patch remains.", context)
                self.assertIn("Inspect the current working tree", context)
                self.assertIn(context, replacement_prompts[0])
            run_dir = run.get_run_dir("quota-handover")
            events = [json.loads(line) for line in (run_dir / "manifest.jsonl").read_text().splitlines()]
            started = [event for event in events if event["event"] == "task_started"]
            self.assertEqual(started[-1]["prompt_sha256"], hashlib.sha256(replacement_prompts[0].encode()).hexdigest())
            self.assertNotIn("Quota hand-over context", (run_dir / "prompts/task-review.md").read_text())

    def test_manual_timeout_handover_is_a_decision_not_completion(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }):
            context = Path(td) / "handover.md"
            context.write_text("Completed: inspection. Remaining: patch.", encoding="utf-8")
            event = run.record_handover("manual", "old", "new",
                "antigravity/google/gemini-4-argon", "opencode/google/gemini-4-argon",
                "No reply by the 30-minute deadline", "timeout", str(context))
            self.assertEqual(event["context_file"], str(context))
            report = run.build_run_report("manual", only_significant=True)
            self.assertEqual(report["handovers"][0]["from_route"], event["from_route"])
            self.assertEqual(report["handovers"][0]["replacement_status"], "pending")
            self.assertIn("replacement: pending", run.format_human_run_report(report))
            self.assertEqual(report["task_count"], 0)
            self.assertEqual(report["participants"], [])
            self.assertIn("timeout", run.format_human_run_report(report))

    def test_failed_replacement_outcome_is_preserved_in_filtered_report(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }):
            run.record_handover("failed", "old", "new", "antigravity/google/gemini-4-argon",
                "opencode/google/gemini-4-argon", "quota exhausted", "automatic")
            run.append_manifest_event(run.get_run_dir("failed"), {
                "event": "task_finished", "task_id": "new", "route_id": "opencode/google/gemini-4-argon",
                "status": "failed", "error_class": "permission", "outcome_summary": "Required file was denied.",
            })
            report = run.build_run_report("failed", only_significant=True)
            self.assertEqual(report["handovers"][0]["replacement_status"], "failed")
            rendered = run.format_human_run_report(report)
            self.assertIn("replacement: failed", rendered)
            self.assertIn("Required file was denied.", rendered)

    def test_registered_native_replacement_outcome_uses_its_actual_route(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }):
            run.record_handover("native", "old", "new", "antigravity/google/gemini-4-argon",
                "codex/codex/gpt-6.1-sol", "quota exhausted", "automatic")
            native = run.record_native_task("native", "new", "codex", "gpt-6.1-sol",
                significant=False, outcome_summary="Inspected remaining files; no actionable findings.",
                result_text="No findings.")
            self.assertEqual(native["route_id"], "codex/codex/gpt-6.1-sol")
            for filtered in (False, True):
                report = run.build_run_report("native", only_significant=filtered)
                handover = report["handovers"][0]
                self.assertEqual(handover["replacement_status"], "ok")
                self.assertEqual(handover["replacement_outcome"], native["outcome_summary"])
                rendered = run.format_human_run_report(report)
                self.assertIn("replacement: ok", rendered)
                self.assertIn(native["outcome_summary"], rendered)
            run.record_handover("native", "old", "new", "antigravity/google/gemini-4-argon",
                "codex/openai/gpt-6.1-sol", "wrong provider must not match the native route", "automatic")
            self.assertEqual(run.build_run_report("native")["handovers"][-1]["replacement_status"], "pending")

    def test_manual_handover_rejects_invalid_identifiers_or_missing_context(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }):
            args = ["manual", "old", "new", "antigravity/google/gemini-4-argon",
                    "opencode/google/gemini-4-argon", "quota exhausted", "automatic"]
            for index, value in ((1, "../escape"), (3, "invalid"), (6, "invented")):
                bad = args.copy()
                bad[index] = value
                with self.assertRaises(ValueError):
                    run.record_handover(*bad)
            with self.assertRaises(FileNotFoundError):
                run.record_handover(*args, context_file=str(Path(td) / "missing"))

    def test_preflight_quota_rejection_is_actionable_without_force_bypass(self):
        primary = common.normalize_model_route("antigravity", "google", "gemini-4-argon")
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }), patch.object(run, "resolve_explicit_route", return_value=primary), patch.object(
            run, "load_session_pins", return_value={}
        ), patch.object(run, "collect_quota", return_value={}), patch.object(
            run, "check_route_eligibility", return_value=(
                False, "quota_exhausted:antigravity:default", {"exhausted": True}, {}
            )
        ), patch.object(run, "invoke_antigravity") as invoke:
            result = run.execute_task(run_id="preflight", task_id="review",
                route_str=primary["route_id"], prompt_text="review", cwd=td)
            self.assertEqual(result["status"], "quota")
            self.assertEqual(result["error_class"], "quota")
            self.assertIn("hand-over", result["error"])
            self.assertNotIn("--force-route", result["error"])
            invoke.assert_not_called()
            self.assertEqual(len(run.build_run_report("preflight")["quota_failures"]), 1)

    def test_record_handover_cli(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "CROSS_SUBAGENTS_CACHE_DIR": str(Path(td) / "cache")
        }), patch.object(sys, "argv", ["run.py", "record-handover", "--run-id", "cli",
             "--from-task", "old", "--to-task", "new", "--from-route",
             "antigravity/google/gemini-4-argon", "--to-route", "opencode/google/gemini-4-argon",
             "--reason", "quota exhausted", "--decision", "user"]), patch("builtins.print"):
            self.assertEqual(run.main(), 0)
            self.assertEqual(run.build_run_report("cli")["handovers"][0]["decision"], "user")


if __name__ == "__main__":
    unittest.main()
