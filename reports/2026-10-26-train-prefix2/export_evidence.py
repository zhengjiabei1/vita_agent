"""Export this historical, completed training diagnostic without publishing secrets.

Run from the repository root with its original ignored artifacts still present:
    python reports/2026-10-26-train-prefix2/export_evidence.py
No network, model calls, database writes, or synthetic benchmark results are used.
This is a fixed historical export: changed inputs fail before any output is written.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'artifacts' / 'real-evaluation'
DEST = Path(__file__).resolve().parent
SECRET_KEYS = {'api_key', 'apikey', 'access_token', 'refresh_token', 'authorization',
               'proxy_authorization', 'headers', 'extra_headers', 'default_headers',
               'api_token', 'bearer_token', 'password', 'client_secret'}
# Compare in memory only. Secret values are never printed or written.
SECRETS = [v for k in ('VITA_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY')
           if (v := os.environ.get(k)) and len(v) >= 12]
REDACTIONS: dict[str, int] = {}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalized = key.lower().replace('-', '_')
            if normalized in SECRET_KEYS:
                out[key] = '[REDACTED]'
                REDACTIONS[normalized] = REDACTIONS.get(normalized, 0) + 1
            else:
                out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        for secret in SECRETS:
            value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b', '[REDACTED]', value)
        value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*', 'Bearer [REDACTED]', value)
        value = value.replace('/workspace/vita_agent', '<ORIGINAL_PROJECT_ROOT>')
        value = value.replace('/workspace/scratch/vitabench2-reference', '<ORIGINAL_UPSTREAM_ROOT>')
        if value.startswith(('http://', 'https://')):
            try:
                url = urlsplit(value)
                if url.username is not None or url.password is not None:
                    host = url.hostname or ''
                    if url.port:
                        host += f':{url.port}'
                    value = urlunsplit((url.scheme, host, url.path, url.query, url.fragment))
            except ValueError:
                value = '[REDACTED_MALFORMED_URL]'
        return value
    return value


def write(name: str, data) -> None:
    (DEST / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    mappings = {
        'baseline.json': 'train-prefix2/baseline.json',
        'harness.json': 'train-prefix2/harness.json',
        'comparisons.json': 'train-prefix2/comparisons.json',
        'plan.provenance.json': 'train-prefix2/plan.json',
        'execution.provenance.json': 'train-prefix2/execution.json',
        'dataset-audit.json': 'dataset-audit.json',
        'user-splits.json': 'user-splits.json',
        'prefix2-derivation.json': 'prefix2/derivation.json',
    }
    # All checks precede every write. A later run or ledger migration must not
    # silently rewrite this historical report or contaminate its time window.
    expected = json.loads((DEST / 'provenance.json').read_text(encoding='utf-8'))
    previous_summary = json.loads((DEST / 'summary.json').read_text(encoding='utf-8'))
    expected_files = {item['published_file']: item for item in expected['files']}
    if set(expected_files) != set(mappings) or len(expected['files']) != len(mappings):
        raise ValueError('Historical provenance must identify exactly the eight original inputs')
    original_data = {}
    for output_name, relative in mappings.items():
        source = SOURCE / relative
        raw = source.read_bytes()
        record = expected_files[output_name]
        if (record['source_artifact'] != 'artifacts/real-evaluation/' + relative
                or hashlib.sha256(raw).hexdigest() != record['source_sha256']):
            raise ValueError(f'Historical source changed: {relative}; refusing to export')
        original_data[output_name] = json.loads(raw)
    ledger = SOURCE / 'shared-budget.sqlite3'
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(ledger) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('Historical ledger has a live journal; refusing to export')
    ledger_bytes = ledger.read_bytes()
    ledger_digest = hashlib.sha256(ledger_bytes).hexdigest()
    if ledger_digest != previous_summary['cost_accounting']['ledger_sha256']:
        raise ValueError('Historical ledger changed; refusing to mix later calls into this export')
    # Snapshot a validated immutable, read-only DB before any writes. SQLite WAL
    # format cannot be deserialized directly into a memory database.
    connection = sqlite3.connect(':memory:')
    source_connection = sqlite3.connect(f'file:{ledger}?mode=ro&immutable=1', uri=True)
    try:
        source_connection.backup(connection)
    finally:
        source_connection.close()
    if sha(ledger) != ledger_digest or any(
        (sidecar := Path(str(ledger) + suffix)).exists() and sidecar.stat().st_size
        for suffix in ('-wal', '-journal')
    ):
        raise ValueError('Ledger changed while taking the snapshot; refusing to export')
    connection.row_factory = sqlite3.Row
    meta = dict(connection.execute('SELECT limit_nano, currency, pricing_basis FROM budget_meta').fetchone())

    comparison = original_data['comparisons.json']['harness']
    plan = original_data['plan.provenance.json']
    counts = {simulation['reward_info']['info']['num_subtasks']
              for simulation in original_data['baseline.json']['simulations']}
    if len(counts) != 1:
        raise ValueError('Historical cohort no longer has a uniform subtask count')
    variants = [run['variant'] for run in plan['runs']]
    subtasks_per_user = counts.pop()
    cohort = {'split': plan['partition'], 'users': len(plan['selected_user_ids']),
              'subtasks_per_user': subtasks_per_user, 'variants': variants,
              'trials_per_variant': plan['num_trials'],
              'subtask_executions': subtasks_per_user * len(plan['selected_user_ids']) * len(variants) * plan['num_trials'],
              'seed': plan['seed']}
    if cohort != previous_summary['cohort']:
        raise ValueError('Historical cohort metadata changed; refusing to export')
    REDACTIONS.clear()
    provenance = []
    for output_name, relative in mappings.items():
        source = SOURCE / relative
        data = sanitize(original_data[output_name])
        if output_name.endswith('.provenance.json'):
            data['archive_notice'] = 'Historical provenance only. Paths were redacted. Do not execute; create a new plan for the new model/config/source.'
        write(output_name, data)
        provenance.append({'published_file': output_name,
                           'source_artifact': 'artifacts/real-evaluation/' + relative,
                           'source_sha256': expected_files[output_name]['source_sha256'],
                           'published_sha256': sha(DEST / output_name)})

    start = original_data['execution.provenance.json']['started_at'][:19].replace('T', ' ')

    def totals(where='', args=()):
        rows = connection.execute('SELECT state, COUNT(*) AS requests, '
            'SUM(prompt_tokens) AS input_tokens, SUM(completion_tokens) AS output_tokens, '
            'SUM(actual_nano) AS token_price_estimate_nano, SUM(held_nano) AS reserved_or_settled_nano '
            'FROM budget_calls ' + where + ' GROUP BY state', args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ('token_price_estimate_nano', 'reserved_or_settled_nano'):
                item[key.replace('_nano', '_cny')] = None if item[key] is None else item[key] / 1_000_000_000
                del item[key]
            result.append(item)
        return result

    entire = totals()
    completed = totals('WHERE created_at >= ?', (start,))
    connection.close()
    write('summary.json', {
        'schema_version': 1,
        'recorded_date': '2026-10-26',
        'status': 'completed_training_diagnostic_only',
        'upstream_commit': plan['upstream_commit'],
        'model': plan['model']['model'],
        'cohort': cohort,
        'scores': {'baseline_mean': comparison['reward']['baseline_mean'],
                   'harness_mean': comparison['reward']['candidate_mean'],
                   'delta': comparison['reward']['delta'],
                   'update_baseline_mean': comparison['update_subset']['baseline_mean'],
                   'update_harness_mean': comparison['update_subset']['candidate_mean'],
                   'confidence_interval': comparison['reward']['paired_user_bootstrap_95ci'],
                   'agent_cost_overhead_percent': comparison['cost']['overhead_percent']},
        'cost_accounting': {
            'currency': meta['currency'], 'pricing_basis': meta['pricing_basis'],
            'invoice_verified': False,
            'budget_ceiling_cny': meta['limit_nano'] / 1_000_000_000,
            'completed_prefix2_requests': completed,
            'historical_all_requests_including_aborted_pilot': entire,
            'ledger_sha256': ledger_digest,
            'completed_request_assignment': 'Ledger created_at >= prefix2 execution started_at, rounded to seconds; no later experiment calls recorded in this snapshot.',
            'interpretation': 'Configured peak uncached token-price estimates and outstanding reservations, NOT a provider invoice. Caching/off-peak prices may be lower. Missing agent aggregate costs remain null. Unknown in-flight response is retained as uncertain.',
        },
        'excluded_run': {'source': 'artifacts/real-evaluation/train-v1',
                         'status': 'interrupted exploratory pilot',
                         'reason': 'Stopped to preserve the project budget; no completed cohort score is reported.',
                         'included_in_historical_budget': True},
        'limitations': [
            'Only a one-user training prefix, not held-out testing and not the official 771-subtask benchmark.',
            'No performance improvement has been established.',
            'Memory, skills-only and full-system variants have not been evaluated.',
            'No real reflected skill bank has been produced or evaluated; v2 integration is unfinished.',
            'Unit tests and demo outputs are not benchmark performance evidence.',
            'Changing provider/model requires a fresh baseline and new predeclared plans.',
        ],
    })
    write('provenance.json', {'schema_version': 1, 'files': provenance,
          'redacted_field_counts': REDACTIONS,
          'transformations': ['Recursively redact credential/header fields and recognizable credential literals.',
                              'Replace original project/upstream absolute roots with placeholders.',
                              'Mark plan/execution snapshots as archival provenance only.',
                              'Preserve train task content, tool traces, scores, null costs and usage counters.',
                              'Do not publish the dataset, SQLite ledger, logs or interrupted raw run.']})
    print('Exported sanitized training evidence and aggregate budget summary; no model calls.')


if __name__ == '__main__':
    main()
