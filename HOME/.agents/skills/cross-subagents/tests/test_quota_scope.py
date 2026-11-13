"""Quota ownership must survive parsing, route planning and stale caches."""
import contextlib
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
import quota
import select_route


class QuotaOwnershipTests(unittest.TestCase):
    def setUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        env = patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": cache.name})
        env.start()
        self.addCleanup(env.stop)

    def catalog(self):
        return {
            agent: {"usable": True, "routes": [common.normalize_model_route(agent, provider, model)]}
            for agent, provider, model in (
                ("codex", "openai", "gpt-6-astra"),
                ("claude-code", "anthropic", "claude-fable-5-1"),
                ("opencode", "github-copilot", "gemini-4-flash"),
            )
        }

    def test_status_flags_before_after_or_without_subcommand_preserve_agent_and_refresh(self):
        for flags in (
            ["--agent", "claude-code", "--refresh", "status"],
            ["status", "--agent", "claude-code", "--refresh"],
            ["--agent", "claude-code", "--refresh"],
        ):
            with self.subTest(flags=flags), patch.object(sys, "argv", ["quota.py", *flags]), patch.object(
                quota, "collect_quota", return_value={}
            ) as collect, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(quota.main(), 0)
                collect.assert_called_once_with(agents=["claude-code"], refresh=True)

    def test_agent_filters_on_both_sides_are_combined(self):
        with patch.object(sys, "argv", ["quota.py", "--agent", "claude-code", "status", "--agent", "opencode"]), patch.object(
            quota, "collect_quota", return_value={}
        ) as collect, contextlib.redirect_stdout(io.StringIO()):
            quota.main()
        collect.assert_called_once_with(agents=["claude-code", "opencode"], refresh=False)

    def test_cache_only_never_probes_and_marks_expired_snapshot_stale(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}):
            path = Path(td) / "quota" / "status.json"
            path.parent.mkdir()
            original = {"schema_version": common.SCHEMA_VERSION, "collected_at": "2000-01-01T00:00:00Z", "agents": {
                "codex": {"usable": True, "collected_at": "2000-01-01T00:00:00Z", "buckets": {}}
            }, "buckets": {}}
            path.write_text(json.dumps(original))
            with patch.object(quota, "probe_all", side_effect=AssertionError("must not probe")):
                result = quota.collect_quota(agents=["codex"], refresh=True, cache_only=True)
            self.assertTrue(result["agents"]["codex"]["stale"])
            self.assertEqual(result["collected_at"], original["collected_at"])
            self.assertEqual(json.loads(path.read_text()), original)

    def test_empty_agent_allowlist_does_not_expand_to_all(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), patch.object(
            quota, "probe_all", side_effect=AssertionError("must not probe")
        ):
            result = quota.collect_quota(agents=[], refresh=True)
        self.assertEqual(result["agents"], {})
        self.assertTrue(result["stale"])

    def test_pin_or_route_vendor_exclusions_limit_account_queries(self):
        for kwargs, expected in (
            ({"pin_agent": "opencode", "pin_provider": "github-copilot"}, ["opencode"]),
            ({"exclude_agents": ["codex"]}, ["claude-code", "opencode"]),
            ({"exclude_vendors": ["openai"]}, ["claude-code", "opencode"]),
            ({"pin_model": "gpt-6-astra"}, ["codex"]),
            ({"include_vendors": ["google"]}, ["opencode"]),
            ({"quota_agents": ["opencode"], "exclude_quota_agents": ["opencode"]}, []),
        ):
            with self.subTest(kwargs=kwargs), patch.object(select_route, "collect_quota", return_value={"agents": {}, "buckets": {}}) as collect:
                select_route.select_routes(all_models=self.catalog(), quality_floor=0, **kwargs)
            collect.assert_called_once_with(agents=expected, refresh=False, cache_only=False)

    def test_worker_read_ownership_can_differ_from_route_eligibility(self):
        with patch.object(select_route, "collect_quota", return_value={"agents": {}, "buckets": {}}) as collect:
            result = select_route.select_routes(
                all_models=self.catalog(), pin_agent="codex", quota_agents=["opencode"],
                quota_cache_only=True, quality_floor=0,
            )
        collect.assert_called_once_with(agents=[], refresh=False, cache_only=True)
        self.assertEqual(result["selected"]["agent"], "codex")
        self.assertEqual(result["selected"]["quota"]["status"], "unknown")

    def test_supplied_state_bypasses_collection(self):
        with patch.object(select_route, "collect_quota", side_effect=AssertionError("must not collect")):
            result = select_route.select_routes(all_models=self.catalog(), quota_state={"agents": {}, "buckets": {}}, quality_floor=0)
        self.assertEqual(result["status"], "ok")

    def test_plan_only_reads_union_of_step_owners(self):
        with patch.object(select_route, "collect_all_models", return_value=self.catalog()), patch.object(
            select_route, "collect_quota", return_value={"agents": {}, "buckets": {}}
        ) as collect:
            result = select_route.select_step_plan([
                {"pin_agent": "opencode", "pin_provider": "github-copilot"},
                {"pin_agent": "claude-code"},
            ], defaults={"quality_floor": 0}, quota_cache_only=True)
        collect.assert_called_once_with(agents=["claude-code", "opencode"], refresh=False, cache_only=True)
        self.assertEqual(result["status"], "ok")
        for step in result["steps"]:
            self.assertTrue(step["constraints"]["quota_cache_only"])

    def test_plan_persists_injected_state_and_ownership(self):
        state = {"agents": {}, "buckets": {}}
        with patch.object(select_route, "collect_all_models", return_value=self.catalog()), patch.object(
            select_route, "collect_quota", side_effect=AssertionError("must not collect")
        ):
            result = select_route.select_step_plan(
                [{"pin_agent": "claude-code"}], defaults={"quality_floor": 0},
                quota_state=state, quota_agents=["claude-code"], exclude_quota_agents=["codex"], run_id="owned-plan",
            )
        constraints = result["steps"][0]["constraints"]
        self.assertEqual(constraints["quota_state"], state)
        self.assertEqual(constraints["quota_agents"], ["claude-code"])
        self.assertEqual(constraints["exclude_quota_agents"], ["codex"])
        saved = select_route.planned_constraints_for_route(
            select_route.load_selection_plan("owned-plan"), result["steps"][0]["selected"]["route_id"],
        )
        self.assertEqual(saved["quota_state"], state)
        self.assertEqual(saved["quota_agents"], ["claude-code"])
        self.assertEqual(saved["exclude_quota_agents"], ["codex"])

    def test_cli_injected_snapshot_and_ownership_reach_selection(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "coordinator.json"
            state = {"agents": {}, "buckets": {}}
            path.write_text(json.dumps(state))
            with patch.object(select_route, "select_routes", return_value={"status": "ok", "selected": {}}) as select, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(select_route.main(["--quota-state", str(path), "--quota-agent", "opencode", "--exclude-quota-agent", "codex"]), 0)
            self.assertEqual(select.call_args.kwargs["quota_state"], state)
            self.assertEqual(select.call_args.kwargs["quota_agents"], ["opencode"])
            self.assertEqual(select.call_args.kwargs["exclude_quota_agents"], ["codex"])


if __name__ == "__main__":
    unittest.main()
