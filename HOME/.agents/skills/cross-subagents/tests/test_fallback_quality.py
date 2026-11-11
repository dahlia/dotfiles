"""Explicit routes must not silently downgrade when quota runs out."""
import sys
import tempfile
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

    def test_legacy_saved_plan_cannot_replace_haiku_with_sonnet(self):
        haiku = common.normalize_model_route("opencode", "anthropic", "claude-haiku-5-5")
        same_model = common.normalize_model_route("claude-code", "anthropic", "claude-haiku-5-5")
        sonnet = common.normalize_model_route("antigravity", "anthropic", "claude-sonnet-5-5-high")
        plan = {"steps": [{"route_id": haiku["route_id"], "constraints": {
            "max_tier": None, "max_cost_index": 0.1, "quality_floor": 0.65,
            "exclude_vendor": ["openai"], "speed": "fast",
        }}]}
        with patch.object(run, "load_selection_plan", return_value=plan), patch.object(
            select_route, "get_survey", return_value=None
        ):
            policy = self.policy(haiku)
            policy.pop("scope")
            self.assertFalse(policy["use_saved_pins"])
            for alternatives, expected in (([sonnet], None), ([sonnet, same_model], same_model["route_id"])):
                catalog = {r["agent"]: {"usable": True, "routes": [r]} for r in alternatives}
                selection = select_route.select_routes("prose", all_models=catalog,
                    quota_state={"buckets": {}, "agents": {}}, **policy)
                self.assertEqual((selection.get("selected") or {}).get("route_id"), expected)

    def test_step_pins_survive_plan_save_load_and_fallback(self):
        haiku = common.normalize_model_route("opencode", "anthropic", "claude-haiku-5-5")
        haiku["quota"] = {"applicable_buckets": haiku["applicable_buckets"]}
        pins = {"pin_agent": "opencode", "pin_provider": "anthropic",
                "pin_model": "claude-haiku-5-5"}
        with tempfile.TemporaryDirectory() as td, patch.object(
            select_route, "get_cache_dir", return_value=Path(td)
        ), patch.object(select_route, "collect_all_models", return_value={}), patch.object(
            select_route, "collect_quota", return_value={}
        ), patch.object(select_route, "select_routes", return_value={
            "status": "ok", "selected": haiku, "rationale": "pinned", "fallbacks": []
        }):
            select_route.select_step_plan([pins], run_id="quality-test")
            saved = select_route.load_selection_plan("quality-test")
            for key, value in pins.items():
                self.assertEqual(saved["steps"][0]["constraints"].get(key), value)
            policy = self.policy(haiku, max_tier=2)
            for key, value in pins.items():
                self.assertEqual(policy.get(key), value)

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
