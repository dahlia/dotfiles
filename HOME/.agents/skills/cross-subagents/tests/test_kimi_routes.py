"""Kimi coding credentials and aliases must retain their billing and identity."""
import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import models
import select_route


class KimiRouteTests(unittest.TestCase):
    def test_membership_key_is_subscription_but_platform_key_is_metered(self):
        info = {"target_usable": True, "cli_major": 2, "authenticated_providers": [
            {"provider": "kimi-code-plan-global", "auth_method": "api_key"},
            {"provider": "moonshot", "auth_method": "api_key"},
        ]}
        proc = argparse.Namespace(returncode=0, stderr="", stdout=(
            "kimi-code-plan-global/k3.1-pro\nmoonshot/kimi-k3.1-pro\n"))
        with patch.object(models, "run_cmd", return_value=proc):
            routes, _ = models.collect_opencode_models("opencode", info)
        membership, platform = routes
        self.assertEqual(membership["auth_plan"], "subscription")
        self.assertEqual(select_route.classify_route_billing(membership), "subscription")
        self.assertEqual(platform["auth_plan"], "api_key")
        self.assertEqual(select_route.classify_route_billing(platform), "api_metered")

    def test_coding_alias_normalizes_without_changing_invocation(self):
        for alias in ("k3.1-pro", "k3.1-pro-highspeed"):
            with self.subTest(alias=alias):
                route = common.normalize_model_route(
                    "opencode", "kimi-code-plan-global", alias + "#default")
                self.assertEqual(route["model_id"], alias)
                self.assertEqual(route["route_id"],
                                 "opencode/kimi-code-plan-global/" + alias + "#default")
                self.assertEqual(route["official_model_id"], "kimi-" + alias)
                self.assertEqual(route["canonical_key"], "kimi-" + alias)
                self.assertEqual(models.resolve_official_model_id(
                    "kimi-code-plan-global/" + alias + "#default", all_models={}), "kimi-" + alias)


if __name__ == "__main__":
    unittest.main()
