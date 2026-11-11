"""Offline orchestration tests. Synthetic scores here are NEVER lab results."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evals'))
import resume_evals as resume


CONFIG = {'provider': 'openrouter', 'model': 'test/model:free',
          'judge_provider': 'openrouter', 'judge_model': 'test/model:free',
          'max_steps': 8, 'dataset': 'core'}
CASES = [{'id': str(i), 'question': f'question {i}', 'expected': f'expected {i}'}
         for i in range(3)]
MANIFEST = {'revision': 'test-revision', 'files_sha256': {'agent.py': 'hash'},
            'case_ids': ['0', '1', '2'], 'controller_sha256': 'controller'}


def scored(case, score=70, status='completed'):
    item = resume.placeholder(case, CONFIG)
    item['result']['status'] = status
    item['grade'] = {'status': 'scored', 'score': score}
    return item


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.output = self.folder / 'checkpoint.json'
        self.original = self.folder / 'input.json'
        self.items = [scored(CASES[0], 0, 'max_steps'),
                      resume.placeholder(CASES[1], CONFIG)]
        resume.atomic_save(self.original, {'config': CONFIG, 'cases': self.items})
        self.report = resume.prepare_report(CONFIG, MANIFEST, CASES, ROOT,
                                            self.original, MANIFEST['revision'])
        self.grading = resume.grading_module(ROOT)
        resume.refresh_summary(self.report, self.grading)
        self.quota = Mock(return_value={'free_model_daily_requests': {'remaining': 30}})

    def run_batch(self, run_one, **kwargs):
        resume.execute_batch(self.report, CASES, ROOT, self.output, self.grading,
                             run_one=run_one, check_quota=self.quota, **kwargs)

    def test_skips_scored_zero_and_includes_missing_case(self):
        seen = []
        def run_one(root, manifest, case, config, remaining):
            seen.append(case['id'])
            return {'item': scored(case), 'provider_events': []}
        self.run_batch(run_one)
        self.assertEqual(seen, ['1', '2'])
        self.assertEqual(self.report['cases'][0], self.items[0])
        self.assertEqual(self.report['summary']['scored_cases'], 3)

    def test_config_source_and_controller_mismatch(self):
        for key, value in [('model', 'different:free'), ('judge_model', 'other:free'),
                           ('max_steps', 16), ('dataset', 'bonus')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'config'):
                resume.validate_checkpoint(self.report, {**CONFIG, key: value}, MANIFEST, CASES)
        for key, value in [('revision', 'wrong'), ('files_sha256', {'agent.py': 'wrong'}),
                           ('controller_sha256', 'wrong')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'manifest'):
                resume.validate_checkpoint(self.report, CONFIG, {**MANIFEST, key: value}, CASES)

    def test_legacy_attestation_and_dataset_validation(self):
        with self.assertRaisesRegex(ValueError, 'legacy-revision'):
            resume.prepare_report(CONFIG, MANIFEST, CASES, ROOT, self.original)
        for mutate in ('duplicate', 'unknown', 'expected', 'model'):
            bad = deepcopy(self.items)
            if mutate == 'duplicate':
                bad.append(deepcopy(bad[0]))
            elif mutate == 'unknown':
                bad[0]['id'] = 'unknown'
            else:
                bad[0][mutate] = 'wrong'
            resume.atomic_save(self.original, {'config': CONFIG, 'cases': bad})
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                resume.prepare_report(CONFIG, MANIFEST, CASES, ROOT,
                                      self.original, MANIFEST['revision'])

    def test_checkpoint_after_each_case_preserves_old_attempt_and_input(self):
        before = self.original.read_bytes()
        count = 0
        def run_one(root, manifest, case, config, remaining):
            nonlocal count
            count += 1
            if count == 2:
                saved = resume.read_json(self.output)
                self.assertEqual(saved['cases'][1]['grade']['score'], 70)
                raise KeyboardInterrupt()
            return {'item': scored(case)}
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(run_one)
        saved = resume.read_json(self.output)
        history = saved['provenance']['attempts']['1']
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]['item']['grade']['score'])
        self.assertEqual(history[1]['replaces'], history[0]['attempt_id'])
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(saved['provenance']['batches'][-1]['status'], 'interrupted')
        self.assertIsNone(saved['summary']['score_0_100'])
        resume.validate_checkpoint(saved, CONFIG, MANIFEST, CASES)

    def test_stops_batch_at_provider_429_and_keeps_previous_scores(self):
        def rate_limit(root, manifest, case, config, remaining):
            return {'item': resume.placeholder(case, CONFIG),
                    'provider_events': [{'event': 'llm_provider_error', 'http_status': 429}]}
        run_one = Mock(side_effect=rate_limit)
        self.run_batch(run_one)
        run_one.assert_called_once()
        self.assertEqual(self.report['cases'][0]['grade']['score'], 0)
        self.assertIsNone(self.report['summary']['score_0_100'])
        self.assertEqual(len(self.report['provenance']['attempts']['1']), 2)
        self.assertEqual(self.report['provenance']['attempts']['2'], [])

    def test_zero_quota_prevents_worker_and_saves_stop(self):
        self.quota.side_effect = RuntimeError('Quota exhausted')
        run_one = Mock()
        with self.assertRaises(RuntimeError):
            self.run_batch(run_one)
        run_one.assert_not_called()
        saved = resume.read_json(self.output)
        self.assertEqual(saved['provenance']['batches'][-1]['status'], 'stopped_error')
        self.assertIsNone(saved['summary']['score_0_100'])

    def test_unscored_judge_stops_batch_even_without_http_error(self):
        run_one = Mock(return_value={'item': resume.placeholder(CASES[1], CONFIG)})
        self.run_batch(run_one)
        run_one.assert_called_once()
        self.assertEqual(self.report['provenance']['batches'][-1]['status'],
                         'stopped_unscored_or_provider_error')

    def test_case_limit_and_resume_do_not_run_completed_cases(self):
        run_one = Mock(side_effect=lambda root, manifest, case, config, remaining:
                       {'item': scored(case)})
        self.run_batch(run_one, max_cases=1)
        self.assertEqual(resume.pending_cases(self.report), ['2'])
        self.assertIsNone(self.report['summary']['score_0_100'])
        self.report = resume.read_json(self.output)
        self.run_batch(run_one)
        self.assertEqual([c.args[2]['id'] for c in run_one.call_args_list], ['1', '2'])
        self.assertIsNotNone(self.report['summary']['score_0_100'])

    def test_atomic_replace_failure_preserves_existing_report(self):
        resume.atomic_save(self.output, self.report)
        before = self.output.read_bytes()
        with patch.object(resume.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                resume.atomic_save(self.output, {'wrong': 'new report'})
        self.assertEqual(self.output.read_bytes(), before)

    def test_input_cannot_be_output(self):
        code = resume.main(['--source-root', str(ROOT), '--revision', 'HEAD',
                            '--input', str(self.original), '--output', str(self.original)])
        self.assertEqual(code, 1)

    def test_entire_core_and_bonus_datasets_stay_incomplete_until_all_scored(self):
        for dataset, filename, count in [('core', 'evals/cases.json', 22),
                                         ('bonus', 'bonus/cases.json', 10)]:
            cases = resume.read_json(ROOT / filename)
            config = {**CONFIG, 'dataset': dataset}
            report = resume.prepare_report(config, MANIFEST, cases, ROOT)
            resume.refresh_summary(report, self.grading)
            self.assertEqual(report['summary']['total_cases'], count)
            self.assertIsNone(report['summary']['score_0_100'])
            if dataset == 'bonus':
                self.assertIsNone(report['summary']['bonus_points_0_15'])

    def test_bad_existing_output_is_never_overwritten(self):
        resume.atomic_save(self.output, {'better': 'unrelated file to preserve'})
        before = self.output.read_bytes()
        with patch.object(resume, 'source_manifest', return_value=(MANIFEST, CASES)), \
                patch.object(resume, 'git', return_value=b'test-revision\n'):
            code = resume.main(['--source-root', str(ROOT), '--revision', 'HEAD',
                                '--input', str(self.original), '--legacy-revision', 'HEAD',
                                '--output', str(self.output), '--initialize'])
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_bytes(), before)


class PreflightTests(unittest.TestCase):
    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-secret'})
    def test_zero_or_unknown_quota_never_checks_model_or_calls_llm(self):
        for quota in ({'remaining': 0}, {}, {'remaining': -1}):
            with patch.object(resume, 'get_openrouter_json', return_value={'data': {
                    'free_model_daily_requests': quota}}) as get:
                with self.assertRaises(RuntimeError):
                    resume.preflight(CONFIG)
                get.assert_called_once_with('key', 'test-secret')

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-secret'})
    def test_valid_free_model_and_safe_metadata(self):
        responses = [{'data': {'label': 'secret', 'free_model_daily_requests': {
            'remaining': 10, 'used': 40, 'limit': 50}, 'is_free_tier': True}},
            {'data': [{'id': CONFIG['model'], 'supported_parameters': ['tools']}]}]
        with patch.object(resume, 'get_openrouter_json', side_effect=responses):
            checked = resume.preflight(CONFIG)
        self.assertEqual(checked['free_model_daily_requests']['remaining'], 10)
        self.assertNotIn('secret', json.dumps(checked))

    def test_paid_or_changed_provider_is_rejected_before_network(self):
        for config in ({**CONFIG, 'model': 'paid/model'}, {**CONFIG, 'judge_provider': 'openai'}):
            with patch.object(resume, 'get_openrouter_json') as get:
                with self.assertRaises(ValueError):
                    resume.preflight(config)
                get.assert_not_called()

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-secret'})
    def test_missing_model_or_tool_support_does_not_fallback(self):
        for models in ([], [{'id': CONFIG['model'], 'supported_parameters': []}]):
            with patch.object(resume, 'get_openrouter_json', side_effect=[
                    {'data': {'free_model_daily_requests': {'remaining': 10}}}, {'data': models}]):
                with self.assertRaises(ValueError):
                    resume.preflight(CONFIG)

    def test_provider_exception_chain_is_safe_and_opens_circuit(self):
        original = Mock()
        def fail(*args):
            try:
                raise urllib.error.HTTPError('https://secret-url', 429, 'secret body', {}, None)
            except urllib.error.HTTPError as exc:
                raise RuntimeError('secret key') from exc
        original.complete.side_effect = fail
        monitor = resume.ProviderMonitor(Mock(return_value=original), remaining=3)
        provider = monitor.factory('openrouter', CONFIG['model'])
        with self.assertRaises(RuntimeError):
            provider.complete([], [])
        with self.assertRaises(RuntimeError):
            provider.complete([], [])
        original.complete.assert_called_once()
        self.assertEqual(monitor.events[0]['http_status'], 429)
        self.assertNotIn('secret', json.dumps(monitor.events))

    def test_known_remaining_is_shared_between_agent_and_judge(self):
        original = Mock()
        monitor = resume.ProviderMonitor(Mock(return_value=original), remaining=1)
        agent = monitor.factory('openrouter', CONFIG['model'])
        judge = monitor.factory('openrouter', CONFIG['judge_model'])
        agent.complete([], [])
        with self.assertRaisesRegex(RuntimeError, 'quota exhausted'):
            judge.complete([], [])
        original.complete.assert_called_once()
        self.assertEqual(monitor.events[-1]['event'], 'llm_quota_exhausted')


class IsolatedWorkerTests(unittest.TestCase):
    """Real subprocess and Git checkout; fake provider never accesses network."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for name in ('status_tools.py', 'bonus/__init__.py', 'bonus/tools.py',
                     'evals/run_evals.py', 'evals/scenarios.py', 'evals/grading.py'):
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        for name in ('evals/cases.json', 'bonus/cases.json'):
            (cls.root / name).write_text(json.dumps(CASES), encoding='utf-8')
        (cls.root / 'fixtures').mkdir()
        for name in ('github', 'cloudflare'):
            (cls.root / 'fixtures' / f'{name}.json').write_text('{}', encoding='utf-8')
        (cls.root / 'agent.py').write_text('''
def run_agent(question, provider, tools, max_steps):
    try:
        reply = provider.complete([], ["tool schema sentinel"])
        return {"status": "completed", "answer": reply["message"]["content"], "trace": [], "usage": []}
    except RuntimeError:
        return {"status": "error", "answer": "", "trace": [], "usage": []}
''', encoding='utf-8')
        (cls.root / 'providers.py').write_text('''
import json
class Provider:
    def __init__(self, name, model): pass
    def complete(self, messages, schemas):
        text = "ISOLATED_CHECKOUT_AGENT" if schemas else json.dumps({
            "factuality":40,"completeness":25,"evidence":20,"boundaries":15,
            "reason":"synthetic test only","improvement":"none"})
        return {"finish":"stop","calls":[],"message":{"content":text},"usage":{}}
''', encoding='utf-8')
        def command(*args):
            subprocess.run(['git', '-c', f'safe.directory={cls.root.as_posix()}',
                            '-C', str(cls.root), *args], check=True, capture_output=True)
        command('init')
        command('add', '.')
        command('-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid',
                'commit', '-m', 'Synthetic isolated test source')
        cls.revision = resume.git(cls.root, 'rev-parse', 'HEAD').decode().strip()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_worker_imports_pinned_checkout_even_when_parent_has_main_agent(self):
        manifest, cases = resume.source_manifest(self.root, self.revision, 'core')
        result = resume.run_worker(self.root, manifest, cases[0], CONFIG, 10)
        self.assertEqual(result['item']['result']['answer'], 'ISOLATED_CHECKOUT_AGENT')
        self.assertEqual(Path(result['agent_path']), (self.root / 'agent.py').resolve())
        self.assertEqual(result['item']['grade']['score'], 100)
        self.assertFalse((self.root / '__pycache__').exists())

    def test_wrong_revision_dirty_source_and_deleted_source_are_rejected(self):
        with self.assertRaises(ValueError):
            resume.source_manifest(self.root, 'missing-revision', 'core')
        path = self.root / 'agent.py'
        original = path.read_bytes()
        try:
            path.write_bytes(original + b'\n# dirty\n')
            with self.assertRaisesRegex(ValueError, 'Uncommitted'):
                resume.source_manifest(self.root, self.revision, 'core')
            path.unlink()
            with self.assertRaises(OSError):
                resume.source_manifest(self.root, self.revision, 'core')
        finally:
            path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
