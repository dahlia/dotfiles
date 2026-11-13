"""Serving aliases must preserve invocation identity and canonical maker identity."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import models


class MiniMaxIdentityTests(unittest.TestCase):
    def test_free_route_preserves_serving_alias(self):
        alias = "minimax-m3.1-flash-free"
        route = common.normalize_model_route("opencode", "opencode", alias)
        self.assertEqual(route["route_id"], "opencode/opencode/" + alias)
        self.assertEqual(route["model_id"], alias)
        self.assertEqual(route["resolved_model_id"], alias)
        self.assertEqual(route["official_model_id"], "minimax-m3.1-flash")
        self.assertEqual(route["canonical_key"], "minimax-m3.1-flash")
        self.assertEqual(models.resolve_official_model_id(
            "opencode/" + alias, all_models={}), "minimax-m3.1-flash")

    def test_only_verified_free_alias_is_normalized(self):
        for raw, expected in (
            ("MiniMax-M3.1-Flash", "minimax-m3.1-flash"),
            ("opencode/minimax-m3.1-flash-free#high", "minimax-m3.1-flash"),
            ("minimax-other-free", "minimax-other-free"),
            ("deepseek-r1:free", "deepseek-r1-free"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(common.normalize_official_model_id(raw)[0], expected)


if __name__ == "__main__":
    unittest.main()
