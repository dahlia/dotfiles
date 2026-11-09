"""Explicit routes must not silently downgrade when quota runs out."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import run
import select_route


class FallbackQualityTests(unittest.TestCase):
    def policy(self, route, **overrides):
        args = dict(run_id="quality-test", exclude_vendors=None,
                    exclude_agents=None, include_vendors=None, max_tier=None,
                    min_context=None, require_modalities=None, require_vision=False,
                    allow_estimated_capabilities=True, allow_micro_quota=True,
                    min_headroom=None, speed="normal", exclude_route_ids=None)
        args.update(overrides)
        return run.resolve_pinned_fallback_constraints(route, **args)

    def test_exhausted_flagship_never_falls_back_to_free_flash(self):
        flagship = common.normalize_model_route("opencode", "opencode-go", "glm-5.3")
        flash = common.normalize_model_route("opencode", "opencode", "glm-5.3-flash-free")
        same_model = common.normalize_model_route("opencode", "zhipu", "glm-5.3")
        quota = {"buckets": {"opencode:opencode-go": {
            "exhausted": True, "status": "exhausted", "used_percent": 100,
        }}, "agents": {"opencode": {}}}
        with patch.object(run, "load_selection_plan", return_value={}), patch.object(
            select_route, "get_survey", return_value=None
        ):
            policy = self.policy(flagship)
            policy.pop("scope")
            # Identical family priors must not make the flash model equivalent.
            for alternatives, expected in (([flash], None), ([flash, same_model], same_model["route_id"])):
                catalog = {"opencode": {"usable": True, "routes": [flagship, *alternatives]}}
                selected = select_route.select_routes(
                    "architecture", all_models=catalog, quota_state=quota, **policy
                )
                self.assertEqual((selected.get("selected") or {}).get("route_id"), expected)
                self.assertFalse(any(r["route_id"] == flash["route_id"] for r in selected["fallbacks"]))

    def test_explicit_tier_bound_allows_model_substitution(self):
        route = common.normalize_model_route("opencode", "opencode-go", "glm-5.3")
        with patch.object(run, "load_selection_plan", return_value={}):
            policy = self.policy(route, max_tier=2)
        self.assertIsNone(policy["pin_model"])
        self.assertEqual(policy["max_tier"], 2)

    def test_saved_plan_retains_its_quality_constraints(self):
        route = common.normalize_model_route("opencode", "opencode-go", "glm-5.3")
        with patch.object(run, "load_selection_plan", return_value={"steps": [1]}), patch.object(
            run, "planned_constraints_for_route", return_value={"quality_floor": 0.9, "max_tier": 1}
        ):
            policy = self.policy(route)
        self.assertEqual(policy["quality_floor"], 0.9)
        self.assertEqual(policy["max_tier"], 1)
        self.assertNotIn("pin_model", policy)


if __name__ == "__main__":
    unittest.main()
