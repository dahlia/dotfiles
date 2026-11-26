"""Attribution uses canonical model IDs without changing the execution route."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import common
import run


class AttributionModelIdentityTests(unittest.TestCase):
    def test_fast_route_keeps_execution_id(self):
        route = common.normalize_model_route("opencode", "openai", "gpt-6.2-sol-fast")
        self.assertEqual(route["route_id"], "opencode/openai/gpt-6.2-sol-fast")
        self.assertEqual(route["resolved_model_id"], "gpt-6.2-sol-fast")
        self.assertEqual(route["official_model_id"], "gpt-6.2-sol")
        self.assertIsNone(route["variant"])

    def test_report_normalizes_old_metadata_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}):
            run.record_native_task("trailers", "fast", "OpenCode", "gpt-6.2-sol-fast")
            run.record_native_task("trailers", "standard", "OpenCode", "gpt-6.2-sol")
            legacy_meta = run.get_run_dir("trailers") / "fast" / "meta.json"
            meta = common.read_json_file(legacy_meta)
            self.assertEqual(meta["official_model_id"], "gpt-6.2-sol")
            meta["official_model_id"] = "gpt-6.2-sol-fast"
            common.atomic_write_json(legacy_meta, meta)

            report = run.build_run_report("trailers", only_significant=True)
            expected = "Assisted-by: OpenCode:gpt-6.2-sol"
            self.assertEqual(report["assisted_by_trailers"], [expected])
            self.assertIn(expected, run.format_human_run_report(report))
            self.assertNotIn("Assisted-by: OpenCode:gpt-6.2-sol-fast", run.format_human_run_report(report))

    def test_offline_normalizer_used_by_commit_skill(self):
        for raw, expected in (
            ("openai/gpt-6.2-sol-fast#high", "gpt-6.2-sol"),
            ("gpt-6.2-sol", "gpt-6.2-sol"),
            ("example-model-fast", "example-model-fast"),
            ("google/gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),
            ("qwen3.6-plus", "qwen3.6-plus"),
            ("deepseek-flash", "deepseek-flash"),
        ):
            with self.subTest(raw=raw):
                result = subprocess.check_output(
                    [sys.executable, str(SCRIPTS / "models.py"), "normalize-id", "--", raw], text=True,
                )
                self.assertEqual(result.strip(), expected)
