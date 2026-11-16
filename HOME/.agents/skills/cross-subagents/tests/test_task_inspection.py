"""Regression tests for compact receipt inspection; no real agent calls."""
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
import task_inspection
from common import SCHEMA_VERSION


class TestTaskInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.task = Path(self.tmp.name) / "runs" / "r" / "t"
        self.task.mkdir(parents=True)
        self.meta = {
            "schema_version": SCHEMA_VERSION,
            "run_id": "r", "task_id": "t", "status": "ok", "exit_code": 0,
            "requested_model": "test-model", "observed_model": "test-model",
            "model_verified": True, "tool_policy": {}, "sandbox_mode": "read-only",
            "result_file": str(self.task / "result.md"),
        }
        (self.task / "result.md").write_text("Verified answer.")

    def inspect(self):
        (self.task / "meta.json").write_text(json.dumps(self.meta))
        return task_inspection.inspect_task("r", "t")

    def test_success_points_to_answer_without_echoing_it(self):
        receipt = self.inspect()
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["result_file"], str(self.task / "result.md"))
        self.assertNotIn("Verified answer.", task_inspection.format_inspection(receipt))

    def test_failed_turn_ignores_stale_success_and_preserves_partial(self):
        partial = self.task / "attempts" / "attempt-2" / "partial_result.md"
        partial.parent.mkdir(parents=True)
        partial.write_text("Incomplete answer.")
        self.meta.update(status="failed", error_class="permission",
                         attempt_result_file=str(partial), exit_code=1)
        receipt = self.inspect()
        self.assertIsNone(receipt["result_file"])
        self.assertEqual(receipt["partial_result_file"], str(partial))

    def test_denials_warnings_and_maintenance_are_lossless_and_advisory(self):
        self.meta.update(
            denials_observed=[{"tool": "shell", "reason": "EPERM"}],
            warnings=["recovered CLI error"],
            maintenance_notice={"file": "scripts/run.py"},
            cleanup_note="Cannot delete this CLI's session",
        )
        receipt = self.inspect()
        self.assertEqual(receipt["status"], "ok")
        rendered = task_inspection.format_inspection(receipt)
        for key in ("denials_observed", "warnings", "maintenance_notice", "cleanup_note"):
            self.assertEqual(receipt[key], self.meta[key])
        for value in ("EPERM", "recovered CLI error", "scripts/run.py", "Cannot delete"):
            self.assertIn(value, rendered)

    def test_guard_failure_and_unenforced_tools_remain_visible(self):
        self.meta.update(guard={"status": "mutated", "changed": ["source.py"]},
                         tool_policy={"no_tools": True}, tools_enforced=False,
                         model_verified=False)
        receipt = self.inspect()
        self.assertEqual(len(receipt["issues"]), 3)
        self.assertIn("source.py", task_inspection.format_inspection(receipt))

    def test_missing_empty_and_external_symlink_final_result_fail_closed(self):
        result = self.task / "result.md"
        for content in (None, "", " \n\t"):
            if result.exists():
                result.unlink()
            if content is not None:
                result.write_text(content)
            self.assertIsNone(self.inspect()["result_file"])
        result.unlink()
        external = Path(self.tmp.name) / "secret.md"
        external.write_text("Not a task answer")
        result.symlink_to(external)
        receipt = self.inspect()
        self.assertIsNone(receipt["result_file"])
        self.assertIn("escapes", " ".join(receipt["issues"]))

    def test_external_partial_pointer_is_not_followed(self):
        external = Path(self.tmp.name) / "outside.md"
        external.write_text("External text")
        self.meta.update(status="failed", attempt_result_file=str(external))
        self.assertIsNone(self.inspect()["partial_result_file"])

    def test_wrong_identity_and_malformed_metadata_are_errors(self):
        for value in ([], {}, {"status": "ok"}, {"status": "ok", "run_id": "other", "task_id": "t"}):
            (self.task / "meta.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                task_inspection.inspect_task("r", "t")

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            task_inspection.inspect_task("../r", "t")
        with self.assertRaises(ValueError):
            task_inspection.inspect_task("r", "../t")

    def test_cli_nonzero_for_failed_missing_and_empty_results(self):
        for status in ("failed", "ok"):
            self.meta["status"] = status
            (self.task / "result.md").write_text("")
            self.inspect()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(task_inspection.main(["--run-id", "r", "--task-id", "t"]), 1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(task_inspection.main(["--run-id", "missing", "--task-id", "t"]), 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "inspection_failed")

    def test_inspection_does_not_mutate_receipt(self):
        self.inspect()
        before = {str(p): p.read_bytes() for p in self.task.rglob("*") if p.is_file()}
        task_inspection.inspect_task("r", "t")
        after = {str(p): p.read_bytes() for p in self.task.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_run_cli_dispatches_to_inspection_without_execution(self):
        import run
        self.inspect()
        with patch.object(sys, "argv", ["run.py", "inspect", "--run-id", "r", "--task-id", "t"]), \
                patch.object(run, "execute_task") as execute, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.main(), 0)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")

    def test_success_respects_final_pointer_not_stale_local_text(self):
        for pointer in (None, "/outside/result.md", "missing.md"):
            self.meta["result_file"] = pointer
            self.assertIsNone(self.inspect()["result_file"])
        del self.meta["result_file"]
        self.assertIsNone(self.inspect()["result_file"])

    def test_native_receipts_explicitly_allow_legacy_local_answer(self):
        del self.meta["result_file"]
        self.meta["native_host_execution"] = True
        self.assertEqual(self.inspect()["result_file"], str(self.task / "result.md"))

    def test_external_metadata_symlink_is_rejected(self):
        external = Path(self.tmp.name) / "outside-meta.json"
        external.write_text(json.dumps(self.meta))
        (self.task / "meta.json").symlink_to(external)
        with self.assertRaises(ValueError):
            task_inspection.inspect_task("r", "t")

    def test_regular_files_required_without_opening_fifo(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("POSIX FIFO test")
        (self.task / "result.md").unlink()
        os.mkfifo(self.task / "result.md")
        self.assertIsNone(self.inspect()["result_file"])
        (self.task / "meta.json").unlink()
        os.mkfifo(self.task / "meta.json")
        with self.assertRaises(ValueError):
            task_inspection.inspect_task("r", "t")

    def test_active_publication_fails_immediately_then_reads_new_generation(self):
        from common import file_lock
        self.inspect()
        with file_lock(self.task / "_task.lock"):
            # Simulate the writer's result-before-metadata publication interval.
            (self.task / "result.md").write_text("New generation answer")
            with self.assertRaises(TimeoutError):
                task_inspection.inspect_task("r", "t")
            self.meta["observed_model"] = "new-model"
            (self.task / "meta.json").write_text(json.dumps(self.meta))
        self.assertEqual(task_inspection.inspect_task("r", "t")["observed_model"], "new-model")

    def test_unsupported_schema_is_rejected(self):
        self.meta["schema_version"] = 999
        with self.assertRaises(ValueError):
            self.inspect()
