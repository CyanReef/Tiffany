"""Independently verify raw scheduler measurements and acceptance gates."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


def verify_source(source):
    assert len(source['sha256']) == 64
    digest = hashlib.sha256(json.dumps(source['files'], sort_keys=True).encode()).hexdigest()
    assert digest == source['sha256'], 'invalid source manifest'


def validate(data):
    if 'requested_seconds' in data:
        verify_source(data['source'])
        verify_source(data['final_source'])
        assert data['source_unchanged'] and data['source'] == data['final_source']
        assert data['passed'] and data['seconds'] >= data['requested_seconds'] >= 1800
        rows = data['samples']
        assert len(rows) >= 3 and rows[-1]['elapsed_seconds'] > rows[0]['elapsed_seconds']
        assert all(row['work'] == row['contexts'] == 0 for row in rows)
        calls = 0
        previous_elapsed = -1
        for cycle, row in enumerate(rows):
            assert row['cycle'] == cycle and row['elapsed_seconds'] > previous_elapsed
            calls += (128, 512, 4096)[cycle % 3]
            assert row['calls'] == calls
            previous_elapsed = row['elapsed_seconds']
            for key in ('queued', 'active', 'estimated_bytes', 'lanes', 'ready_adapters', 'tasks', 'event_tasks', 'owners'):
                if key in row:
                    assert row[key] == 0, f'resources retained: {key}'
        # Compare the same phase: each cycle has a different burst size.
        for phase in range(3):
            futures = [row['futures'] for row in rows if row['cycle'] % 3 == phase]
            assert len(futures) >= 2 and max(futures) == min(futures), 'Future retention grew'
        return {'passed': True, 'seconds': data['seconds'], 'cycles': len(rows), 'completed_events': rows[-1]['calls']}
    verify_source(data['baseline_source'])
    verify_source(data['candidate_source'])
    assert data['finished_at_utc'] and data['source_unchanged']
    if resume := data.get('resumed_from'):
        path = Path(resume['file'].replace('\\', '/'))
        if not path.exists():
            path = Path(__file__).resolve().parents[1] / path
        if not path.exists():
            # Keep recorded paths/checksums intact when samples are classified.
            root = Path(__file__).resolve().parents[1] / 'benchmarks/results'
            for directory in ('elastic', 'archive/elastic-development'):
                candidate = root / directory / path.name
                if candidate.exists():
                    path = candidate
                    break
        if 'file_sha256' in resume:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == resume['file_sha256']
        previous = json.loads(path.read_text(encoding='utf-8'))
        assert previous['runner_sha256'] == resume['runner_sha256']
        assert previous['environment'] == data['environment']
        assert previous['baseline_source'] == data['baseline_source']
        assert previous['candidate_source'] == data['candidate_source']
        for key, value in previous['method'].items():
            if key != 'repeats':
                assert data['method'][key] == value, f'changed resumed method: {key}'
        assert data['samples'][:len(previous['samples'])] == previous['samples'], 'retained observations changed'
        if resume.get('interrupted'):
            assert not previous.get('finished_at_utc')
            assert previous['method']['repeats'] == data['method']['repeats']
        else:
            assert previous['finished_at_utc'] and previous['source_unchanged']
    samples = data['samples']
    profiles = list(dict.fromkeys(sample['profile'] for sample in samples))
    repeats = data['method']['repeats']
    is_http = 'before_tuned' in profiles
    assert data['method']['metrics'] == 'real, immediate' and data['method']['trace'] is False
    normal_cases = set(row['case'] for row in samples[0]['cases'])
    cases = set(row['case'] for sample in samples for row in sample['cases'])
    index = {}
    for sample in samples:
        verify_source(sample['source'])
        expected = data['candidate_source'] if sample['profile'] == 'after' else data['baseline_source']
        assert sample['source'] == expected
        for row in sample['cases']:
            key = sample['round'], sample['profile'], row['case']
            assert key not in index, f'duplicate observation: {key}'
            index[key] = row
            assert row['seconds'] > 0 and math.isfinite(row['throughput'])
            completed = row['completed'] if is_http else row['events']
            assert math.isclose(row['throughput'], completed / row['seconds'], rel_tol=1e-12)
            if is_http:
                assert row['offered'] == completed + row['rejected']
                assert row['verified_http_calls'] == completed + row.get('warmup_events', 4)
                assert 0 <= row['p95_ms'] <= row['p99_ms']
            else:
                assert row['verified_calls'] >= completed and row['rss_bytes'] > 0
                assert 0 <= row['p95_us'] <= row['p99_us']
    assert set(index) == {(r, p, c) for r in range(repeats) for p in profiles for c in cases}
    assert normal_cases == cases if not is_http else True
    summaries = {}
    for summary in data['summary']:
        selected = [index[r, summary['profile'], summary['case']] for r in range(repeats)]
        for key in selected[0]:
            if isinstance(selected[0][key], (int, float)):
                values = [row[key] for row in selected]
                assert summary[key] == {'median': statistics.median(values), 'min': min(values), 'max': max(values)}
        summaries[summary['profile'], summary['case']] = summary
    assert set(summaries) == {(p, c) for p in profiles for c in cases}
    acceptance = []
    for result in data['acceptance']:
        case = result['case']
        before = summaries['before_tuned' if is_http else 'before', case]
        after = summaries['after', case]
        for key, ratio in result['ratios'].items():
            assert math.isclose(ratio, after[key]['median'] / before[key]['median'], rel_tol=1e-12)
        ratios = result['ratios']
        if is_http:
            no_rejections = after['rejected']['max'] == before['rejected']['max'] == 0
            same_concurrency = after['peak_active'] == before['peak_active']
            assert result['no_rejections'] == no_rejections
            assert result['same_concurrency'] == same_concurrency
            passed = (no_rejections and same_concurrency and ratios['throughput'] >= .95
                      and ratios['p95_ms'] <= 1.10 and ratios['p99_ms'] <= 1.10)
        else:
            assert repeats >= 7 and result['minimum_case_seconds'] >= 2
            minimum_seconds = min(index[r, p, case]['seconds'] for r in range(repeats) for p in profiles)
            assert result['minimum_case_seconds'] == minimum_seconds
            paired = [index[r, 'after', case]['throughput'] / index[r, 'before', case]['throughput'] for r in range(repeats)]
            assert result['paired_throughput_ratios'] == paired
            rng = random.Random(20261005)
            boot = sorted(statistics.median(rng.choices(paired, k=len(paired))) for _ in range(4000))
            interval = [boot[100], boot[3899]]
            assert result['paired_median_95_percent_interval'] == interval
            assert result['throughput_inconclusive'] == (interval[0] < .97 < interval[1])
            if result['throughput_inconclusive']:
                assert repeats >= 15, 'inconclusive results need 15 rounds'
            passed = (not result['throughput_inconclusive'] and minimum_seconds >= 2
                      and ratios['throughput'] >= .97 and ratios['p95_us'] <= 1.05
                      and ratios['p99_us'] <= 1.10 and ratios['rss_bytes'] <= 1.05)
        assert result['passed'] == passed, f'incorrect acceptance gate: {case}'
        # Inconclusive at 15 rounds is not an accepted performance result.
        acceptance.append({'case': case, 'passed': result['passed'] and not result.get('throughput_inconclusive', False)})
    assert set(row['case'] for row in acceptance) == cases
    return {'passed': all(row['passed'] for row in acceptance), 'cases': acceptance,
            'timed_completed_events': sum(row.get('completed', row.get('events', 0)) for row in index.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', type=Path)
    args = parser.parse_args()
    for path in args.paths:
        report = validate(json.loads(path.read_text(encoding='utf-8')))
        print(json.dumps({'file': str(path), **report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
