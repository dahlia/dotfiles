"""Execution honors coordinator quota ownership without skipping eligibility."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import run


class RunQuotaScopeTests(unittest.TestCase):
    def test_explicit_route_collects_only_its_agent_and_honors_ownership(self):
        route = common.normalize_model_route('opencode', 'github-copilot', 'gpt-6.1-sol')
        for options, targets in (({}, ['opencode']),
                                 ({'quota_agents': ['claude-code']}, []),
                                 ({'exclude_quota_agents': ['opencode']}, []),
                                 ({'quota_cache_only': True}, ['opencode'])):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
                'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')
            }), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
                run, 'load_session_pins', return_value={}
            ), patch.object(run, 'collect_quota', return_value={}) as collect, patch.object(
                run, 'check_route_eligibility', return_value=(False, 'quota_exhausted:test', {'exhausted': True}, {})
            ) as eligibility:
                result = run.execute_task(run_id='scoped', task_id='review', route_str=route['route_id'],
                                          prompt_text='review', cwd=td, no_fallback=True, **options)
                self.assertEqual(result['status'], 'quota')
                collect.assert_called_once_with(agents=targets, refresh=False,
                                                cache_only=options.get('quota_cache_only', False))
                eligibility.assert_called_once()

    def test_injected_snapshot_skips_collection_but_is_checked(self):
        route = common.normalize_model_route('opencode', 'github-copilot', 'gpt-6.1-sol')
        snapshot = {'agents': {}, 'buckets': {}}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')
        }), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
            run, 'load_session_pins', return_value={}
        ), patch.object(run, 'collect_quota') as collect, patch.object(
            run, 'check_route_eligibility', return_value=(False, 'quota_exhausted:test', {'exhausted': True}, {})
        ) as eligibility:
            run.execute_task(run_id='supplied', task_id='review', route_str=route['route_id'],
                             prompt_text='review', cwd=td, quota_state=snapshot)
            collect.assert_not_called()
            self.assertIs(eligibility.call_args.kwargs['quota_state'], snapshot)

    def test_saved_plan_ownership_cannot_be_widened_before_preflight(self):
        route = common.normalize_model_route('opencode', 'github-copilot', 'gpt-6.1-sol')
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')
        }), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
            run, 'load_session_pins', return_value={}
        ), patch.object(run, 'load_selection_plan', return_value={'steps': []}), patch.object(
            run, 'planned_constraints_for_route', return_value={
                'quota_agents': ['claude-code'], 'exclude_quota_agents': ['codex'],
                'quota_cache_only': True,
            }
        ), patch.object(run, 'collect_quota', return_value={}) as collect, patch.object(
            run, 'check_route_eligibility', return_value=(False, 'quota_exhausted:test', {'exhausted': True}, {})
        ):
            run.execute_task(run_id='plan', task_id='review', route_str=route['route_id'],
                             prompt_text='review', cwd=td, quota_agents=['opencode', 'claude-code'])
            collect.assert_called_once_with(agents=[], refresh=False, cache_only=True)

    def test_cli_passes_quota_controls_to_execution_and_cost_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / 'quota.json'
            state.write_text(json.dumps({'agents': {}, 'buckets': {}}))
            flags = ['--quota-agent', 'opencode', '--exclude-quota-agent', 'codex',
                     '--quota-cache-only', '--quota-state', str(state)]
            for gate in (False, True):
                argv = ['run.py', *flags, *(['--cost-gate', '2'] if gate else [])]
                with patch.object(sys, 'argv', argv), patch('builtins.print'), patch.object(
                    run, 'execute_task', return_value={'status': 'ok'}
                ) as execute, patch.object(run, 'evaluate_cost_gate', return_value={}) as cost:
                    self.assertEqual(run.main(), 0)
                    called = cost if gate else execute
                    self.assertEqual(called.call_args.kwargs['quota_agents'], ['opencode'])
                    self.assertEqual(called.call_args.kwargs['exclude_quota_agents'], ['codex'])
                    self.assertTrue(called.call_args.kwargs['quota_cache_only'])
                    self.assertEqual(called.call_args.kwargs['quota_state'], {'agents': {}, 'buckets': {}})

    def test_cli_rejects_malformed_quota_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'quota.json'
            for value in ({}, {'buckets': []}, [], {'opencode': {}}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), patch.object(sys, 'argv', [
                    'run.py', '--quota-state', str(path)
                ]), patch('builtins.print'), patch.object(run, 'execute_task') as execute:
                    self.assertEqual(run.main(), 2)
                    execute.assert_not_called()

    def test_automatic_selection_receives_quota_controls(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')
        }), patch.object(run, 'select_routes', return_value={'status': 'unavailable'}) as select:
            run.execute_task(run_id='auto', task_id='review', prompt_text='review', cwd=td,
                             quota_agents=['opencode'], exclude_quota_agents=['codex'],
                             quota_cache_only=True, quota_state={})
            self.assertEqual(select.call_args.kwargs['quota_agents'], ['opencode'])
            self.assertEqual(select.call_args.kwargs['exclude_quota_agents'], ['codex'])
            self.assertTrue(select.call_args.kwargs['quota_cache_only'])
            self.assertEqual(select.call_args.kwargs['quota_state'], {})
