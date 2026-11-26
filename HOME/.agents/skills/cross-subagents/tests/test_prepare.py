"""Preparation delegates selection semantics instead of reimplementing them."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common
import models
import prepare
import select_route


class TestPreparation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.state = self.root / "quota.json"
        self.state.write_text(json.dumps({"agents": {}, "buckets": {}}))
        self.catalog = {
            agent: {"usable": True, "routes": [common.normalize_model_route(agent, provider, model)]}
            for agent, provider, model in (
                ("codex", "openai", "gpt-6-astra"),
                ("claude-code", "anthropic", "claude-fable-5-1"),
                ("opencode", "github-copilot", "gemini-4-flash"),
            )
        }
        for target, value in (
            ("probe_all", {"agents": {}}), ("list_reports", []),
        ):
            mock = patch.object(prepare, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        model_patch = patch.object(prepare, "collect_all_models", side_effect=lambda **kw: copy.deepcopy(self.catalog))
        self.models = model_patch.start()
        self.addCleanup(model_patch.stop)
        selector_models = patch.object(select_route, "collect_all_models", side_effect=lambda **kw: copy.deepcopy(self.catalog))
        self.selector_models = selector_models.start()
        self.addCleanup(selector_models.stop)
        quota = patch.object(select_route, "collect_quota", return_value={"agents": {}, "buckets": {}})
        self.quota = quota.start()
        self.addCleanup(quota.stop)

    def flags(self):
        return ["--quality-floor", "0", "--quota-state", str(self.state)]

    def direct(self, flags):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = select_route.main(flags)
        return json.loads(out.getvalue()), code

    def test_selection_is_identical_to_direct_selector(self):
        flags = self.flags() + ["--exclude-vendor", "openai", "--speed", "fast"]
        with patch.object(select_route, "now_iso", return_value="2026-11-16T00:00:00Z"):
            expected, code = self.direct(flags)
            receipt, actual_code = prepare.prepare(flags)
        self.assertEqual(receipt["selection"], expected)
        self.assertEqual(actual_code, code)
        self.quota.assert_not_called()

    def test_all_selection_filters_are_forwarded(self):
        flags = self.flags() + [
            "--require-vision", "--sandbox", "read-only", "--min-context", "100000",
            "--exclude-vendor", "openai", "--include-vendor", "anthropic",
            "--max-tier", "2", "--max-cost-index", "0.1",
            "--quota-agent", "claude-code", "--exclude-quota-agent", "codex",
        ]
        with patch.object(select_route, "select_routes", return_value={"status": "no_route_available"}) as select:
            receipt, code = prepare.prepare(flags)
        self.assertEqual(code, 1)
        kw = select.call_args.kwargs
        self.assertTrue(kw["require_vision"])
        self.assertEqual(kw["sandbox_mode"], "read-only")
        self.assertEqual(kw["min_context"], 100000)
        self.assertEqual(kw["exclude_vendors"], ["openai"])
        self.assertEqual(kw["include_vendors"], ["anthropic"])
        self.assertEqual(kw["max_tier"], 2)
        self.assertEqual(kw["max_cost_index"], 0.1)
        self.assertEqual(kw["quota_agents"], ["claude-code"])
        self.assertEqual(kw["exclude_quota_agents"], ["codex"])

    def test_quota_scope_and_cache_only_survive(self):
        prepare.prepare(["--quality-floor", "0", "--pin-agent", "codex",
                         "--quota-agent", "opencode", "--quota-cache-only"])
        self.quota.assert_called_once_with(agents=[], refresh=False, cache_only=True)

    def test_refresh_discovery_does_not_refresh_injected_quota(self):
        prepare.prepare(self.flags(), refresh=True)
        self.assertTrue(self.models.call_args_list[0].kwargs["refresh"])
        self.models.assert_called_once()
        prepare.probe_all.assert_called_once_with(refresh=True)
        self.selector_models.assert_not_called()
        self.quota.assert_not_called()

    def test_latest_flag_resolves_and_saves_model_from_refreshed_catalog(self):
        latest = "claude-fable-5-2"
        self.catalog["claude-code"]["routes"].append(
            common.normalize_model_route("claude-code", "anthropic", latest)
        )
        receipt, code = prepare.prepare(self.flags() + [
            "--latest-model", "claude-fable", "--pin-agent", "claude-code",
            "--steps", "1", "--run-id", "latest-plan",
        ], refresh=True)
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["selection"]["steps"][0]["selected"]["canonical_key"], latest)
        step = select_route.load_selection_plan("latest-plan")["steps"][0]
        self.assertEqual(step["route_id"], f"claude-code/anthropic/{latest}")
        self.assertEqual(step["constraints"]["pin_model"], latest)
        self.assertTrue(self.models.call_args.kwargs["refresh"])
        self.quota.assert_not_called()

    def test_step_plan_constraints_are_persisted(self):
        flags = self.flags() + [
            "--run-id", "plan", "--steps", "2",
            "--step-spec", '{"pin_agent":"codex"}',
            "--step-spec", '{"pin_agent":"claude-code","max_tier":2}',
        ]
        receipt, code = prepare.prepare(flags)
        self.assertEqual(code, 0, receipt)
        plan = select_route.load_selection_plan("plan")
        self.assertEqual(len(plan["steps"]), 2)
        for step in receipt["selection"]["steps"]:
            saved = select_route.planned_constraints_for_route(plan, step["selected"]["route_id"])
            self.assertEqual(saved["quota_state"], {"agents": {}, "buckets": {}})
        self.assertEqual(plan["steps"][1]["constraints"]["max_tier"], 2)
        self.quota.assert_not_called()

    def test_invalid_quota_flags_fail_before_discovery(self):
        for flags in (
            ["--quota-state", str(self.state), "--refresh-quota"],
            ["--quota-cache-only", "--refresh-quota"],
            ["--quota-state", str(self.root / "missing.json")],
            ["--not-a-real-option"],
        ):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    prepare.prepare(flags, refresh=True)
                self.assertEqual(exc.exception.code, 2)
        self.models.assert_not_called()
        self.quota.assert_not_called()
        prepare.probe_all.assert_not_called()

    def test_invalid_tier_and_step_spec_fail_before_refresh(self):
        for flags in (["--max-tier", "0"], ["--steps", "0"], ["--step-spec", '{"invalid":true}']):
            with contextlib.redirect_stderr(io.StringIO()):
                receipt, code = prepare.prepare(flags, refresh=True)
            self.assertEqual(code, 2)
            self.assertEqual(receipt["status"], "invalid_arguments")
        self.models.assert_not_called()
        self.quota.assert_not_called()
        prepare.probe_all.assert_not_called()

    def test_human_output_preserves_defects_stale_and_full_selection(self):
        defect = {"severity": "major", "title": "Bad adapter", "path": "/cache/defect.md"}
        with patch.object(prepare, "probe_all", return_value={
            "agents": {"opencode": {"stale": True, "warnings": ["Requires v2"]}}
        }), patch.object(prepare, "list_reports", return_value=[defect]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = prepare.main(self.flags() + ["--human"])
        self.assertEqual(code, 0)
        for word in ("Requires v2", "stale", "Bad adapter", "/cache/defect.md"):
            self.assertIn(word, out.getvalue())
        detail = next((self.root / "preparations").glob("*.json"))
        saved = json.loads(detail.read_text())
        self.assertEqual(saved["schema_version"], common.SCHEMA_VERSION)
        self.assertIn("selected", saved["selection"])
        self.assertEqual(saved["defects"], [defect])

    def test_no_route_is_not_reported_as_success(self):
        receipt, code = prepare.prepare(self.flags() + ["--pin-model", "no-such-model"])
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "no_route_available")

    def test_exact_probe_error_is_preserved_without_second_probe(self):
        probes = {"agents": {"opencode": {
            "stale": True, "probe_error": "ORIGINAL_PROBE_DIAGNOSTIC",
            "warnings": [], "raw_initialization": "DO_NOT_STORE_RAW_INIT",
        }}}
        with patch.object(prepare, "probe_all", return_value=probes) as probe, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(prepare.main(self.flags() + ["--human"]), 0)
        probe.assert_called_once_with(refresh=False)
        self.assertIn("ORIGINAL_PROBE_DIAGNOSTIC", output.getvalue())
        detail = next((self.root / "preparations").glob("*.json")).read_text()
        self.assertIn("ORIGINAL_PROBE_DIAGNOSTIC", detail)
        self.assertNotIn("DO_NOT_STORE_RAW_INIT", detail)

    def test_failed_forced_refresh_snapshot_reused_for_single_and_plan(self):
        route = self.catalog["claude-code"]["routes"][0]
        probes = {"agents": {"claude-code": {
            "usable": True, "binary": "/fixture/claude", "version": "2.0.0",
            "config_fingerprint": "fixture", "stale": True,
            "probe_error": "FIRST_PROBE_FAILURE", "warnings": [],
        }}}
        for plan in (False, True):
            with self.subTest(plan=plan), \
                    patch.object(models, "compute_agent_config_fingerprint", return_value="fixture"), \
                    patch.object(models, "probe_all", side_effect=AssertionError("must reuse supplied probes")), \
                    patch.object(models, "collect_claude_models", return_value=([route], {})) as collect:
                # Seed a genuine matching model cache using the real collector.
                models.collect_all_models(agents=["claude-code"], refresh=True, probe_data=probes)
                collect.reset_mock()
                collect.side_effect = [RuntimeError("FIRST_REFRESH_FAILURE"), ([route], {})]
                with patch.object(prepare, "collect_all_models", side_effect=models.collect_all_models) as all_models, \
                        patch.object(prepare, "probe_all", return_value=probes) as probe:
                    flags = self.flags() + ["--pin-agent", "claude-code"]
                    if plan:
                        flags += ["--steps", "1", "--run-id", "refresh-plan"]
                    receipt, code = prepare.prepare(flags, refresh=True)
                self.assertEqual(code, 0, receipt)
                all_models.assert_called_once()
                probe.assert_called_once_with(refresh=True)
                collect.assert_called_once()
                self.selector_models.assert_not_called()
                text = json.dumps(receipt)
                self.assertIn("FIRST_PROBE_FAILURE", text)
                self.assertIn("FIRST_REFRESH_FAILURE", text)
                saved = json.loads((self.root / "models/claude-code.json").read_text())
                self.assertEqual(saved["refresh_error"], "FIRST_REFRESH_FAILURE")

    def test_direct_selector_refresh_reuses_catalog_without_callback(self):
        for plan in (False, True):
            self.selector_models.reset_mock()
            flags = self.flags() + ["--pin-agent", "codex"]
            if plan:
                flags += ["--steps", "1"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(select_route.main(flags, refresh_discovery=True), 0)
            self.selector_models.assert_called_once_with(refresh=True)

    def test_empty_supplied_snapshots_do_not_trigger_implicit_rediscovery(self):
        with patch.object(models, "probe_all", side_effect=AssertionError("must not probe")), \
                patch.object(models, "collect_agent_models", return_value={}) as collect, \
                patch.object(models, "rebuild_survey_index"):
            models.collect_all_models(agents=["codex"], probe_data={})
        collect.assert_called_once_with("codex", probe_data={}, refresh=False)
        self.selector_models.reset_mock()
        plan = select_route.select_step_plan(
            [{}], all_models={}, quota_state={"agents": {}, "buckets": {}},
        )
        self.assertEqual(plan["status"], "no_route_available")
        self.selector_models.assert_not_called()
