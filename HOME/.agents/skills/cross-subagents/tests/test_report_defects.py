import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import defects
import run


class TestReportDefects(unittest.TestCase):
    def test_opt_in_report_includes_exact_run_even_when_insignificant(self):
        reports = [{"severity": "major", "title": "Adapter failure", "path": "/cache/defect.md"}]
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), \
                patch.object(defects, "list_reports", return_value=reports) as listing:
            report = run.build_run_report("report-test", only_significant=True, include_defects=True)
        listing.assert_called_once_with(run_id="report-test")
        self.assertEqual(report["defects"], reports)
        text = run.format_human_run_report(report)
        for item in ("major", "Adapter failure", "/cache/defect.md"):
            self.assertIn(item, text)

    def test_default_report_does_not_change_or_query_defects(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": td}), \
                patch.object(defects, "list_reports") as listing:
            report = run.build_run_report("report-test")
        listing.assert_not_called()
        self.assertNotIn("defects", report)

    def test_cli_forwards_opt_in_without_changing_default(self):
        for flags, expected in (([], False), (["--include-defects"], True)):
            with patch.object(sys, "argv", ["run.py", "report", "--run-id", "r", *flags]), \
                    patch.object(run, "build_run_report", return_value={}) as build, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(), 0)
            build.assert_called_once_with("r", only_significant=False, include_defects=expected)
