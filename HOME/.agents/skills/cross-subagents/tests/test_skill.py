#!/usr/bin/env python3
"""Automated regression and verification test suite for cross-subagents skill."""

import contextlib
import io
import json
import argparse
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import common
from common import (
    normalize_official_model_id,
    is_bare_generation_prefix,
    validate_official_model_id,
    canonical_agent_name,
    official_agent_name,
    validate_safe_identifier,
    get_cache_dir,
    file_lock,
)
import models
from models import rebuild_survey_index, resolve_official_model_id
import quota
from quota import compute_window_metrics, PLAN_CAPACITY_FACTORS
import select_route
from select_route import (
    evaluate_route_traits,
    check_route_eligibility,
    select_routes,
    select_step_plan,
    evaluate_route_quota,
    compute_vendor_tier_ranks,
    parse_step_spec,
)
import guard
from guard import snapshot as guard_snapshot, verify as guard_verify
import defects
import run
from run import (
    prepare_prompts,
    record_native_task,
    mark_task_outcome,
    build_run_report,
    format_human_run_report,
    resolve_explicit_route,
    claude_read_only_tool_args,
    opencode_read_only_config,
    tools_enforcement,
)


class TestCrossSubagents(unittest.TestCase):
    def test_colon_sanitization_and_normalization(self):
        cases = [
            ("qwen2.5-coder:latest", "qwen2.5-coder-latest"),
            ("deepseek-r1:free", "deepseek-r1-free"),
            ("opencode-go/glm-5.3:chat", "glm-5.3-chat"),
            ("claude-sonnet-5[1m]:turbo", "claude-sonnet-5-turbo"),
        ]
        for raw, expected in cases:
            norm, _, _ = normalize_official_model_id(raw)
            self.assertEqual(norm, expected, f"Failed for {raw}")
            validate_safe_identifier(norm, "official_model_id")

    def test_bare_generation_prefixes_across_all_paths(self):
        bare = [
            "gpt-6", "gpt-6.1", "gpt",
            "claude-5", "claude-5.1", "claude",
            "gemini-2", "gemini-2.5", "gemini",
            "deepseek", "deepseek-v4.1",
            "qwen", "qwen-3", "qwen-3.5",
            "glm", "glm-5",
        ]
        for b in bare:
            self.assertTrue(is_bare_generation_prefix(b), f"Expected {b} to be identified as bare prefix")
            with self.assertRaises(ValueError):
                validate_official_model_id(b)

            with self.assertRaises(ValueError):
                resolve_explicit_route(f"codex/openai/{b}", force_route=True)

            with self.assertRaises(ValueError):
                record_native_task(
                    run_id="test-bare",
                    task_id=f"t-bare-{int(time.time()*1000)%100000}",
                    agent="Codex",
                    model=b,
                )

        concrete = [
            "gpt-6.1-sol",
            "gpt-6-astra",
            "claude-fable-5-1",
            "claude-sonnet-5",
            "deepseek-flash",
            "glm-5.3",
            "qwen3.5-32b",
            "gemini-2.5-pro",
        ]
        for c in concrete:
            self.assertFalse(is_bare_generation_prefix(c), f"Expected {c} to NOT be bare prefix")
            self.assertEqual(validate_official_model_id(c), c)

    def test_rebuild_survey_index_resilience(self):
        bad_catalog = {
            "opencode": {
                "routes": [
                    {
                        "route_id": "opencode/bad/invalid:model",
                        "canonical_key": "invalid:model",
                        "family": "other",
                        "model_id": "invalid:model",
                    },
                    {
                        "route_id": "opencode/good/valid-model",
                        "canonical_key": "valid-model",
                        "family": "other",
                        "model_id": "valid-model",
                    },
                ]
            }
        }
        rebuild_survey_index(bad_catalog)

    def test_verified_modalities_heuristic_suppression(self):
        route_verified = {
            "route_id": "codex/openai/vision-text-only",
            "canonical_key": "vision-text-only",
            "family": "openai",
            "billing_mode": "included_in_subscription",
            "input_modalities": ["text"],
        }
        traits_verified = evaluate_route_traits(route_verified, task="code_review")
        self.assertFalse(traits_verified["supports_vision"])
        self.assertEqual(traits_verified["input_modalities"], ["text"])

        route_heuristic = {
            "route_id": "codex/openai/vision-text-only",
            "canonical_key": "vision-text-only",
            "family": "openai",
            "billing_mode": "included_in_subscription",
        }
        traits_heuristic = evaluate_route_traits(route_heuristic, task="code_review")
        self.assertTrue(traits_heuristic["supports_vision"])
        self.assertIn("image", traits_heuristic["input_modalities"])

    def test_independent_pointer_blocks_in_prompt(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("# Common")
            common_prompt = Path(f.name)

        run_dir = get_cache_dir() / "runs" / "test-ptrs-run"
        task_id = "task-ptrs"

        prompt_with_ctx = "Task instructions\n\n## Context File Pointers\n- `existing.py`"
        combined, saved_task, _, _ = prepare_prompts(
            run_dir=run_dir,
            task_id=task_id,
            prompt_text=prompt_with_ctx,
            prompt_file=None,
            common_prompt_file=str(common_prompt),
            context_files=["new.py"],
            references=["https://github.com/org/repo/issues/42"],
            recommend_inspect=True,
        )
        self.assertIn("https://github.com/org/repo/issues/42", combined)
        self.assertIn("Coordinator recommendation", combined)

    def test_codex_network_flag_in_read_only(self):
        from run import invoke_codex
        attempt_dir = Path(tempfile.mkdtemp())

        captured_cmd = []
        def mock_run(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            return 0, '{"type": "turn.completed"}', "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                allow_gh=True,
                allow_network=True,
            )
            self.assertNotIn("sandbox_workspace_write.network_access=true", captured_cmd)

            captured_cmd.clear()
            invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
                allow_gh=True,
                allow_network=True,
            )
            self.assertIn("sandbox_workspace_write.network_access=true", captured_cmd)

    def test_claude_code_read_only_gh_subcommands(self):
        from run import invoke_claude_code
        attempt_dir = Path(tempfile.mkdtemp())

        captured_cmd = []
        def mock_run(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            return 0, '{"type": "result"}', "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            invoke_claude_code(
                binary="echo",
                route={"route_id": "claude-code/anthropic/claude-sonnet-5", "model_id": "claude-sonnet-5"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                allow_gh=True,
            )
            allowed_arg_idx = captured_cmd.index("--allowedTools") + 1
            allowed_specs = captured_cmd[allowed_arg_idx].split(",")
            self.assertIn("Bash(gh issue view:*)", allowed_specs)
            self.assertIn("Bash(gh pr view:*)", allowed_specs)
            self.assertNotIn("Bash(gh api:*)", allowed_specs)
            self.assertNotIn("Bash(gh api -X GET:*)", allowed_specs)
            self.assertNotIn("Bash(gh issue:*)", allowed_specs)

    def test_opencode_read_only_strict_shell_denial(self):
        from run import invoke_opencode
        attempt_dir = Path(tempfile.mkdtemp())

        captured_env = {}
        def mock_run(cmd, prompt_text, cwd, timeout_sec, env=None):
            captured_env.update(env or {})
            return 0, '{"type": "step_finish"}', "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            invoke_opencode(
                binary="echo",
                route={"route_id": "opencode/deepseek/deepseek-flash", "model_id": "deepseek-flash", "provider": "deepseek"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                allow_gh=True,
                recipe_flags={"major_version": 2, "supports_read_only": True},
            )
            cfg = json.loads(captured_env["OPENCODE_CONFIG_CONTENT"])
            perm_actions = {rule["action"]: rule["effect"] for rule in cfg.get("permissions", [])}
            self.assertEqual(perm_actions.get("bash"), "deny")
            self.assertEqual(perm_actions.get("shell"), "deny")
            self.assertEqual(perm_actions.get("write"), "deny")

    def test_record_native_task_schema_atomicity_and_locking(self):
        with tempfile.TemporaryDirectory() as td:
            run_id = "test-atomic-native"
            with patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}):
                # 1. Invalid model pattern must raise ValueError
                for invalid_m in ["", "!!!", "---", "   "]:
                    with self.assertRaises(ValueError):
                        record_native_task(
                            run_id=run_id,
                            task_id="task-invalid-m",
                            agent="Antigravity",
                            model=invalid_m,
                        )

                # 2. Nonexistent result_file must raise FileNotFoundError
                with self.assertRaises(FileNotFoundError):
                    record_native_task(
                        run_id=run_id,
                        task_id="task-nonexistent",
                        agent="Antigravity",
                        model="gemini-2.5-pro",
                        result_file="/nonexistent/file/path/here.md",
                    )

                # 3. Successful record
                rec = record_native_task(
                    run_id=run_id,
                    task_id="task-1",
                    agent="Antigravity",
                    model="gemini-2.5-pro",
                    significant=True,
                    task_summary="Root planning.",
                    outcome_summary="Architectural plan ready.",
                )
                self.assertEqual(rec["official_agent"], "Antigravity")
                self.assertEqual(rec["official_model_id"], "gemini-2.5-pro")

                # 4. Verify all required schema fields
                schema_path = SKILL_DIR / "assets/schemas/meta.schema.json"
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                for req in schema["required"]:
                    self.assertIn(req, rec, f"Missing required schema field '{req}' in meta.json")

                # 5. Duplicate task without overwrite=True must raise FileExistsError
                task_dir = run.get_run_dir(run_id) / "task-1"
                with self.assertRaises(FileExistsError):
                    record_native_task(
                        run_id=run_id,
                        task_id="task-1",
                        agent="Antigravity",
                        model="gemini-2.5-pro",
                        overwrite=False,
                    )

                # 6. Atomic failure simulation: if writing metadata fails, result.md is not replaced
                old_res = (task_dir / "result.md").read_text(encoding="utf-8")
                old_meta_text = (task_dir / "meta.json").read_text(encoding="utf-8")
                with patch.object(run, "atomic_write_json", side_effect=OSError("simulated disk error")):
                    with self.assertRaises(OSError):
                        record_native_task(
                            run_id=run_id,
                            task_id="task-1",
                            agent="Antigravity",
                            model="gemini-2.5-pro",
                            result_text="New text should not commit on error",
                            overwrite=True,
                        )
                # Old result and meta must be preserved, temp files cleaned up
                self.assertEqual((task_dir / "result.md").read_text(encoding="utf-8"), old_res)
                self.assertEqual((task_dir / "meta.json").read_text(encoding="utf-8"), old_meta_text)
                self.assertFalse((task_dir / "result.md.tmp").exists())

                # 6b. Rollback simulation: if tmp_result.replace() fails, meta.json rolls back
                orig_replace = Path.replace
                def fail_on_result_replace(self_path, target_path):
                    if str(target_path).endswith("result.md"):
                        raise OSError("simulated rename failure")
                    return orig_replace(self_path, target_path)

                with patch.object(Path, "replace", fail_on_result_replace):
                    with self.assertRaises(OSError):
                        record_native_task(
                            run_id=run_id,
                            task_id="task-1",
                            agent="Antigravity",
                            model="gemini-2.5-pro",
                            result_text="New text that fails to rename",
                            task_summary="Updated summary that must roll back",
                            overwrite=True,
                        )
                # Both meta.json and result.md must have rolled back to previous state, temp files cleaned up
                self.assertEqual((task_dir / "result.md").read_text(encoding="utf-8"), old_res)
                self.assertEqual((task_dir / "meta.json").read_text(encoding="utf-8"), old_meta_text)
                self.assertFalse((task_dir / "result.md.tmp").exists())

                # 6c. Rollback simulation on brand new task: if replace fails, meta.json is unlinked
                new_task_dir = run.get_run_dir(run_id) / "task-rollback-new"
                with patch.object(Path, "replace", fail_on_result_replace):
                    with self.assertRaises(OSError):
                        record_native_task(
                            run_id=run_id,
                            task_id="task-rollback-new",
                            agent="Antigravity",
                            model="gemini-2.5-pro",
                            result_text="Text for new task failing replace",
                        )
                self.assertFalse((new_task_dir / "meta.json").exists())
                self.assertFalse((new_task_dir / "result.md").exists())
                self.assertFalse((new_task_dir / "result.md.tmp").exists())

                # 6d. Rollback failure chaining: if rollback atomic_write_text also fails, raise replace_err with cause
                with patch.object(Path, "replace", fail_on_result_replace):
                    with patch.object(run, "atomic_write_text", side_effect=OSError("disk full on rollback")):
                        with self.assertRaises(OSError) as cm:
                            record_native_task(
                                run_id=run_id,
                                task_id="task-1",
                                agent="Antigravity",
                                model="gemini-2.5-pro",
                                result_text="Text failing with rollback error",
                                overwrite=True,
                            )
                        self.assertEqual(str(cm.exception), "simulated rename failure")
                        self.assertIsNotNone(cm.exception.__cause__)
                        self.assertIn("disk full on rollback", str(cm.exception.__cause__))

                # 6e. Unchanged manifest assertion: failed attempts must not append to manifest.jsonl
                manifest_path = run.get_run_dir(run_id) / "manifest.jsonl"
                manifest_lines = manifest_path.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(line) for line in manifest_lines]
                # Only initial task-1 should be recorded so far
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["task_id"], "task-1")
                self.assertNotIn("task-rollback-new", [e.get("task_id") for e in events])

                # 7. Overwrite=True succeeds when write is normal
                rec_ov = record_native_task(
                    run_id=run_id,
                    task_id="task-1",
                    agent="Antigravity",
                    model="gemini-2.5-pro",
                    result_text="Committed replacement text",
                    overwrite=True,
                )
                self.assertEqual((task_dir / "result.md").read_text(encoding="utf-8"), "Committed replacement text")

    def test_absolute_quota_headroom_protection(self):
        # 1. Free tier plan: 100% remaining (0% used), but capacity_factor=0.1
        free_metrics = compute_window_metrics(
            used_percent=0.0,
            window_mins=10080,
            resets_at_epoch=int(time.time()) + 86400,
            capacity_factor=0.1,
            reached_limit=False,
        )
        self.assertEqual(free_metrics["effective_headroom"], 0.10)
        self.assertTrue(free_metrics["is_micro_capacity"])
        self.assertTrue(free_metrics["insufficient_headroom"])

        route_free = {
            "agent": "opencode",
            "provider": "openrouter",
            "model_id": "deepseek-flash",
            "official_model_id": "deepseek-flash",
            "canonical_key": "deepseek-flash",
            "quota_bucket": "opencode:free-bucket",
            "applicable_buckets": ["opencode:free-bucket"],
        }
        quota_state = {
            "buckets": {
                "opencode:free-bucket": {
                    "bucket_id": "opencode:free-bucket",
                    "status": "ample",
                    "used_percent": 0.0,
                    "scarcity": 0.40,
                    "effective_headroom": 0.10,
                    "is_micro_capacity": True,
                    "exhausted": False,
                }
            },
            "agents": {"opencode": {"stale": False}},
        }

        # Code review task must be disqualified due to micro headroom even if pinned or explicit
        ok, reason, _, _ = check_route_eligibility(
            route=route_free,
            task_norm="code_review",
            quota_state=quota_state,
            allow_micro_quota=False,
            is_explicit_route=True,
        )
        self.assertFalse(ok)
        self.assertIn("insufficient_absolute_quota", reason)

        # But with allow_micro_quota=True, it passes
        ok_override, _, _, _ = check_route_eligibility(
            route=route_free,
            task_norm="code_review",
            quota_state=quota_state,
            allow_micro_quota=True,
            is_explicit_route=True,
        )
        self.assertTrue(ok_override)

    @staticmethod
    def _fake_route(agent, provider, model, canonical, bucket):
        return {
            "route_id": f"{agent}/{provider}/{model}",
            "agent": agent,
            "provider": provider,
            "model_id": model,
            "canonical_key": canonical,
            "official_model_id": canonical,
            "quota_bucket": bucket,
            "applicable_buckets": [bucket],
        }

    def _fake_selection_inputs(self):
        catalog = {
            "codex": {
                "usable": True,
                "routes": [
                    self._fake_route("codex", "openai", "gpt-6-astra", "gpt-6-astra", "codex:codex"),
                    self._fake_route("codex", "openai", "gpt-6.1-sol", "gpt-6.1-sol", "codex:codex"),
                ],
            },
            "claude-code": {
                "usable": True,
                "routes": [
                    self._fake_route(
                        "claude-code", "anthropic", "claude-fable-5-1", "claude-fable-5-1", "claude-code:session"
                    )
                ],
            },
            "opencode": {
                "usable": True,
                "routes": [
                    self._fake_route("opencode", "deepseek", "deepseek-flash", "deepseek-flash", "opencode:deepseek"),
                    self._fake_route("opencode", "opencode-go", "glm-5.3", "glm-5.3", "opencode:opencode-go"),
                    self._fake_route("opencode", "openai", "gpt-6-astra", "gpt-6-astra", "opencode:openai"),
                ],
            },
        }
        quota_state = {
            "buckets": {
                bucket_id: {
                    "bucket_id": bucket_id,
                    "status": "ample",
                    "used_percent": 10.0,
                    "scarcity": 0.0,
                    "effective_headroom": 0.9,
                    "is_micro_capacity": False,
                    "exhausted": False,
                }
                for bucket_id in (
                    "codex:codex",
                    "claude-code:session",
                    "opencode:deepseek",
                    "opencode:opencode-go",
                    "opencode:openai",
                )
            },
            "agents": {agent: {"stale": False} for agent in catalog},
        }
        return catalog, quota_state

    def test_vendor_tier_ranks_are_per_vendor_and_stable(self):
        catalog, _ = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None):
            ranks = compute_vendor_tier_ranks(catalog, "code_review")
        self.assertEqual(ranks["gpt-6-astra"], 1)
        self.assertEqual(ranks["gpt-6.1-sol"], 2)
        self.assertEqual(ranks["claude-fable-5-1"], 1)
        self.assertEqual(ranks["deepseek-flash"], 1)

    def test_include_vendor_and_max_tier_filters(self):
        catalog, quota_state = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None):
            openai_only = select_routes(
                task="code_review",
                include_vendors=["openai"],
                all_models=catalog,
                quota_state=quota_state,
                use_saved_pins=False,
            )
            self.assertEqual(openai_only["status"], "ok")
            self.assertEqual(openai_only["selected"]["vendor"], "openai")

            top_tier = select_routes(
                task="code_review",
                include_vendors=["openai"],
                max_tier=1,
                all_models=catalog,
                quota_state=quota_state,
                use_saved_pins=False,
            )
            self.assertEqual(top_tier["selected"]["canonical_key"], "gpt-6-astra")
            self.assertEqual(top_tier["selected"]["vendor_tier_rank"], 1)

            route_excluded = select_routes(
                task="code_review",
                include_vendors=["openai"],
                max_tier=2,
                exclude_route_ids=["codex/openai/gpt-6-astra"],
                all_models=catalog,
                quota_state=quota_state,
                use_saved_pins=False,
            )
            self.assertNotEqual(route_excluded["selected"]["route_id"], "codex/openai/gpt-6-astra")

            second_tier = select_routes(
                task="code_review",
                include_vendors=["openai"],
                max_tier=2,
                exclude_canonical_keys=["gpt-6-astra"],
                all_models=catalog,
                quota_state=quota_state,
                use_saved_pins=False,
            )
            self.assertEqual(second_tier["selected"]["canonical_key"], "gpt-6.1-sol")

    def test_step_plan_distinct_vendors_and_partial_plan(self):
        catalog, quota_state = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [
                    {"include_vendor": ["deepseek"], "speed": "fast"},
                    {"include_vendor": ["openai", "anthropic"]},
                ],
                require_distinct_vendor=True,
            )
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(len(plan["steps"]), 2)
            vendors = [entry["selected"]["vendor"] for entry in plan["steps"]]
            self.assertEqual(len(set(vendors)), 2)

            partial = select_step_plan(
                [{"include_vendor": ["openai"]}, {"include_vendor": ["openai"]}],
                require_distinct_vendor=True,
            )
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(len(partial["steps"]), 1)
            self.assertEqual(partial["unfilled"][0]["step"], 2)

    def test_plan_constraints_persist_for_pinned_fallbacks(self):
        catalog, quota_state = self._fake_selection_inputs()
        with tempfile.TemporaryDirectory() as td, patch.object(
            select_route, "get_cache_dir", return_value=Path(td)
        ), patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [{}],
                defaults={"task": "code_review", "exclude_vendor": ["openai", "anthropic"]},
                require_distinct_vendor=False,
                run_id="plan-run",
            )
            self.assertEqual(plan["status"], "ok")
            selected = plan["steps"][0]["selected"]
            persisted = select_route.load_selection_plan("plan-run")
            constraints = select_route.planned_constraints_for_route(
                persisted, selected["route_id"]
            )
            self.assertIsNotNone(constraints)
            self.assertIn("openai", constraints["exclude_vendor"])
            self.assertIn("anthropic", constraints["exclude_vendor"])

            policy = run.resolve_pinned_fallback_constraints(
                selected,
                run_id="plan-run",
                exclude_vendors=None,
                exclude_agents=None,
                include_vendors=None,
                max_tier=None,
                min_context=None,
                require_modalities=None,
                require_vision=False,
                allow_estimated_capabilities=False,
                allow_micro_quota=False,
                min_headroom=None,
                speed="normal",
                exclude_route_ids=None,
            )
        self.assertEqual(policy["scope"], "planned_constraints")
        self.assertIn("openai", policy["exclude_vendors"])
        self.assertIn("anthropic", policy["exclude_vendors"])

    def test_pinned_fallback_stays_in_primary_vendor(self):
        route = {
            "route_id": "antigravity/google/gemini-4-argon-high",
            "agent": "antigravity",
            "provider": "google",
            "model_id": "gemini-4-argon-high",
            "canonical_key": "gemini-4-argon",
            "vendor": "google",
        }
        with patch.object(run, "load_selection_plan", return_value={}):
            policy = run.resolve_pinned_fallback_constraints(
                route,
                run_id="no-plan",
                exclude_vendors=None,
                exclude_agents=None,
                include_vendors=None,
                max_tier=None,
                min_context=None,
                require_modalities=None,
                require_vision=False,
                allow_estimated_capabilities=False,
                allow_micro_quota=False,
                min_headroom=None,
                speed="normal",
                exclude_route_ids=None,
            )
        self.assertEqual(policy["scope"], "same_vendor")
        self.assertEqual(policy["include_vendors"], ["google"])

        with patch.object(run, "load_selection_plan", return_value={}):
            explicit = run.resolve_pinned_fallback_constraints(
                route,
                run_id="no-plan",
                exclude_vendors=["anthropic"],
                exclude_agents=None,
                include_vendors=["openai", "anthropic"],
                max_tier=2,
                min_context=None,
                require_modalities=None,
                require_vision=False,
                allow_estimated_capabilities=False,
                allow_micro_quota=False,
                min_headroom=None,
                speed="normal",
                exclude_route_ids=None,
            )
        self.assertEqual(explicit["scope"], "include_vendors")
        self.assertEqual(explicit["include_vendors"], ["openai", "anthropic"])
        self.assertEqual(explicit["max_tier"], 2)

        unknown_vendor = {**route, "canonical_key": "", "provider": ""}
        with patch.object(run, "load_selection_plan", return_value={}):
            self.assertIsNone(
                run.resolve_pinned_fallback_constraints(
                    unknown_vendor,
                    run_id="no-plan",
                    exclude_vendors=None,
                    exclude_agents=None,
                    include_vendors=None,
                    max_tier=None,
                    min_context=None,
                    require_modalities=None,
                    require_vision=False,
                    allow_estimated_capabilities=False,
                    allow_micro_quota=False,
                    min_headroom=None,
                    speed="normal",
                    exclude_route_ids=None,
                )
            )

    def test_parse_step_spec(self):
        parsed = parse_step_spec('{"include_vendor":"openai","max_tier":"2","speed":"fast"}')
        self.assertEqual(parsed["include_vendor"], ["openai"])
        self.assertEqual(parsed["max_tier"], 2)
        with self.assertRaises(ValueError):
            parse_step_spec('{"unknown_field": true}')
        with self.assertRaises(ValueError):
            parse_step_spec("not json")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"exclude_vendor":["google"]}')
            spec_path = f.name
        self.assertEqual(parse_step_spec(f"@{spec_path}")["exclude_vendor"], ["google"])

    def test_guard_snapshot_and_verify(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("one\n", encoding="utf-8")
            (root / "build").mkdir()
            baseline = guard_snapshot(root, excludes=["build/**"])
            self.assertEqual(baseline["status"], "ok")
            self.assertEqual(guard_verify(root, baseline)["status"], "clean")

            (root / "build" / "ignored.txt").write_text("ignored", encoding="utf-8")
            self.assertEqual(guard_verify(root, baseline)["status"], "clean")

            (root / "src" / "a.py").write_text("two\n", encoding="utf-8")
            changed = guard_verify(root, baseline)
            self.assertEqual(changed["status"], "mutated")
            self.assertIn("src/a.py", changed["changed"])

            (root / "src" / "b.py").write_text("new\n", encoding="utf-8")
            added = guard_verify(root, baseline)
            self.assertIn("src/b.py", added["added"])

    def test_guard_preserves_leading_dot_in_excludes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".cache").mkdir()
            (root / ".cache" / "x.txt").write_text("x", encoding="utf-8")
            (root / "keep.txt").write_text("k", encoding="utf-8")
            baseline = guard_snapshot(root, excludes=[".cache/**"])
            self.assertEqual(baseline["status"], "ok")
            self.assertIn("keep.txt", baseline["files"])
            self.assertNotIn(".cache/x.txt", baseline["files"])

    def test_leading_double_star_excludes_root_directory_descendants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            (root / "scripts" / "a.py").write_text("x", encoding="utf-8")
            (root / "keep.txt").write_text("k", encoding="utf-8")
            baseline = guard_snapshot(root, excludes=["**/scripts"])
            self.assertEqual(baseline["status"], "ok")
            self.assertNotIn("scripts/a.py", baseline["files"])
            self.assertIn("keep.txt", baseline["files"])
            # Verification applies the same exclusion to both maps.
            self.assertEqual(guard_verify(root, baseline)["status"], "clean")

    def test_guard_detects_marker_content_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gitdir = root / ".git"
            gitdir.mkdir()
            (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (gitdir / "MERGE_HEAD").write_text("first\n", encoding="utf-8")
            baseline = guard_snapshot(root)
            (gitdir / "MERGE_HEAD").write_text("second\n", encoding="utf-8")
            report = guard_verify(root, baseline)
            self.assertEqual(report["status"], "mutated")
            self.assertIn("git:state:MERGE_HEAD", report["git_changes"])

    def test_guard_detects_git_state_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gitdir = root / ".git"
            gitdir.mkdir()
            (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            baseline = guard_snapshot(root)
            self.assertTrue(baseline["git"]["is_repo"])
            (gitdir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
            report = guard_verify(root, baseline)
            self.assertEqual(report["status"], "mutated")
            self.assertIn("git:head_raw", report["git_changes"])

    def test_claude_read_only_tool_postures(self):
        flags = {
            "has_permission_mode": True,
            "has_permission_prompts": True,
            "has_tools": True,
            "has_disallowed_tools": True,
            "has_allowed_tools": True,
            "has_safe_mode": True,
            "has_strict_mcp_config": True,
        }
        no_tools = claude_read_only_tool_args(flags, no_tools=True)
        self.assertIn("--safe-mode", no_tools)
        self.assertIn("--strict-mcp-config", no_tools)
        self.assertEqual(no_tools[no_tools.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", no_tools)

        shell = claude_read_only_tool_args(flags, allow_shell=True)
        self.assertNotIn("--safe-mode", shell)
        self.assertIn("Bash", shell[shell.index("--tools") + 1].split(","))
        self.assertIn("Bash(*)", shell[shell.index("--allowedTools") + 1].split(","))

    def test_claude_recipe_recovers_flags_hidden_by_truncated_help(self):
        import probe

        truncated_help = "--allowedTools, --allowed-tools <tools...>\n  --disallowedTools <tools...>"

        def fake_run(cmd, **kwargs):
            if cmd[1:] == ["--help"]:
                return argparse.Namespace(returncode=0, stdout=truncated_help, stderr="")
            return argparse.Namespace(
                returncode=1,
                stdout="error: unknown option '--cross-subagents-probe-unknown-option'\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as td, patch.object(
            probe, "run_cmd", side_effect=fake_run
        ), patch.object(probe, "get_cache_dir", return_value=Path(td)):
            flags = probe.cache_cli_help_and_recipe("claude-code", "/tmp/claude", "2.1.312")

        # `--help` was truncated before `--tools`; the parser still accepts it.
        self.assertTrue(flags["has_tools"])
        self.assertTrue(flags["has_permission_mode"])
        self.assertTrue(flags["has_safe_mode"])

    def test_claude_recipe_keeps_flags_the_parser_rejects(self):
        import probe

        def fake_run(cmd, **kwargs):
            if cmd[1:] == ["--help"]:
                return argparse.Namespace(returncode=0, stdout="--allowedTools <tools...>", stderr="")
            return argparse.Namespace(
                returncode=1, stdout="", stderr="error: unknown option '--tools'\n"
            )

        with tempfile.TemporaryDirectory() as td, patch.object(
            probe, "run_cmd", side_effect=fake_run
        ), patch.object(probe, "get_cache_dir", return_value=Path(td)):
            flags = probe.cache_cli_help_and_recipe("claude-code", "/tmp/claude", "2.1.312")

        self.assertFalse(flags["has_tools"])

    def test_claude_read_only_rescues_stale_recipe_flags(self):
        from run import invoke_claude_code

        attempt_dir = Path(tempfile.mkdtemp())
        captured: list[str] = []
        payload = {
            "session_id": "claude-session",
            "is_error": False,
            "subtype": "success",
            "result": "CLAUDE ANSWER",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "modelUsage": {"claude-opus-5-5": {}},
        }

        def mock_run(cmd, *args, **kwargs):
            captured.extend(cmd)
            return 0, json.dumps(payload), "", False, True

        stale_flags = {
            key: False
            for key in (
                "has_tools",
                "has_disallowed_tools",
                "has_permission_mode",
                "has_permission_prompts",
                "has_safe_mode",
                "has_strict_mcp_config",
            )
        }
        with patch.object(run, "parser_accepts_flag", return_value=True), patch.object(
            run, "run_process_with_timeout", side_effect=mock_run
        ):
            res = invoke_claude_code(
                binary="claude",
                route={
                    "route_id": "claude-code/anthropic/claude-opus-5-5",
                    "model_id": "claude-opus-5-5",
                    "canonical_key": "claude-opus-5-5",
                    "provider": "anthropic",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags=stale_flags,
            )
        self.assertNotEqual(res.get("error_class"), "permission")
        self.assertIn("--tools", captured)
        self.assertEqual(captured[captured.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--disallowedTools", captured)

    def test_claude_read_only_refusal_names_the_missing_flag(self):
        from run import invoke_claude_code

        with patch.object(run, "parser_accepts_flag", return_value=False), patch.object(
            run, "run_process_with_timeout", side_effect=AssertionError("must not invoke")
        ):
            res = invoke_claude_code(
                binary="claude",
                route={
                    "route_id": "claude-code/anthropic/claude-opus-5-5",
                    "model_id": "claude-opus-5-5",
                    "canonical_key": "claude-opus-5-5",
                },
                prompt_text="dummy",
                attempt_dir=Path(tempfile.mkdtemp()),
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={"has_tools": False, "has_disallowed_tools": True},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "permission")
        self.assertIn("--tools", res["error"])
        self.assertNotIn("--disallowedTools", res["error"])

    def test_opencode_read_only_config_postures(self):
        cfg, verified = opencode_read_only_config({}, major_ver=2, no_tools=True)
        self.assertTrue(verified)
        self.assertIn("tools-off", cfg["agents"])
        rules = {(rule["action"], rule["effect"]) for rule in cfg["permissions"]}
        self.assertIn(("shell", "deny"), rules)
        self.assertIn(("webfetch", "deny"), rules)
        self.assertEqual(cfg["share"], "disabled")

        shell_cfg, shell_verified = opencode_read_only_config({}, major_ver=2, allow_shell=True)
        self.assertTrue(shell_verified)
        shell_rules = {(rule["action"], rule["effect"]) for rule in shell_cfg["permissions"]}
        self.assertIn(("shell", "allow"), shell_rules)
        self.assertIn("review-shell", shell_cfg["agents"])
        self.assertIn(
            ("edit", "deny"),
            shell_rules,
        )

    def test_tools_enforcement_reporting(self):
        self.assertIs(tools_enforcement("claude-code", {"no_tools": True}), True)
        self.assertIs(tools_enforcement("opencode", {"no_tools": True}), True)
        self.assertIs(tools_enforcement("codex", {"no_tools": True}), False)
        self.assertIs(tools_enforcement("antigravity", {"no_tools": True}), False)
        self.assertIsNone(tools_enforcement("codex", {}))
        self.assertIsNone(tools_enforcement("claude-code", {"no_tools": True}, "failed"))

    def test_step_plan_never_repeats_a_canonical_model(self):
        catalog, quota_state = self._fake_selection_inputs()
        # Recency is covered by its own tests; neutralize the local registry so
        # this test only checks plan mechanics.
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "lookup_model_release_date", return_value=(None, None)
        ), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [{"include_vendor": ["openai"]}, {"include_vendor": ["openai"]}],
                require_distinct_vendor=False,
            )
        self.assertEqual(plan["status"], "ok")
        canonicals = [entry["selected"]["canonical_key"] for entry in plan["steps"]]
        self.assertEqual(canonicals[0], "gpt-6-astra")
        self.assertNotEqual(canonicals[0], canonicals[1])

    def test_step_plan_honors_global_route_exclusions(self):
        catalog, quota_state = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [{"include_vendor": ["openai"]}],
                defaults={
                    "task": "code_review",
                    "include_vendor": ["openai"],
                    "exclude_route": ["codex/openai/gpt-6-astra"],
                },
            )
        self.assertEqual(plan["status"], "ok")
        self.assertNotEqual(plan["steps"][0]["selected"]["route_id"], "codex/openai/gpt-6-astra")

    def test_zero_steps_is_rejected(self):
        self.assertEqual(select_route.main(["--steps", "0"]), 2)

    def test_recency_score_anchors(self):
        self.assertEqual(select_route.compute_recency_score(None), 0.0)
        self.assertEqual(select_route.compute_recency_score(0), 1.0)
        self.assertEqual(select_route.compute_recency_score(30), 1.0)
        self.assertEqual(select_route.compute_recency_score(180), 0.0)
        self.assertEqual(select_route.compute_recency_score(540), -1.0)
        self.assertEqual(select_route.compute_recency_score(900), -1.0)
        # Monotonic decay between the anchors.
        scores = [select_route.compute_recency_score(d) for d in (30, 90, 180, 300, 540)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(select_route.compute_recency_score(105), 0.0)
        self.assertLess(select_route.compute_recency_score(200), 0.0)

    def test_model_age_days_from_iso_date(self):
        from datetime import date

        self.assertEqual(
            select_route.model_age_days("2026-10-04", today=date(2026, 11, 3)), 30
        )
        self.assertIsNone(select_route.model_age_days(None))
        self.assertIsNone(select_route.model_age_days("not-a-date"))

    def test_release_registry_lookup_by_model_and_variant_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "github-copilot": {
                            "models": {
                                "kimi-k3": {"release_date": "2026-07-16"},
                                "kimi-k3.1-pro": {"release_date": "2026-10-23T00:00:00Z"},
                            }
                        },
                        "moonshotai": {
                            "models": {
                                "kimi-k3": {"release_date": "2026-07-16"},
                                "kimi-k3.1-pro": {"release_date": "2026-10-23"},
                            }
                        },
                        "google": {"models": {"gemini-4-argon": {"release_date": "2026-10-13"}}},
                        "deepseek": {"models": {"deepseek-flash": {"release_date": "2026-10-17"}}},
                        "acme": {"models": {"deepseek-flash": {"release_date": "2026-09-10"}}},
                        "contoso": {"models": {"deepseek-flash": {"release_date": "2026-09-10"}}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CROSS_SUBAGENTS_MODEL_REGISTRY": str(registry)}):
                self.assertEqual(
                    models.lookup_model_release_date(
                        provider="github-copilot", model_id="kimi-k3", canonical_key="kimi-k3"
                    ),
                    ("2026-07-16", "registry"),
                )
                # Effort-suffixed ids resolve through their canonical key.
                self.assertEqual(
                    models.lookup_model_release_date(
                        provider="google",
                        model_id="gemini-4-argon-high",
                        canonical_key="gemini-4-argon",
                    ),
                    ("2026-10-13", "registry"),
                )
                # Unknown providers fall back to the bare model id.
                self.assertEqual(
                    models.lookup_model_release_date(
                        provider="unknown-provider", model_id="kimi-k3.1-pro"
                    ),
                    ("2026-10-23", "registry"),
                )
                # The release date is model-level: a provider-specific outlier
                # does not make the same model look newer on one route.
                self.assertEqual(
                    models.lookup_model_release_date(
                        provider="deepseek", model_id="deepseek-flash", canonical_key="deepseek-flash"
                    ),
                    ("2026-09-10", "registry"),
                )
                self.assertEqual(
                    models.lookup_model_release_date(provider="google", model_id="no-such-model"),
                    (None, None),
                )

    def test_missing_release_registry_is_neutral(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"CROSS_SUBAGENTS_MODEL_REGISTRY": str(Path(td) / "absent.json")}
        ):
            self.assertEqual(
                models.lookup_model_release_date(provider="openai", model_id="gpt-6.1-sol"),
                (None, None),
            )
            traits = evaluate_route_traits(
                self._fake_route("codex", "openai", "gpt-6.1-sol", "gpt-6.1-sol", "codex:codex"),
                task="code_review",
            )
            self.assertEqual(traits["recency_score"], 0.0)
            self.assertIsNone(traits["release_date"])

    def test_newer_model_wins_a_close_selection(self):
        catalog = {
            "opencode": {
                "usable": True,
                "routes": [
                    self._fake_route(
                        "opencode", "deepseek", "deepseek-flash", "deepseek-flash", "opencode:deepseek"
                    ),
                    self._fake_route("opencode", "opencode-go", "glm-5.3", "glm-5.3", "opencode:opencode-go"),
                ],
            },
        }
        quota_state = {
            "buckets": {
                bucket: {
                    "bucket_id": bucket,
                    "status": "ample",
                    "used_percent": 10.0,
                    "scarcity": 0.0,
                    "effective_headroom": 0.9,
                    "is_micro_capacity": False,
                    "exhausted": False,
                }
                for bucket in ("opencode:deepseek", "opencode:opencode-go")
            },
            "agents": {"opencode": {"stale": False}},
        }
        surveys = {
            "deepseek-flash": {
                "confidence": "low",
                "task_fit": {"code_review": 0.80},
                "released_at": "2024-01-01",  # ~2.8 years old: stale
            },
            "glm-5.3": {
                "confidence": "low",
                "task_fit": {"code_review": 0.80},
                "released_at": "2026-11-01",  # two days old
            },
        }
        with patch.object(select_route, "get_survey", side_effect=lambda key: surveys.get(key)):
            res = select_routes(
                task="code_review",
                all_models=catalog,
                quota_state=quota_state,
                use_saved_pins=False,
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["selected"]["canonical_key"], "glm-5.3")
        self.assertGreater(res["selected"]["recency_score"], 0.9)

    def test_survey_rejects_invalid_released_at(self):
        with self.assertRaises(ValueError):
            models.validate_survey_payload(
                {"confidence": "low", "released_at": "last tuesday", "sources": []}
            )
        models.validate_survey_payload(
            {"confidence": "low", "released_at": "2026-09-22", "sources": []}
        )

    def test_provider_model_not_supported_is_model_unavailable(self):
        self.assertEqual(
            run.classify_failure(
                1,
                '{"type":"provider.invalid-request","message":"The requested model is not supported."}',
                "",
            ),
            "model_unavailable",
        )
        self.assertEqual(
            run.classify_failure(1, '{"code":"model_not_supported"}', ""),
            "model_unavailable",
        )

    def test_provider_no_route_is_model_unavailable(self):
        # Regression: OpenCode emits this payload when its provider cannot
        # route a model. It used to classify as `failed`, which is not a
        # fallback-triggering class, so a pinned route that lost its model
        # returned after one attempt instead of walking the planned chain.
        self.assertEqual(
            run.classify_failure(
                1,
                '{"type": "provider.no-route", "message": "Model unavailable: github-copilot/kimi-k3"}',
                "",
            ),
            "model_unavailable",
        )
        self.assertEqual(
            run.classify_failure(1, '{"type":"provider.no-route","message":"No provider for model."}', ""),
            "model_unavailable",
        )

    def test_pinned_route_falls_back_on_provider_no_route(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            repo = Path(td) / "repo"
            repo.mkdir()
            pinned = {
                "route_id": "opencode/github-copilot/kimi-k3",
                "agent": "opencode",
                "provider": "github-copilot",
                "model_id": "kimi-k3",
                "canonical_key": "kimi-k3",
                "official_model_id": "kimi-k3",
                "quota_bucket": "opencode:github-copilot",
                "applicable_buckets": ["opencode:github-copilot"],
            }
            fallback = {
                "route_id": "opencode/deepseek/deepseek-flash",
                "agent": "opencode",
                "provider": "deepseek",
                "model_id": "deepseek-flash",
                "canonical_key": "deepseek-flash",
                "official_model_id": "deepseek-flash",
                "quota_bucket": "opencode:deepseek",
                "applicable_buckets": ["opencode:deepseek"],
            }
            payload = '{"type": "provider.no-route", "message": "Model unavailable: github-copilot/kimi-k3"}'
            calls: list[str] = []

            def fake_invoke(binary, route, *args, **kwargs):
                attempt_dir = args[1]
                calls.append(route["route_id"])
                if route["route_id"] == pinned["route_id"]:
                    return {
                        "status": "failed",
                        "exit_code": 1,
                        "session_id": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                        "requested_model": route["model_id"],
                        "resolved_model": route["model_id"],
                        "observed_model": None,
                        "session_cleaned_up": True,
                        "cleanup_verified": True,
                        "error_class": run.classify_failure(1, payload, ""),
                        "error": payload,
                    }
                (attempt_dir / "result.md").write_text("fallback answer", encoding="utf-8")
                return {
                    "status": "ok",
                    "exit_code": 0,
                    "session_id": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "requested_model": route["model_id"],
                    "resolved_model": route["model_id"],
                    "observed_model": route["model_id"],
                    "session_cleaned_up": True,
                    "cleanup_verified": True,
                    "error_class": None,
                }

            with patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": str(cache)}), patch.object(
                run, "resolve_explicit_route", return_value=dict(pinned)
            ), patch.object(
                run,
                "resolve_pinned_fallback_constraints",
                return_value={"scope": "planned_constraints"},
            ), patch.object(
                run,
                "select_routes",
                return_value={"status": "ok", "selected": dict(fallback), "fallbacks": []},
            ), patch.object(
                run,
                "probe_all",
                return_value={
                    "agents": {
                        "opencode": {
                            "usable": True,
                            "binary": "true",
                            "version": "2.2.0",
                            "recipe_flags": {"cli_major": 2, "has_agent_flag": True},
                        }
                    }
                },
            ), patch.object(run, "invoke_opencode", side_effect=fake_invoke):
                res = run.execute_task(
                    run_id="no-route-fallback-run",
                    task_id="t1",
                    route_str=pinned["route_id"],
                    prompt_text="x",
                    cwd=str(repo),
                    sandbox_mode="read-only",
                    force_route=True,
                )

            self.assertEqual(calls, [pinned["route_id"], fallback["route_id"]])
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["route_id"], fallback["route_id"])
            self.assertEqual(
                res["prior_failed_attempts"],
                [{"route_id": pinned["route_id"], "error_class": "model_unavailable"}],
            )

    def test_opencode_export_is_authoritative_and_must_be_current(self):
        from run import invoke_opencode

        def run_with(fresh=True, stream_text="PARTIAL", stream_finish="stop", denial=False, omit_model=False):
            attempt_dir = Path(tempfile.mkdtemp())
            export = {
                "messages": [
                    {"type": "user", "content": [{"type": "text", "text": "prompt"}]},
                    {
                        "type": "assistant",
                        "time": {"created": 0, "completed": 0},
                        "finish": "stop",
                        "model": {"id": "deepseek-flash", "providerID": "deepseek", "variant": "default"},
                        "content": [{"type": "text", "text": "FULL ANSWER"}],
                    },
                ]
            }
            stream = "\n".join(
                [
                    json.dumps({"type": "step_start", "sessionID": "ses_test"}),
                    json.dumps({"type": "text", "part": {"text": stream_text}}),
                    json.dumps({"type": "step_finish", "part": {"reason": stream_finish}}),
                ]
            )

            captured: dict[str, str] = {}

            def fake_run(cmd, prompt="", *args, **kwargs):
                if cmd[1:3] == ["session", "export"]:
                    sent = captured.get("prompt", "")
                    nonce = sent[sent.rfind("[cross-subagents turn:") :].strip()
                    export["messages"][0]["content"] = [{"type": "text", "text": f"prompt {nonce}"}]
                    completed = int(time.time() * 1000) - (0 if fresh else 3_600_000)
                    export["messages"][1]["time"] = {"created": completed, "completed": completed}
                    if denial:
                        export["messages"][1]["content"].append(
                            {
                                "type": "tool",
                                "tool": "read",
                                "state": {"status": "error", "error": "permission denied"},
                            }
                        )
                    if omit_model:
                        export["messages"][1].pop("model", None)
                    return 0, json.dumps(export), "", False, True
                captured["prompt"] = prompt
                return 0, stream, "", False, True

            with patch.object(run, "run_process_with_timeout", side_effect=fake_run):
                return invoke_opencode(
                    binary="echo",
                    route={
                        "route_id": "opencode/deepseek/deepseek-flash",
                        "agent": "opencode",
                        "provider": "deepseek",
                        "model_id": "deepseek-flash",
                        "canonical_key": "deepseek-flash",
                    },
                    prompt_text="prompt",
                    attempt_dir=attempt_dir,
                    resume_session_id=None,
                    effort=None,
                    keep_session=True,
                    sandbox_mode="read-only",
                    cwd=None,
                    timeout_sec=5,
                    child_env={},
                    recipe_flags={"cli_major": 2, "has_agent_flag": True, "has_session_cmd": True},
                    policy={"allow_shell": True},
                ), attempt_dir

        res, attempt_dir = run_with()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(
            (attempt_dir / "result.md").read_text(encoding="utf-8").strip(), "FULL ANSWER"
        )

        stale, _ = run_with(fresh=False)
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(stale["error_class"], "format")

        denied, _ = run_with(denial=True)
        self.assertEqual(denied["status"], "ok")
        self.assertEqual(len(denied["denials_observed"]), 1)

        missing, _ = run_with(omit_model=True)
        self.assertEqual(missing["status"], "failed")

    def test_sandbox_unsupported_routes_are_ineligible(self):
        route = self._fake_route(
            "antigravity", "google", "gemini-4-argon-high", "gemini-4-argon-high", "antigravity:default"
        )
        quota_state = {
            "buckets": {
                "antigravity:default": {
                    "bucket_id": "antigravity:default",
                    "status": "ample",
                    "used_percent": 0.0,
                    "scarcity": 0.0,
                    "effective_headroom": 1.0,
                    "is_micro_capacity": False,
                    "exhausted": False,
                }
            },
            "agents": {"antigravity": {"stale": False}},
        }
        with patch.object(
            select_route, "get_agent_recipe", return_value={"has_mode": False, "has_sandbox": False}
        ):
            self.assertTrue(select_route.sandbox_supported("antigravity", "bypass"))
            self.assertFalse(select_route.sandbox_supported("antigravity", "read-only"))
            ok, reason, _, _ = check_route_eligibility(
                route=route,
                task_norm="general",
                quota_state=quota_state,
                sandbox_mode="read-only",
            )
            self.assertFalse(ok)
            self.assertIn("sandbox_unsupported", reason)

        with patch.object(
            select_route, "get_agent_recipe", return_value={"has_mode": True, "has_sandbox": True}
        ):
            ok, reason, _, _ = check_route_eligibility(
                route=route,
                task_norm="general",
                quota_state=quota_state,
                sandbox_mode="read-only",
            )
            self.assertTrue(ok, reason)

    def test_namespaced_model_identity_and_compatibility(self):
        self.assertTrue(
            run._verify_model_match(
                "anthropic/claude-sonnet-4",
                "claude-sonnet-4",
                "openrouter/anthropic/claude-sonnet-4",
                expected_provider="openrouter",
            )
        )
        self.assertTrue(
            run._models_compatible(
                ["deepseek/deepseek-flash", "deepseek/deepseek-flash#high"]
            )
        )
        self.assertFalse(
            run._models_compatible(
                ["deepseek/deepseek-flash#high", "deepseek/deepseek-flash#low"]
            )
        )
        self.assertFalse(
            run._models_compatible(["qwen/qwen3.5", "deepseek/deepseek-flash"])
        )

    def test_tool_denial_shapes(self):
        # Structured statuses and flags are denials.
        self.assertTrue(
            run._tool_denial({"tool": "read", "state": {"status": "denied"}})
        )
        self.assertTrue(
            run._tool_denial({"tool": "shell", "status": "declined"})
        )
        self.assertTrue(
            run._tool_denial({"tool": "read", "state": {"metadata": {"denied": True}}})
        )
        # A short tool-runner error naming an OS/sandbox denial is a denial.
        self.assertTrue(
            run._tool_denial(
                {"tool": "read", "state": {"status": "error", "error": "Operation not permitted"}}
            )
        )
        self.assertTrue(
            run._tool_denial(
                {"tool": "shell", "state": {"error": "spawnSync git EPERM"}}
            )
        )
        # Ordinary command output is never evidence, even on a failing command:
        # a failing test that prints "permission denied" is not a sandbox denial.
        self.assertIsNone(
            run._tool_denial(
                {
                    "tool": "shell",
                    "state": {
                        "status": "completed",
                        "metadata": {"exit": 1},
                        "output": "The directory '/home/u/.cache/pip' is not writable: permission denied",
                    },
                }
            )
        )
        self.assertIsNone(
            run._tool_denial(
                {
                    "tool": "shell",
                    "state": {"status": "error", "exit_code": 1},
                    "aggregated_output": "grep: denied by policy docs\n",
                }
            )
        )
        # Long transcripts in an error field are command output, not a runner error.
        self.assertIsNone(
            run._tool_denial(
                {
                    "tool": "shell",
                    "state": {"status": "error", "error": ("permission denied\n" + "x" * 500)},
                }
            )
        )
        self.assertIsNone(run._tool_denial({"tool": "read", "state": {"status": "completed"}}))

    def test_codex_completed_turn_with_denial_word_in_output_is_ok(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python3 -m unittest -q",
                    "exit_code": 1,
                    "aggregated_output": (
                        "The directory '/home/u/.cache/pip' or its parent directory "
                        "is not owned or is not writable: permission denied"
                    ),
                },
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer."}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("Final answer.\n", encoding="utf-8")
            return 0, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertIsNone(res["error_class"])
        self.assertEqual(res["denials_observed"], [])
        self.assertEqual((attempt_dir / "result.md").read_text(encoding="utf-8").strip(), "Final answer.")

    def test_codex_structured_denial_is_a_warning_not_a_failure(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "thread.started", "thread_id": "thread-2"},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status", "status": "denied"},
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer despite denial."}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("Answer despite denial.\n", encoding="utf-8")
            return 0, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["denials_observed"]), 1)
        self.assertIn("denied", res["denials_observed"][0])

    def test_codex_failure_names_the_denial_instead_of_empty_result(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "thread.started", "thread_id": "thread-3"},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status", "status": "denied"},
            },
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertIn("denied", res["error"])
        self.assertNotIn("Empty result", res["error"])

    def test_codex_recovered_error_event_is_a_warning(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "error", "message": "stream disconnected before completion; retrying"},
            {"type": "thread.started", "thread_id": "thread-4"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer."}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("Answer.\n", encoding="utf-8")
            return 0, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("disconnected", res["warnings"][0])

    def test_codex_turn_failed_stays_fatal_even_with_an_agent_message(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "thread.started", "thread_id": "thread-5"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Partial answer."}},
            {"type": "turn.failed", "error": {"message": "boom"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("Partial answer.\n", encoding="utf-8")
            return 0, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertIn("boom", res["error"])

    def test_codex_failure_classification_ignores_command_output(self):
        from run import invoke_codex

        attempt_dir = Path(tempfile.mkdtemp())
        events = [
            {"type": "thread.started", "thread_id": "thread-6"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "pytest",
                    "exit_code": 1,
                    "aggregated_output": "429 Too Many Requests: quota exceeded",
                },
            },
        ]

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 1, "\n".join(json.dumps(event) for event in events), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_codex(
                binary="echo",
                route={"route_id": "codex/openai/gpt-6.1-sol", "model_id": "gpt-6.1-sol"},
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "failed")

    def test_claude_output_with_leading_noise_is_parsed(self):
        from run import invoke_claude_code

        attempt_dir = Path(tempfile.mkdtemp())
        payload = {
            "session_id": "claude-session",
            "is_error": False,
            "subtype": "success",
            "result": "CLAUDE ANSWER",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "modelUsage": {"claude-sonnet-5": {}},
        }

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, "Warning: config {deprecated}\n" + json.dumps(payload), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_claude_code(
                binary="echo",
                route={
                    "route_id": "claude-code/anthropic/claude-sonnet-5",
                    "model_id": "claude-sonnet-5",
                    "canonical_key": "claude-sonnet-5",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(
            (attempt_dir / "result.md").read_text(encoding="utf-8").strip(), "CLAUDE ANSWER"
        )

    def test_claude_auxiliary_model_does_not_fail_the_answer(self):
        from run import invoke_claude_code

        attempt_dir = Path(tempfile.mkdtemp())
        payload = {
            "session_id": "claude-session",
            "is_error": False,
            "subtype": "success",
            "result": "CLAUDE ANSWER",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "modelUsage": {"claude-opus-5-5": {}, "claude-sonnet-5": {}},
        }

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, json.dumps(payload), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_claude_code(
                binary="echo",
                route={
                    "route_id": "claude-code/anthropic/claude-sonnet-5",
                    "model_id": "claude-sonnet-5",
                    "canonical_key": "claude-sonnet-5",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["observed_model"], "claude-sonnet-5")

    def test_claude_missing_requested_model_is_still_rejected(self):
        from run import invoke_claude_code

        attempt_dir = Path(tempfile.mkdtemp())
        payload = {
            "session_id": "claude-session",
            "is_error": False,
            "subtype": "success",
            "result": "CLAUDE ANSWER",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "modelUsage": {"claude-opus-5-5": {}},
        }

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, json.dumps(payload), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_claude_code(
                binary="echo",
                route={
                    "route_id": "claude-code/anthropic/claude-sonnet-5",
                    "model_id": "claude-sonnet-5",
                    "canonical_key": "claude-sonnet-5",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "model_unavailable")

    def test_unexpected_capability_error_is_no_option(self):
        self.assertEqual(
            run.classify_exception(
                ValueError("Explicit route 'x' failed eligibility check (sandbox_unsupported:antigravity:read-only).")
            ),
            ("no_option", 1),
        )

    def test_claude_unparsable_output_is_a_format_failure(self):
        from run import invoke_claude_code

        attempt_dir = Path(tempfile.mkdtemp())

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, "this is not json", "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_claude_code(
                binary="echo",
                route={
                    "route_id": "claude-code/anthropic/claude-sonnet-5",
                    "model_id": "claude-sonnet-5",
                    "canonical_key": "claude-sonnet-5",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "format")
        self.assertIn("not valid JSON", res["error"])

    def test_antigravity_lowercase_success_status_is_accepted(self):
        from run import invoke_antigravity

        attempt_dir = Path(tempfile.mkdtemp())
        result = {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "success",
                "response": "AGY ANSWER",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, json.dumps(result), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_antigravity(
                binary="echo",
                route={
                    "route_id": "antigravity/google/gemini-4-argon-high",
                    "model_id": "gemini-4-argon-high",
                    "canonical_key": "gemini-4-argon-high",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="bypass",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(
            (attempt_dir / "result.md").read_text(encoding="utf-8").strip(), "AGY ANSWER"
        )

    def test_antigravity_missing_result_event_is_a_format_failure(self):
        from run import invoke_antigravity

        attempt_dir = Path(tempfile.mkdtemp())

        def mock_run(cmd, prompt="", *args, **kwargs):
            return 0, json.dumps({"event": "step_update", "step": {"status": "running"}}), "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_antigravity(
                binary="echo",
                route={
                    "route_id": "antigravity/google/gemini-4-argon-high",
                    "model_id": "gemini-4-argon-high",
                    "canonical_key": "gemini-4-argon-high",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="bypass",
                cwd=None,
                timeout_sec=5,
                child_env={},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "format")
        self.assertIn("no terminal result event", res["error"])

    def test_opencode_recovered_stream_error_is_a_warning(self):
        from run import invoke_opencode

        attempt_dir = Path(tempfile.mkdtemp())
        export = {
            "messages": [
                {"type": "user", "content": [{"type": "text", "text": "prompt"}]},
                {
                    "type": "assistant",
                    "time": {"created": 0, "completed": 0},
                    "finish": "stop",
                    "model": {"id": "deepseek-flash", "providerID": "deepseek", "variant": "default"},
                    "content": [{"type": "text", "text": "FULL ANSWER"}],
                },
            ]
        }
        captured: dict[str, str] = {}

        def fake_run(cmd, prompt="", *args, **kwargs):
            if cmd[1:3] == ["session", "export"]:
                sent = captured.get("prompt", "")
                nonce = sent[sent.rfind("[cross-subagents turn:") :].strip()
                export["messages"][0]["content"] = [{"type": "text", "text": f"prompt {nonce}"}]
                completed = int(time.time() * 1000)
                export["messages"][1]["time"] = {"created": completed, "completed": completed}
                return 0, json.dumps(export), "", False, True
            if cmd[1:3] == ["session", "delete"]:
                return 0, "", "", False, True
            captured["prompt"] = prompt
            stream = "\n".join(
                [
                    json.dumps(
                        {
                            "type": "step_start",
                            "sessionID": "ses_test",
                            "model": "deepseek/deepseek-flash",
                        }
                    ),
                    json.dumps(
                        {"type": "error", "error": {"message": "provider temporarily unavailable"}}
                    ),
                    json.dumps({"type": "text", "part": {"text": "FULL ANSWER"}}),
                    json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                ]
            )
            return 0, stream, "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=fake_run):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "agent": "opencode",
                    "provider": "deepseek",
                    "model_id": "deepseek-flash",
                    "canonical_key": "deepseek-flash",
                },
                prompt_text="prompt",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=True,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={"cli_major": 2, "has_agent_flag": True, "has_session_cmd": True},
                policy={"allow_shell": True},
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("temporarily unavailable", res["warnings"][0])

    def test_defect_reports_are_written_listed_and_resolved(self):
        report_dir = Path(tempfile.mkdtemp())
        env = {"CROSS_SUBAGENTS_DEFECT_DIR": str(report_dir)}
        with patch.dict(os.environ, env, clear=False):
            created = defects.create_report(
                argparse.Namespace(
                    title="Test defect",
                    summary="Something in the skill misbehaved.",
                    severity="minor",
                    area="scripts/run.py",
                    step="Step 4",
                    observed="observed",
                    expected="expected",
                    reproduction="python3 scripts/run.py ...",
                    impact="cost one attempt",
                    suggested_fix="fix it",
                    evidence="/tmp/evidence",
                    run_id="run-1",
                    task_id="task-1",
                )
            )
            self.assertTrue(Path(created["path"]).is_file())
            text = Path(created["path"]).read_text(encoding="utf-8")
            self.assertIn("# Skill defect: Test defect", text)
            self.assertIn("Something in the skill misbehaved.", text)
            self.assertIn("run-1", text)

            reports = defects.list_reports()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["severity"], "minor")
            self.assertEqual(reports[0]["title"], "Test defect")
            self.assertEqual(reports[0]["run_id"], "run-1")
            self.assertEqual(len(defects.list_reports(run_id="run-1")), 1)
            self.assertEqual(defects.list_reports(run_id="other-run"), [])

            resolved = defects.resolve_report(created["id"], "Fixed.")
            self.assertTrue(Path(resolved["path"]).is_file())
            self.assertIn("## Resolution", Path(resolved["path"]).read_text(encoding="utf-8"))
            self.assertEqual(defects.list_reports(), [])

    def test_defect_report_requires_title_and_summary(self):
        report_dir = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {"CROSS_SUBAGENTS_DEFECT_DIR": str(report_dir)}, clear=False):
            base = dict(
                title="",
                summary="x",
                severity="minor",
                area=None,
                step=None,
                observed=None,
                expected=None,
                reproduction=None,
                impact=None,
                suggested_fix=None,
                evidence=None,
                run_id=None,
                task_id=None,
            )
            with self.assertRaises(ValueError):
                defects.create_report(argparse.Namespace(**base))
            base["title"] = "t"
            base["summary"] = ""
            with self.assertRaises(ValueError):
                defects.create_report(argparse.Namespace(**base))
            base["severity"] = "catastrophic"
            base["summary"] = "s"
            with self.assertRaises(ValueError):
                defects.create_report(argparse.Namespace(**base))

    def test_opencode_conflicting_stream_and_export_models_are_rejected(self):
        from run import invoke_opencode

        attempt_dir = Path(tempfile.mkdtemp())
        now_ms = int(time.time() * 1000)
        export = {
            "messages": [
                {"type": "user", "content": [{"type": "text", "text": "prompt"}]},
                {
                    "type": "assistant",
                    "time": {"created": now_ms, "completed": now_ms},
                    "finish": "stop",
                    "model": {"id": "deepseek-flash", "providerID": "deepseek", "variant": "default"},
                    "content": [{"type": "text", "text": "FULL ANSWER"}],
                },
            ]
        }
        captured: dict[str, str] = {}

        def fake_run(cmd, prompt="", *args, **kwargs):
            if cmd[1:3] == ["session", "export"]:
                sent = captured.get("prompt", "")
                nonce = sent[sent.rfind("[cross-subagents turn:") :].strip()
                export["messages"][0]["content"] = [{"type": "text", "text": f"prompt {nonce}"}]
                return 0, json.dumps(export), "", False, True
            captured["prompt"] = prompt
            stream = "\n".join(
                [
                    json.dumps(
                        {"type": "step_start", "sessionID": "ses_test", "model": "qwen/qwen3.5"}
                    ),
                    json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                ]
            )
            return 0, stream, "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=fake_run):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "agent": "opencode",
                    "provider": "deepseek",
                    "model_id": "deepseek-flash",
                    "canonical_key": "deepseek-flash",
                },
                prompt_text="prompt",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=True,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={"cli_major": 2, "has_agent_flag": True, "has_session_cmd": True},
                policy={"allow_shell": True},
            )
        self.assertEqual(res["status"], "failed")

    def test_opencode_stream_step_after_finish_is_incomplete(self):
        from run import invoke_opencode

        attempt_dir = Path(tempfile.mkdtemp())
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "step_start",
                        "sessionID": "ses_test",
                        "model": "deepseek/deepseek-flash#default",
                    }
                ),
                json.dumps({"type": "text", "part": {"text": "EARLY TEXT"}}),
                json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                json.dumps({"type": "step_start"}),
            ]
        )
        with patch.object(
            run, "run_process_with_timeout", return_value=(0, stream, "", False, True)
        ):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "agent": "opencode",
                    "provider": "deepseek",
                    "model_id": "deepseek-flash",
                    "canonical_key": "deepseek-flash",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={
                    "cli_major": 2,
                    "has_agent_flag": True,
                    "has_session_cmd": False,
                },
                policy={"allow_shell": True},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "format")
        self.assertFalse((attempt_dir / "result.md").exists())

    def test_opencode_model_change_within_turn_is_rejected(self):
        from run import invoke_opencode

        attempt_dir = Path(tempfile.mkdtemp())
        now_ms = int(time.time() * 1000)
        export = {
            "messages": [
                {"type": "user", "content": [{"type": "text", "text": "prompt"}]},
                {
                    "type": "assistant",
                    "time": {"created": now_ms, "completed": now_ms},
                    "finish": "tool-calls",
                    "model": {"id": "qwen3.5", "providerID": "qwen", "variant": "default"},
                    "content": [{"type": "text", "text": "intermediate"}],
                },
                {
                    "type": "assistant",
                    "time": {"created": now_ms, "completed": now_ms},
                    "finish": "stop",
                    "model": {"id": "deepseek-flash", "providerID": "deepseek", "variant": "default"},
                    "content": [{"type": "text", "text": "FINAL ANSWER"}],
                },
            ]
        }
        stream = json.dumps({"type": "step_start", "sessionID": "ses_test"})

        captured: dict[str, str] = {}

        def fake_run(cmd, prompt="", *args, **kwargs):
            if cmd[1:3] == ["session", "export"]:
                sent = captured.get("prompt", "")
                nonce = sent[sent.rfind("[cross-subagents turn:") :].strip()
                export["messages"][0]["content"] = [{"type": "text", "text": f"prompt {nonce}"}]
                return 0, json.dumps(export), "", False, True
            captured["prompt"] = prompt
            return 0, stream, "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=fake_run):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "agent": "opencode",
                    "provider": "deepseek",
                    "model_id": "deepseek-flash",
                    "canonical_key": "deepseek-flash",
                },
                prompt_text="prompt",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=True,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={"cli_major": 2, "has_agent_flag": True, "has_session_cmd": True},
                policy={"allow_shell": True},
            )
        self.assertEqual(res["status"], "failed")
        self.assertFalse((attempt_dir / "result.md").exists())

    def test_opencode_length_finish_is_not_a_completed_review(self):
        from run import invoke_opencode

        attempt_dir = Path(tempfile.mkdtemp())
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "step_start",
                        "model": "deepseek/deepseek-flash#default",
                    }
                ),
                json.dumps({"type": "text", "part": {"text": "NO ACTIONABLE FINDINGS"}}),
                json.dumps({"type": "step_finish", "part": {"reason": "length"}}),
            ]
        )
        with patch.object(
            run, "run_process_with_timeout", return_value=(0, stream, "", False, True)
        ):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "agent": "opencode",
                    "provider": "deepseek",
                    "model_id": "deepseek-flash",
                    "canonical_key": "deepseek-flash",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="read-only",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={
                    "cli_major": 2,
                    "has_agent_flag": True,
                    "has_session_cmd": False,
                },
                policy={"allow_shell": True},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "format")
        self.assertFalse((attempt_dir / "result.md").exists())

    def test_transient_throttle_is_not_exhausted_quota(self):
        self.assertEqual(
            run.classify_failure(
                1, "", "429 Too Many Requests: retry-after: 1"
            ),
            "transient",
        )
        self.assertEqual(
            run.classify_failure(1, "", "429 Too Many Requests: usage limit reached"),
            "quota",
        )
        self.assertEqual(
            run.classify_failure(1, "", "429 insufficient_quota; retry-after: 86400"),
            "quota",
        )
        self.assertEqual(
            run.classify_failure(1, "", "503 Service Unavailable"),
            "transient",
        )
        self.assertEqual(
            run.classify_failure(1, "", "rate_limit_exceeded"), "transient"
        )
        self.assertEqual(
            run.classify_failure(1, "", "429 rate limit reached (TPM): limit 10000"),
            "transient",
        )
        self.assertEqual(run._epoch_ms(1800000000.6), 1800000000600)

    def test_step_spec_adds_to_global_vendor_and_agent_exclusions(self):
        catalog, quota_state = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [{"exclude_vendor": ["deepseek"], "exclude_agent": ["claude-code"]}],
                defaults={"exclude_vendor": ["openai"], "exclude_agent": ["codex"]},
                require_distinct_vendor=False,
            )
        self.assertEqual(plan["status"], "ok")
        constraints = plan["steps"][0]["constraints"]
        self.assertIn("openai", constraints["exclude_vendor"])
        self.assertIn("deepseek", constraints["exclude_vendor"])
        selected = plan["steps"][0]["selected"]
        self.assertNotIn(selected["vendor"], ("openai", "deepseek"))
        self.assertNotIn(selected["agent"], ("codex", "claude-code"))

    def test_step_spec_cannot_reenable_globally_excluded_route(self):
        catalog, quota_state = self._fake_selection_inputs()
        with patch.object(select_route, "get_survey", return_value=None), patch.object(
            select_route, "collect_all_models", return_value=catalog
        ), patch.object(select_route, "collect_quota", return_value=quota_state):
            plan = select_step_plan(
                [
                    {
                        "include_vendor": ["openai"],
                        "exclude_route": ["opencode/openai/gpt-6-astra"],
                    }
                ],
                defaults={
                    "task": "code_review",
                    "include_vendor": ["openai"],
                    "exclude_route": ["codex/openai/gpt-6-astra"],
                },
                require_distinct_vendor=False,
            )
        self.assertEqual(plan["status"], "ok")
        self.assertNotEqual(plan["steps"][0]["selected"]["route_id"], "codex/openai/gpt-6-astra")

    def test_opencode_go_catalog_uses_console_credential(self):
        output = (
            "opencode/glm-5.3\n"
            "opencode/glm-5.3-flash-free\n"
            "opencode-go/glm-5.3\n"
            "opencode-go/qwen3.8-max\n"
        )

        def fake_run(cmd, **kwargs):
            if cmd[1:] == ["models"]:
                return argparse.Namespace(returncode=0, stdout=output, stderr="")
            raise AssertionError(cmd)

        agent_info = {
            "target_usable": True,
            "cli_major": 2,
            "version": "2.2.2",
            "authenticated_providers": [{"provider": "opencode", "auth_method": "oauth"}],
        }
        with patch.object(models, "run_cmd", side_effect=fake_run):
            routes, meta = models.collect_opencode_models("/tmp/opencode", agent_info)
        by_id = {route["route_id"]: route for route in routes}

        go_route = by_id["opencode/opencode-go/glm-5.3"]
        self.assertEqual(go_route["provider"], "opencode-go")
        self.assertEqual(go_route["quota_bucket"], "opencode:opencode-go")
        self.assertEqual(go_route["auth_plan"], "subscription")
        self.assertEqual(go_route["cli_model_arg"], "opencode-go/glm-5.3")
        self.assertFalse(go_route["hidden"])

        # The unfunded Zen duplicate is demoted, and a free Zen-only model stays visible.
        self.assertTrue(by_id["opencode/opencode/glm-5.3"]["hidden"])
        self.assertEqual(
            by_id["opencode/opencode/glm-5.3"]["hidden_reason"], "duplicate-opencode-go"
        )
        self.assertFalse(by_id["opencode/opencode/glm-5.3-flash-free"]["hidden"])
        self.assertEqual(meta["derived_providers"], ["opencode-go"])

        quota_state = quota.build_observed_quota("opencode", agent_info)
        self.assertIn("opencode:opencode-go", quota_state["buckets"])

        # Without the console credential, opencode-go lines stay hidden.
        with patch.object(models, "run_cmd", side_effect=fake_run):
            routes_without_console, meta_without_console = models.collect_opencode_models(
                "/tmp/opencode",
                {
                    "target_usable": True,
                    "cli_major": 2,
                    "version": "2.2.2",
                    "authenticated_providers": [{"provider": "anthropic", "auth_method": "oauth"}],
                },
            )
        self.assertFalse(
            any(route["provider"] == "opencode-go" for route in routes_without_console)
        )
        self.assertEqual(meta_without_console["derived_providers"], [])

    def test_opencode_v1_rejects_agent_dependent_postures(self):
        for kwargs in ({"allow_shell": True}, {"no_tools": True}):
            _cfg, verified = opencode_read_only_config({}, major_ver=1, **kwargs)
            self.assertFalse(verified)

    def test_opencode_v1_is_unusable_and_rejected_before_invocation(self):
        import probe
        from probe import parse_opencode_auth_text
        from run import invoke_opencode

        def fake_run(cmd, **kwargs):
            if cmd[1:] == ["--version"]:
                return argparse.Namespace(returncode=0, stdout="1.19.4\n", stderr="")
            return argparse.Namespace(returncode=1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td, patch.object(
            probe,
            "resolve_agent_binary",
            return_value={
                "installed": True,
                "binary": "/tmp/opencode",
                "all_paths": ["/tmp/opencode"],
                "distinct_targets": ["/tmp/opencode"],
                "duplicate_installs": False,
            },
        ), patch.object(probe, "run_cmd", side_effect=fake_run), patch.object(
            probe, "get_cache_dir", return_value=Path(td)
        ), patch.object(probe, "compute_agent_config_fingerprint", return_value="fp"):
            info = probe.probe_opencode()
        self.assertFalse(info["version_ok"])
        self.assertFalse(info["usable"])
        self.assertFalse(info["host_usable"])
        self.assertFalse(info["target_usable"])
        self.assertTrue(any("2.0.0" in warning for warning in info["warnings"]))

        attempt_dir = Path(tempfile.mkdtemp())
        invoked = False

        def mock_run(*_args, **_kwargs):
            nonlocal invoked
            invoked = True
            return 0, "", "", False, True

        with patch.object(run, "run_process_with_timeout", side_effect=mock_run):
            res = invoke_opencode(
                binary="echo",
                route={
                    "route_id": "opencode/deepseek/deepseek-flash",
                    "model_id": "deepseek-flash",
                    "provider": "deepseek",
                },
                prompt_text="dummy",
                attempt_dir=attempt_dir,
                resume_session_id=None,
                effort=None,
                keep_session=False,
                sandbox_mode="workspace-write",
                cwd=None,
                timeout_sec=5,
                child_env={},
                recipe_flags={"cli_major": 1, "version": "1.19.4", "has_standalone": True},
            )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error_class"], "not_installed")
        self.assertIn("2.0.0", res["error"])
        self.assertFalse(invoked)

        cases = {
            "●  Google api\n●  DeepSeek api\n": {"google", "deepseek"},
            "OpenCode Go     API key                     stored\n"
            "Anthropic       OAuth                       stored\n": {"opencode-go", "anthropic"},
        }
        for text, expected in cases.items():
            providers = parse_opencode_auth_text(text)
            self.assertEqual({p["provider"] for p in providers}, expected)
            self.assertTrue(all(p["auth_method"] in {"api_key", "oauth"} for p in providers))

    def test_review_shell_agent_preserves_network_access(self):
        cfg, verified = opencode_read_only_config(
            {}, major_ver=2, allow_shell=True, allow_network=True
        )
        self.assertTrue(verified)
        permissions = cfg["agents"]["review-shell"]["permissions"]
        self.assertIn(
            {"action": "webfetch", "resource": "*", "effect": "allow"},
            permissions,
        )

    def test_verify_can_exclude_guard_owned_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            baseline = guard_snapshot(root)
            (root / "report.json").write_text("{}", encoding="utf-8")
            self.assertEqual(guard_verify(root, baseline)["status"], "mutated")
            self.assertEqual(
                guard_verify(root, baseline, excludes=["report.json"])["status"], "clean"
            )

            # A file present in the baseline must not be reported as removed
            # merely because verification now excludes it.
            (root / "existing.json").write_text("old", encoding="utf-8")
            baseline_with_report = guard_snapshot(root)
            self.assertEqual(
                guard_verify(root, baseline_with_report, excludes=["existing.json"])["status"],
                "clean",
            )

    def test_guard_fails_closed_on_unresolvable_gitdir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("x", encoding="utf-8")
            (root / ".git").write_text("gitdir: ../missing-gitdir\n", encoding="utf-8")
            baseline = guard_snapshot(root)
            self.assertEqual(baseline["status"], "guard_failed")
            self.assertTrue(any("Git" in err for err in baseline.get("errors") or []))

    def test_normal_exit_detects_surviving_descendant(self):
        if os.name == "nt":
            self.skipTest("POSIX process-group check")
        code, _out, _err, timed_out, cleanup = run.run_process_with_timeout(
            ["sh", "-c", "sleep 1 >/dev/null 2>&1 & exit 0"],
            "",
            None,
            10,
        )
        self.assertEqual(code, 0)
        self.assertFalse(timed_out)
        self.assertFalse(cleanup)

    def test_guard_records_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "target").mkdir()
            (root / "target" / "f.txt").write_text("x", encoding="utf-8")
            (root / "other").mkdir()
            (root / "link").symlink_to("target", target_is_directory=True)
            baseline = guard_snapshot(root)
            self.assertEqual(baseline["status"], "ok")
            self.assertIn("link", baseline["files"])
            (root / "link").unlink()
            (root / "link").symlink_to("other", target_is_directory=True)
            report = guard_verify(root, baseline)
            self.assertEqual(report["status"], "mutated")
            self.assertIn("link", report["changed"])

    def _guarded_execute(self, repo, cache, invoke, task_id):
        route = {
            "route_id": "opencode/deepseek/deepseek-flash",
            "agent": "opencode",
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "canonical_key": "deepseek-flash",
            "official_model_id": "deepseek-flash",
            "quota_bucket": "opencode:deepseek",
            "applicable_buckets": ["opencode:deepseek"],
        }
        with patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": str(cache)}), patch.object(
            run, "resolve_explicit_route", return_value=dict(route)
        ), patch.object(
            run,
            "probe_all",
            return_value={
                "agents": {
                    "opencode": {
                        "usable": True,
                        "binary": "true",
                        "version": "2.2.0",
                        "recipe_flags": {"cli_major": 2, "has_agent_flag": True},
                    }
                }
            },
        ), patch.object(run, "invoke_opencode", side_effect=invoke):
            return run.execute_task(
                run_id="guard-order-run",
                task_id=task_id,
                route_str="opencode/deepseek/deepseek-flash",
                prompt_text="x",
                cwd=str(repo),
                sandbox_mode="read-only",
                force_route=True,
                no_fallback=True,
                guard_enabled=True,
                guard_max_files=1000,
            )

    def test_guard_snapshot_happens_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            cache = Path(td) / "cache"

            def fake_invoke(*args, **kwargs):
                attempt_dir = args[3]
                (repo / "a.txt").write_text("two\n", encoding="utf-8")
                (attempt_dir / "result.md").write_text("review text", encoding="utf-8")
                return {
                    "status": "ok",
                    "exit_code": 0,
                    "session_id": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "requested_model": "deepseek-flash",
                    "resolved_model": "deepseek-flash",
                    "observed_model": "deepseek-flash",
                    "session_cleaned_up": True,
                    "cleanup_verified": True,
                    "error_class": None,
                }

            res = self._guarded_execute(repo, cache, fake_invoke, "t1")

            self.assertEqual(res["status"], "failed")
            self.assertEqual(res["error_class"], "mutated")
            self.assertIsNone(res["result_file"])
            self.assertIn("a.txt", res["guard"]["changed"])
            self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "two\n")

    def test_guard_fails_closed_when_adapter_raises(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            cache = Path(td) / "cache"

            def fake_invoke(*args, **kwargs):
                raise FileNotFoundError("adapter binary vanished")

            res = self._guarded_execute(repo, cache, fake_invoke, "t2")

            self.assertEqual(res["status"], "failed")
            self.assertEqual(res["error_class"], "guard_failed")
            self.assertIsNone(res["result_file"])

    def test_timeout_cleanup_verifies_after_reaping(self):
        if os.name == "nt":
            self.skipTest("POSIX process-group check")
        _code, _out, _err, timed_out, cleanup = run.run_process_with_timeout(
            ["sh", "-c", "sleep 5"],
            "",
            None,
            1,
        )
        self.assertTrue(timed_out)
        self.assertTrue(cleanup)


if __name__ == "__main__":
    unittest.main()
