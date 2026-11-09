"""Provider rejections must not be selected from a broad CLI catalog."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import common
import models
import select_route


class TestUnavailableRoutes(unittest.TestCase):
    def test_collection_marks_exact_rejected_provider_model(self):
        proc = subprocess.CompletedProcess([], 0, stdout=(
            'github-copilot/gemini-4-argon\n'
            'github-copilot/claude-sonnet-5\n'
            'google/gemini-4-argon\n'), stderr='')
        info = {'usable': True, 'cli_major': 2, 'authenticated_providers': [
            {'provider': 'github-copilot', 'auth_method': 'oauth'},
            {'provider': 'google', 'auth_method': 'api_key'}]}
        with patch.object(models, 'run_cmd', return_value=proc):
            routes, _ = models.collect_opencode_models('opencode', info)
        by_id = {r['route_id']: r for r in routes}
        rejected = by_id['opencode/github-copilot/gemini-4-argon']
        self.assertIs(rejected['available'], False)
        self.assertIn('model_not_supported', rejected['unavailable_reason'])
        for route in routes[1:]:
            self.assertIsNone(models.route_unavailability_reason(route))
            self.assertNotEqual(route.get('available'), False)

    def test_old_cache_and_explicit_route_rejected_without_metadata(self):
        route = common.normalize_model_route('opencode', 'github-copilot', 'gemini-4-argon')
        for explicit in (False, True):
            eligible, reason, _, _ = select_route.check_route_eligibility(
                route, 'general', {'buckets': {}, 'agents': {}},
                is_explicit_route=explicit)
            self.assertFalse(eligible)
            self.assertIn('model_unavailable:', reason)

    def test_other_provider_of_same_model_remains_eligible(self):
        route = common.normalize_model_route('opencode', 'google', 'gemini-4-argon')
        eligible, reason, _, _ = select_route.check_route_eligibility(
            route, 'general', {'buckets': {}, 'agents': {}})
        self.assertTrue(eligible, reason)


if __name__ == '__main__':
    unittest.main()
