"""Permissions for a resumed OpenCode session must include its requested cwd."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import common
import run


class TestOpenCodeResume(unittest.TestCase):
    def test_timeout_continuation_reuses_retained_session_only_after_cleanup(self):
        route = common.normalize_model_route('opencode', 'opencode', 'glm-5.3-flash-free')
        for cleanup, support, session, keep, expected in [
            (True, True, 'retained', True, True),
            (False, True, 'retained', True, False),
            (True, False, 'retained', True, False),
            (True, True, None, True, False),
            (True, True, 'retained', False, False),
        ]:
            with self.subTest(cleanup=cleanup, support=support, session=session, keep=keep), \
                    tempfile.TemporaryDirectory() as td, patch.dict(
                        os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')}
                    ), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
                        run, 'probe_all', return_value={'agents': {'opencode': {
                            'usable': True, 'binary': 'true', 'version': '2.0.0', 'recipe_flags': {},
                        }}}
                    ), patch.object(run, 'invoke_opencode', return_value={
                        'status': 'timeout', 'exit_code': 124, 'error_class': 'timeout',
                        'session_id': session, 'cleanup_verified': cleanup,
                        'session_cleaned_up': False, 'timeout_resume_supported': support,
                    }) as invoke:
                options = dict(run_id='timeout-resume', cwd=td, sandbox_mode='read-only',
                               allow_shell=True, isolate=True, force_route=True, no_fallback=True,
                               task_category='code_review')
                result = run.execute_task(task_id='r1-a1', route_str=route['route_id'],
                                          keep_session=keep, prompt_text='review', **options)
                self.assertEqual(result['status'], 'timeout')
                self.assertIsNone(result['result_file'])
                self.assertEqual(result['resumable'], expected)
                followup = run.execute_task(task_id='r1-a2', resume_task_id='r1-a1',
                                            prompt_text='continue review', **options)
                self.assertEqual(invoke.call_count, 2 if expected else 1)
                if expected:
                    self.assertIn('retained', invoke.call_args.args)
                    self.assertIn('continue review', invoke.call_args.args[2])
                    self.assertTrue(followup['resumed'])
                else:
                    self.assertEqual(followup['status'], 'failed')

    def test_resume_roots_reach_the_read_only_agent(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')}
        ):
            root = Path(td)
            original, requested = root / 'original', root / 'requested'
            original.mkdir()
            requested.mkdir()
            context = root / 'context'
            context.mkdir()
            context_file = context / 'review.md'
            context_file.write_text('context')
            route = common.normalize_model_route('opencode', 'opencode', 'glm-5.3-flash-free')
            run_dir = run.get_run_dir('resume-roots')
            run.save_coroutine_session(run_dir, 'initial', route, 'session-test', None,
                                       'read-only', str(original), True)

            # Simulate a second resume too: the saved process cwd can already
            # be B while the OpenCode session's tool project is still A.
            for previous, explicit_cwd in [('initial', str(requested)), ('followup', None)]:
                with patch.object(run, 'resolve_explicit_route', return_value=route):
                    result = run.execute_task(
                        run_id='resume-roots', task_id='next-' + previous,
                        resume_task_id=previous, cwd=explicit_cwd,
                        workspace_dirs=[str(requested)], context_files=[str(context_file)],
                        prompt_text='review', sandbox_mode='read-only',
                        allow_shell=True, isolate=True, force_route=True,
                        no_fallback=True, dry_run=True)
                self.assertEqual(result['status'], 'dry_run', result)
                self.assertEqual(result['cwd'], str(requested))
                self.assertIn(str(requested), result['workspace_dirs'])
                self.assertIn(str(context), result['workspace_dirs'])
                cfg, verified = run.opencode_read_only_config(
                    {}, major_ver=2, allow_shell=True, isolate=True,
                    workspace_dirs=result['workspace_dirs'])
                self.assertTrue(verified)
                rules = cfg['agents']['review-shell']['permissions']
                self.assertIn({'action': 'external_directory', 'resource': str(requested) + '/*',
                               'effect': 'allow'}, rules)
                self.assertIn({'action': 'edit', 'resource': '*', 'effect': 'deny'}, cfg['permissions'])
                run.save_coroutine_session(run_dir, 'followup', route, 'session-test', None,
                                           'read-only', str(requested), True)

    def test_raw_resume_preserves_requested_cwd(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')}
        ):
            route = common.normalize_model_route('opencode', 'opencode', 'glm-5.3-flash-free')
            with patch.object(run, 'resolve_explicit_route', return_value=route):
                result = run.execute_task(
                    run_id='raw-resume', task_id='review', route_str=route['route_id'],
                    resume_session_id='session-test', cwd=td, prompt_text='review',
                    sandbox_mode='read-only', force_route=True, no_fallback=True, dry_run=True)
            self.assertIn(td, result['workspace_dirs'])

    def test_fresh_normalization_still_skips_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            child = Path(td) / 'child'
            child.mkdir()
            self.assertEqual(run.normalize_workspace_dirs([td, str(child)], [], td), [])
            self.assertEqual(run.normalize_workspace_dirs([td, str(child)], [], td,
                                                          include_cwd=True), [td])


if __name__ == '__main__':
    unittest.main()
