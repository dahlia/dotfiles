"""Provider usage and credential checks use the connected account, without inference."""
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common
import quota
import select_route


class OpenCodeProviderQuotaTests(unittest.TestCase):
    def test_console_go_limits_and_direct_provider_checks_are_independent(self):
        account = {"authenticated_providers": [
            {"provider": "opencode"}, {"provider": "deepseek"},
            {"provider": "kimi-code-plan-global"},
        ]}
        reset = 1795000000
        go = {"data": {"go": {
            "rolling": {"limited": True, "percent": 100, "resetsAt": reset * 1000},
            "weekly": {"limited": True, "percent": 100, "resetsAt": (reset + 3600) * 1000},
        }}}
        kimi = {
            "usage": {"limit": "100", "used": "100", "resetTime": "2026-11-20T00:00:00Z"},
            "usages": {
                "limit_7d": {"used_ratio": 0, "reset_time": "2026-11-20T00:00:00Z"},
                "limit_month_code": {"used_ratio": 0.3, "reset_time": "2026-12-01T00:00:00Z"},
            },
        }

        def fetch(url, key):
            self.assertEqual(key, "active-key")
            return (200, kimi) if "kimi.ai" in url else (401, {"secret": "never cache this"})

        def cli(cmd, timeout):
            if cmd[1] == "api":
                return CompletedProcess(cmd, 0, json.dumps(go), "")
            return CompletedProcess(cmd, 0, json.dumps([
                {"integrationID": cmd[-1], "active": False, "value": {"type": "key", "key": "other"}},
                {"integrationID": cmd[-1], "active": True, "value": {"type": "key", "key": "active-key"}},
            ]), "")

        with patch.object(quota, "run_cmd", side_effect=cli), patch.object(
            quota, "_fetch_account_usage", side_effect=fetch
        ), patch.object(quota.time, "time", return_value=reset - 300) as clock:
            state = quota.query_opencode_quota("opencode", account)
            buckets = state["buckets"]
            self.assertEqual(buckets["opencode:deepseek"]["auth_status"], "rejected")
            self.assertEqual(buckets["opencode:kimi-code-plan-global"]["used_percent"], 100)
            self.assertEqual(set(buckets["opencode:kimi-code-plan-global"]["windows"]), {"limit7d", "monthCode"})
            self.assertNotIn("active-key", json.dumps(state))
            self.assertNotIn("never cache this", json.dumps(state))
            route = common.normalize_model_route("opencode", "opencode-go", "deepseek-v4.1-flash")
            self.assertTrue(select_route.evaluate_route_quota(route, buckets, False)["exhausted"])
            clock.return_value = reset + 1
            self.assertTrue(select_route.evaluate_route_quota(route, buckets, True)["exhausted"])
            clock.return_value = reset + 3600
            self.assertFalse(select_route.evaluate_route_quota(route, buckets, False)["exhausted"])

    def test_deepseek_empty_balance_blocks_without_inventing_usage_percentage(self):
        with patch.object(quota, "_active_opencode_key", return_value="key"), patch.object(
            quota, "_fetch_account_usage", return_value=(200, {"is_available": False})
        ):
            state = quota.query_opencode_quota("opencode", {"authenticated_providers": [{"provider": "deepseek"}]})
        account = state["buckets"]["opencode:deepseek"]
        self.assertTrue(account["exhausted"])
        self.assertIsNone(account["used_percent"])

    def test_observed_auth_rejection_expires_or_yields_to_newer_verification(self):
        route = common.normalize_model_route("opencode", "deepseek", "deepseek-v4.1-flash")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CROSS_SUBAGENTS_CACHE_DIR": tmp}), patch.object(
            quota, "compute_agent_config_fingerprint", return_value="account"
        ), patch.object(quota.time, "time", return_value=1000) as clock:
            quota.record_auth_rejection(route["quota_bucket"], "HTTP 401 token expired", "2.4.2")
            state = quota.overlay_observations({})
            self.assertTrue(select_route.evaluate_route_quota(route, state["buckets"], True)["auth_rejected_buckets"])
            verified = {"buckets": {route["quota_bucket"]: {
                "auth_status": "verified", "auth_checked_at_epoch": 1001,
            }}}
            state = quota.overlay_observations(verified)
            self.assertFalse(select_route.evaluate_route_quota(route, state["buckets"], False)["auth_rejected_buckets"])
            clock.return_value = 1300
            self.assertEqual(quota.overlay_observations({})["buckets"], {})
