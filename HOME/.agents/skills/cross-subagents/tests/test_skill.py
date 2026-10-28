#!/usr/bin/env python3
"""Automated regression and verification test suite for cross-subagents skill."""

import contextlib
import io
import json
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
from select_route import evaluate_route_traits, check_route_eligibility, select_routes, evaluate_route_quota
import run
from run import (
    prepare_prompts,
    record_native_task,
    mark_task_outcome,
    build_run_report,
    format_human_run_report,
    resolve_explicit_route,
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


if __name__ == "__main__":
    unittest.main()
