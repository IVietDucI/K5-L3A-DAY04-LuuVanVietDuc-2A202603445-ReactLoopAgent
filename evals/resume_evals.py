"""Checkpointed eval batches. Default: offline validation/plan, no API calls.

The worker imports the ORIGINAL runner from a pinned, clean source checkout in
a fresh isolated Python process. This file never imports the working agent.
See evals/RESUME.md for PowerShell commands and legacy provenance limitations.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid


VERSION = 1
CONFIG_KEYS = ('provider', 'model', 'judge_provider', 'judge_model', 'max_steps', 'dataset')
sys.dont_write_bytecode = True  # Never write __pycache__ into the baseline checkout.


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def git(root, *args):
    # Per-command trust for the explicitly selected checkout; no global edits.
    proc = subprocess.run(['git', '-c', f'safe.directory={root.as_posix()}',
                           '-C', str(root), *args], capture_output=True)
    if proc.returncode:
        raise ValueError('Git could not verify the selected source checkout.')
    return proc.stdout


def source_manifest(root, revision, dataset):
    root = Path(root).resolve()
    rev = git(root, 'rev-parse', '--verify', f'{revision}^{{commit}}').decode().strip()
    if git(root, 'rev-parse', 'HEAD').decode().strip() != rev:
        raise ValueError('Source HEAD does not match --revision.')
    # All runtime Python and JSON inputs, including optional local dependencies.
    # Tests and this orchestration helper do not alter the original eval engine.
    def runtime_file(name):
        p = Path(name)
        return ((p.parent == Path('.') and p.suffix == '.py') or
                (name.startswith(('evals/', 'bonus/', 'fixtures/')) and
                 p.suffix in ('.py', '.json'))) and not p.name.startswith('test_') and p.name != 'resume_evals.py'

    committed_names = git(root, 'ls-tree', '-r', '--name-only', rev).decode().splitlines()
    names = {name for name in committed_names if runtime_file(name)}
    # Reject extra runtime files as well as changed/deleted tracked files.
    local = [*root.glob('*.py')]
    for directory in ('evals', 'bonus', 'fixtures'):
        local.extend((root / directory).rglob('*'))
    names.update(p.relative_to(root).as_posix() for p in local if p.is_file() and
                 runtime_file(p.relative_to(root).as_posix()))
    hashes = {}
    for name in sorted(names):
        path = root / name
        committed = git(root, 'show', f'{rev}:{name}')
        # Git on Windows may check out CRLF; hash normalized Python/JSON text.
        actual = path.read_bytes().replace(b'\r\n', b'\n')
        if actual != committed.replace(b'\r\n', b'\n'):
            raise ValueError(f'Uncommitted runtime source/data: {name}')
        hashes[name] = digest(actual)
    required = ['agent.py', 'providers.py', 'status_tools.py', 'evals/run_evals.py',
                'evals/grading.py', 'evals/scenarios.py', 'evals/cases.json',
                'bonus/tools.py', 'bonus/cases.json', 'fixtures/github.json',
                'fixtures/cloudflare.json']
    if any(name not in hashes for name in required):
        raise ValueError('Source checkout is missing required runtime/data files.')
    cases = read_json(root / ('bonus/cases.json' if dataset == 'bonus' else 'evals/cases.json'))
    ids = [c['id'] for c in cases]
    if len(ids) != len(set(ids)) or not cases:
        raise ValueError('Dataset contains duplicate IDs or is empty.')
    manifest = {'revision': rev, 'files_sha256': hashes, 'case_ids': ids,
                'controller_sha256': digest(Path(__file__).read_bytes().replace(b'\r\n', b'\n'))}
    return manifest, cases


def grading_module(root):
    spec = importlib.util.spec_from_file_location('_resume_original_grading',
                                                Path(root) / 'evals/grading.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_item(item, case, config):
    for key in ('id', 'question', 'expected'):
        if item.get(key) != case[key]:
            raise ValueError(f'Case {case["id"]}: dataset/{key} mismatch.')
    if item.get('scenario', 'normal') != case.get('scenario', 'normal'):
        raise ValueError(f'Case {case["id"]}: scenario mismatch.')
    for key in CONFIG_KEYS[:4]:
        if item.get(key) != config[key]:
            raise ValueError(f'Case {case["id"]}: {key} mismatch.')
    grade = item.get('grade', {})
    score = grade.get('score')
    if score is not None and (type(score) not in (int, float) or
                              not math.isfinite(score) or not 0 <= score <= 100 or
                              grade.get('status') != 'scored'):
        raise ValueError(f'Case {case["id"]}: invalid scored grade.')
    if not isinstance(item.get('result'), dict):
        raise ValueError(f'Case {case["id"]}: missing result.')


def placeholder(case, config):
    return {**{k: case[k] for k in ('id', 'question', 'expected')},
            **{k: config[k] for k in CONFIG_KEYS[:4]},
            'scenario': case.get('scenario', 'normal'),
            'result': {'status': 'not_run', 'answer': '', 'trace': [], 'usage': []},
            'grade': {'score': None, 'status': 'unscored', 'reason': 'Not run yet.'}}


def refresh_summary(report, grading):
    report['summary'] = grading.summarize(report['cases'])
    if report['config']['dataset'] == 'bonus':
        report['summary']['bonus_points_0_15'] = grading.bonus_points(
            report['summary']['score_0_100'], True)


def prepare_report(config, manifest, cases, source_root, input_path=None, legacy_revision=None):
    report = {'config': config, 'cases': [], 'provenance': {
        'version': VERSION, 'manifest': manifest, 'source_root': str(Path(source_root).resolve()),
        'created_at': now(), 'multi_batch': True, 'batches': [], 'attempts': {},
        'active_attempts': {}, 'origin': None}}
    imported = {}
    if input_path:
        original = read_json(input_path)
        if original.get('config') != config:
            raise ValueError('Input config mismatch; do not mix model/judge/steps/dataset.')
        if 'provenance' in original:
            validate_checkpoint(original, config, manifest, cases)
            return original
        if legacy_revision != manifest['revision']:
            raise ValueError('Legacy input requires --legacy-revision matching the pinned source.')
        report['provenance']['origin'] = {
            'path': str(Path(input_path).resolve()), 'sha256': digest(Path(input_path).read_bytes()),
            'legacy_revision_attested': legacy_revision,
            'limitation': 'Historical revision supplied by operator; old runner recorded no source hashes. '
                          'Current pinned checkout verified; historical source cannot be retroactively proven.'}
        known = {c['id']: c for c in cases}
        for item in original['cases']:
            cid = item['id']
            if cid not in known or cid in imported:
                raise ValueError('Unknown or duplicate input case ID.')
            validate_item(item, known[cid], config)
            imported[cid] = item
    for case in cases:
        cid = case['id']
        report['cases'].append(deepcopy(imported.get(cid, placeholder(case, config))))
        report['provenance']['attempts'][cid] = []
        if cid in imported:
            aid = f'legacy:{cid}:1'
            report['provenance']['attempts'][cid].append({
                'attempt_id': aid, 'batch_id': 'legacy-import', 'replaces': None,
                'item': deepcopy(imported[cid]), 'source': 'legacy input; see origin'})
            report['provenance']['active_attempts'][cid] = aid
    return report


def validate_checkpoint(report, config, manifest, cases):
    prov = report.get('provenance', {})
    if report.get('config') != config or prov.get('version') != VERSION:
        raise ValueError('Checkpoint config/version mismatch.')
    if prov.get('manifest') != manifest:
        raise ValueError('Checkpoint source/data/controller manifest mismatch.')
    if [i['id'] for i in report['cases']] != [c['id'] for c in cases]:
        raise ValueError('Checkpoint dataset IDs/order mismatch.')
    for item, case in zip(report['cases'], cases):
        validate_item(item, case, config)
        history = prov['attempts'][case['id']]
        active = prov['active_attempts'].get(case['id'])
        if history:
            if history[-1]['attempt_id'] != active or history[-1]['item'] != item:
                raise ValueError('Checkpoint active attempt does not match history.')
        elif active or item != placeholder(case, config):
            raise ValueError('Checkpoint has a result without attempt provenance.')


def pending_cases(report):
    # max_steps is a scored zero in the original rubric, and must stay scored.
    return [item['id'] for item in report['cases'] if item['grade']['score'] is None]


def atomic_save(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def get_openrouter_json(endpoint, key):
    request = urllib.request.Request('https://openrouter.ai/api/v1/' + endpoint,
                                     headers={'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'OpenRouter preflight HTTP {exc.code}; no LLM call made.') from None
    except (OSError, ValueError):
        raise RuntimeError('OpenRouter preflight failed; no LLM call made.') from None


def preflight(config):
    # Deliberately limited to the authorized free OpenRouter setup. No paid
    # fallback and no generic "assume quota" bypass for other providers.
    if any(config[k] != 'openrouter' for k in ('provider', 'judge_provider')):
        raise ValueError('Execution supports OpenRouter free models only; offline planning remains available.')
    models = {config['model'], config['judge_model']}
    if any(not m.endswith(':free') or m == 'openrouter/free' for m in models):
        raise ValueError('Execution requires fixed :free model IDs; no paid/automatic routing.')
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        raise ValueError('Missing OPENROUTER_API_KEY in this terminal.')
    data = get_openrouter_json('key', key)['data']
    quota = data.get('free_model_daily_requests') or {}
    remaining = quota.get('remaining')
    if type(remaining) is not int or remaining <= 0:
        raise RuntimeError('Free daily quota exhausted or unavailable; no LLM call made.')
    catalogue = {m['id']: m for m in get_openrouter_json('models', key)['data']}
    for model in models:
        if model not in catalogue:
            raise ValueError('Configured model is unavailable; no model substitution allowed.')
    if 'tools' not in catalogue[config['model']].get('supported_parameters', []):
        raise ValueError('Agent model does not advertise tool calling.')
    return {'checked_at': now(), 'is_free_tier': data.get('is_free_tier'),
            'free_model_daily_requests': {k: quota.get(k) for k in ('used', 'limit', 'remaining')},
            'models': sorted(models)}


class ProviderMonitor:
    """Observe provider errors outside the baseline agent; never inspect tools.

    Stop on the first provider failure (including 429), without sleeping or
    retrying. Exception text/body/headers are deliberately not retained.
    """
    def __init__(self, provider_class, remaining=None):
        self.provider_class = provider_class
        self.events = []
        self.blocked = False
        self.remaining = remaining

    def factory(self, name, model):
        monitor = self
        original = self.provider_class(name, model)

        class ObservedProvider:
            def complete(self, messages, schemas):
                if monitor.blocked:
                    raise RuntimeError('Provider circuit open; batch stopped.')
                if monitor.remaining is not None:
                    if monitor.remaining <= 0:
                        monitor.events.append({'event': 'llm_quota_exhausted',
                                               'provider': name, 'model': model})
                        monitor.blocked = True
                        raise RuntimeError('Known free quota exhausted; no LLM call made.')
                    monitor.remaining -= 1
                try:
                    return original.complete(messages, schemas)
                except Exception as exc:
                    code, seen = None, set()
                    current = exc
                    while current is not None and id(current) not in seen:
                        seen.add(id(current))
                        if isinstance(current, urllib.error.HTTPError):
                            code = current.code
                            current.close()
                            break
                        current = current.__cause__ or current.__context__
                    monitor.events.append({'event': 'llm_provider_error', 'provider': name,
                                           'model': model, 'http_status': code,
                                           'exception_type': type(exc).__name__})
                    monitor.blocked = True
                    raise RuntimeError(f'LLM provider failed (HTTP {code}); batch stopped.') from None

        return ObservedProvider()


def worker(request):
    root = Path(request['source_root']).resolve()
    actual, _ = source_manifest(root, request['manifest']['revision'], request['config']['dataset'])
    if actual != request['manifest']:
        raise ValueError('Worker source manifest mismatch.')
    sys.path[:0] = [str(root), str(root / 'evals')]
    import run_evals
    import agent
    if Path(agent.__file__).resolve() != root / 'agent.py':
        raise ValueError('Worker imported the wrong agent.')
    monitor = ProviderMonitor(run_evals.Provider, request['remaining'])
    run_evals.Provider = monitor.factory
    args = SimpleNamespace(**request['config'], bonus=request['config']['dataset'] == 'bonus')
    item = run_evals.run_case(request['case'], args)
    return {'item': item, 'provider_events': monitor.events,
            'agent_path': str(Path(agent.__file__).resolve())}


def run_worker(root, manifest, case, config, remaining):
    request = {'source_root': str(root), 'manifest': manifest, 'case': case,
               'config': config, 'remaining': remaining}
    proc = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '_worker'],
                          input=json.dumps(request), text=True, encoding='utf-8',
                          capture_output=True, cwd=root)
    if proc.returncode:
        # Do not echo subprocess output: it could contain exception request data.
        raise RuntimeError('Isolated worker failed; checkpoint retained; no subsequent case run.')
    return json.loads(proc.stdout)


def execute_batch(report, cases, root, output, grading, run_one=run_worker,
                  check_quota=preflight, max_cases=None):
    batch = {'batch_id': str(uuid.uuid4()), 'started_at': now(), 'status': 'running',
             'attempt_ids': [], 'preflights': []}
    report['provenance']['batches'].append(batch)
    atomic_save(output, report)
    by_id = {case['id']: case for case in cases}
    try:
        for index, cid in enumerate(pending_cases(report)):
            if max_cases is not None and index >= max_cases:
                batch['status'] = 'case_limit'
                break
            # Check before EVERY case; worker stops on any provider failure.
            quota = check_quota(report['config'])
            batch['preflights'].append(quota)
            print(f'Running {cid}', flush=True)
            outcome = run_one(root, report['provenance']['manifest'], by_id[cid],
                              report['config'], quota['free_model_daily_requests']['remaining'])
            item = outcome['item']
            validate_item(item, by_id[cid], report['config'])
            history = report['provenance']['attempts'][cid]
            aid = f'{batch["batch_id"]}:{cid}:{len(history) + 1}'
            attempt = {'attempt_id': aid, 'batch_id': batch['batch_id'], 'recorded_at': now(),
                       'replaces': report['provenance']['active_attempts'].get(cid),
                       'item': deepcopy(item), 'provider_events': outcome.get('provider_events', []),
                       'agent_path': outcome.get('agent_path')}
            history.append(attempt)
            report['provenance']['active_attempts'][cid] = aid
            pos = next(i for i, old in enumerate(report['cases']) if old['id'] == cid)
            report['cases'][pos] = item
            batch['attempt_ids'].append(aid)
            refresh_summary(report, grading)
            atomic_save(output, report)
            print(f'{cid}: {item["grade"]["score"]}', flush=True)
            if outcome.get('provider_events') or item['grade']['score'] is None:
                batch['status'] = 'stopped_unscored_or_provider_error'
                break
        else:
            batch['status'] = 'complete'
    except (Exception, KeyboardInterrupt) as exc:
        batch['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'stopped_error'
        # Keep only exception type; never persist credentials or response text.
        batch['error_type'] = type(exc).__name__
        raise
    finally:
        batch['ended_at'] = now()
        refresh_summary(report, grading)
        atomic_save(output, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--revision', required=True, help='Exact source commit, not a moving branch')
    parser.add_argument('--input', type=Path, help='Immutable old report or checkpoint')
    parser.add_argument('--legacy-revision', help='Explicit historical revision attestation for old reports')
    parser.add_argument('--output', type=Path, required=True)
    for key in CONFIG_KEYS[:4]:
        parser.add_argument('--' + key.replace('_', '-'))
    parser.add_argument('--max-steps', type=int)
    parser.add_argument('--dataset', choices=['core', 'bonus'])
    parser.add_argument('--max-cases', type=int)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--initialize', action='store_true', help='Save initial checkpoint offline')
    action.add_argument('--execute', action='store_true', help='Explicitly enable real API calls')
    args = parser.parse_args(argv)
    try:
        root, output = args.source_root.resolve(), args.output.resolve()
        if args.input and args.input.resolve() == output:
            raise ValueError('--output must differ from immutable --input.')
        base = read_json(args.input)['config'] if args.input else {}
        config = {k: getattr(args, k) if getattr(args, k) is not None else base.get(k)
                  for k in CONFIG_KEYS}
        config['dataset'] = config['dataset'] or 'core'
        config['max_steps'] = config['max_steps'] if config['max_steps'] is not None else (16 if config['dataset'] == 'bonus' else 8)
        if not all(config[k] for k in CONFIG_KEYS[:4]) or config['max_steps'] < 1:
            raise ValueError('Specify provider/model/judge and max_steps >= 1.')
        if args.max_cases is not None and args.max_cases < 1:
            raise ValueError('--max-cases must be >= 1.')
        manifest, cases = source_manifest(root, args.revision, config['dataset'])
        legacy = (git(root, 'rev-parse', '--verify', f'{args.legacy_revision}^{{commit}}').decode().strip()
                  if args.legacy_revision else None)
        prepared = prepare_report(config, manifest, cases, root, args.input, legacy)
        if output.exists():
            report = read_json(output)
            validate_checkpoint(report, config, manifest, cases)
            if report['provenance']['origin'] != prepared['provenance']['origin']:
                raise ValueError('Existing output belongs to a different input.')
        else:
            report = prepared
        grading = grading_module(root)
        refresh_summary(report, grading)
        print(json.dumps({'revision': manifest['revision'], 'config': config,
                          'pending': pending_cases(report), 'summary': report['summary'],
                          'action': 'execute' if args.execute else 'offline'}, indent=2))
        if args.initialize or (args.execute and pending_cases(report)):
            # Exclusive lock prevents simultaneous writers and duplicate calls.
            output.parent.mkdir(parents=True, exist_ok=True)
            lock = output.with_suffix(output.suffix + '.lock')
            with lock.open('x', encoding='utf-8') as stream:
                stream.write(str(os.getpid()))
            try:
                # Reload under the lock to avoid using a stale pre-lock report.
                if output.exists():
                    report = read_json(output)
                    validate_checkpoint(report, config, manifest, cases)
                    if report['provenance']['origin'] != prepared['provenance']['origin']:
                        raise ValueError('Existing output belongs to a different input.')
                if args.initialize:
                    atomic_save(output, report)
                elif pending_cases(report):
                    execute_batch(report, cases, root, output, grading, max_cases=args.max_cases)
            finally:
                lock.unlink()
            if args.execute:
                return 0 if report['summary']['score_0_100'] is not None else 1
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f'Resume stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if sys.argv[1:] == ['_worker']:
        print(json.dumps(worker(json.load(sys.stdin)), ensure_ascii=False))
    else:
        sys.exit(main())
