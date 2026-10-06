"""Isolated before/after elastic-scheduler checks, HTTP loads and retention soak.

Run with a benchmark environment containing psutil and aiohttp. Neither is a
new core dependency. The baseline must be saved before editing production code.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gc
import hashlib
import json
import logging
import math
import os
from importlib.metadata import version
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
from time import perf_counter, perf_counter_ns, process_time, sleep

ROOT = Path(__file__).resolve().parents[1]
NORMAL = ('one_handler', 'ten_handlers', 'unmatched_1000', 'reads_16', 'text_64k', 'onebot_frames')


def fingerprint(root):
    files = [root / name for name in ('settings.py', 'application.py', 'fields.py')]
    for directory in ('core', 'adapters', 'clients', 'shared', 'deployment', 'hooks'):
        files.extend((root / directory).rglob('*.py'))
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {'sha256': digest, 'files': hashes}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def percentile(values, fraction):
    values = sorted(values)
    at = (len(values) - 1) * fraction
    low = int(at)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (at - low)


def raw_event(index, session, text='hello'):
    return {'time': 1700000000, 'self_id': 12345, 'post_type': 'message',
            'message_type': 'private', 'sub_type': 'friend', 'user_id': 20000 + session,
            'message_id': index, 'message': [{'type': 'text', 'data': {'text': text}}],
            'raw_message': text, 'font': 0, 'sender': {'user_id': 20000 + session, 'nickname': 'bench'}}


class Engine:
    async def setup(self, case, profile='default', url=None, sessions=1):
        from core import Bot, Envelope
        from adapters.onebot_fields import register_onebot_fields
        from fields import TEXT
        self.bot, self.Envelope = Bot(), Envelope
        self.case, self.calls, self.characters = case, 0, 0
        self.peak_active = 0
        self.unmatched = 0
        self.text = 'hello' + 'x' * (65536 - 5) if case == 'text_64k' else 'hello'
        self.matching = 10 if case == 'ten_handlers' else 1
        self.done = asyncio.Event()
        self.http = None
        if url:
            import aiohttp
            self.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=64),
                                             timeout=aiohttp.ClientTimeout(total=120))
        self.url = url
        async def handle(ctx):
            text = ctx.resolve(TEXT)
            if case == 'reads_16':
                for _ in range(15):
                    assert ctx.resolve(TEXT) == text
            assert text == self.text
            if self.http:
                self.peak_active = max(self.peak_active, self.bot.runtime.scheduler.active)
                async with self.http.get(self.url) as response:
                    assert response.status == 200 and await response.text() == 'hello'
                    assert int(response.headers['X-Service-Delay-Ns']) >= self.delay_ns
            self.calls += 1
            self.characters += len(text)
            self.done.set()
        async def unrelated(ctx):
            self.unmatched += 1
        for i in range(self.matching):
            self.bot.register_hook(handle, on='message', name=f'matching_{i}')
        for i in range(1000 if case == 'unmatched_1000' else 0):
            self.bot.register_hook(unrelated, on='notice', name=f'unmatched_{i}')
        self.adapter = None
        if case == 'onebot_frames':
            from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
            from clients import OneBotWebSocketClient
            self.adapter = OneBotWebSocketAdapter('127.0.0.1', 0, 'onebot', adapter_id='benchmark')
            await self.adapter.setup(self.bot.runtime)
            self.adapter._accepting_events = True
            self.client = OneBotWebSocketClient(None)
        else:
            register_onebot_fields(self.bot, 'onebot')
        if profile == 'tuned':
            scheduler = self.bot.runtime.scheduler
            scheduler.capacity = 8192
            if hasattr(scheduler, 'session_backlog'):
                scheduler.session_backlog = 8192
            scheduler.global_limit = 64
            scheduler.configure_adapter('benchmark', 64)
        await self.bot.start()

    async def emit(self, index, session):
        raw = raw_event(index, session, self.text)
        if self.adapter:
            self.done.clear()
            await self.adapter._process_frame(json.dumps(raw), self.client, 'offline')
            await self.done.wait()
        else:
            await self.bot.emit(self.Envelope('onebot', raw, kind='message', adapter_id='benchmark',
                                             connection_id='offline', session_id=f'private:{session}'))

    def admit(self, index, session):
        envelope = self.Envelope('onebot', raw_event(index, session, self.text), kind='message',
                                 adapter_id='benchmark', session_id=f'private:{session}')
        if hasattr(self.bot.runtime, 'submit'):
            return self.bot.runtime.submit(envelope)
        # Historical baseline exposes only async admission. The caller awaits
        # this coroutine before counting acceptance; no replacement dispatcher.
        return self.bot.runtime.emit(envelope, reject=False, wait=False)

    async def close(self, expected):
        await self.bot.runtime.scheduler.drain(120)
        assert self.calls == expected * self.matching and not self.unmatched
        assert self.characters == self.calls * len(self.text)
        snapshot = self.bot.metrics.snapshot()
        labels = {'platform': 'onebot', 'adapter': 'benchmark'}
        assert snapshot.get('events_received_total', labels) == expected
        assert snapshot.get('event_duration_seconds_count', labels) == expected
        for i in range(self.matching):
            assert snapshot.get('hook_executions_total', {'platform': 'onebot', 'hook': f'matching_{i}', 'reason': 'completed'}) == expected
        assert self.bot.runtime.scheduler.idle
        assert not self.bot.runtime._owner_events
        if hasattr(self.bot.runtime.scheduler, 'buffered_bytes'):
            assert self.bot.runtime.scheduler.buffered_bytes == 0
        assert (await self.bot.stop()).successful
        if self.http:
            await self.http.close()
        if self.adapter:
            await self.adapter.teardown()


async def normal_worker(args):
    import psutil
    process = psutil.Process()
    rows = []
    for case in args.cases:
        engine = Engine()
        await engine.setup(case)
        for i in range(args.warmup):
            await engine.emit(i, 0)
        await asyncio.sleep(0)
        gc.collect()
        rss = process.memory_info().rss
        latencies = []
        cpu, started = process_time(), perf_counter()
        for i in range(args.events):
            at = perf_counter_ns()
            await engine.emit(i, 0)
            latencies.append((perf_counter_ns() - at) / 1000)
        duration = perf_counter() - started
        rows.append({'case': case, 'events': args.events, 'seconds': duration,
                     'throughput': args.events / duration, 'cpu_seconds': process_time() - cpu,
                     'p95_us': percentile(latencies, .95), 'p99_us': percentile(latencies, .99),
                     'rss_bytes': rss, 'verified_calls': engine.calls})
        await engine.close(args.events + args.warmup)
        del latencies
    return rows


async def http_worker(args):
    engine = Engine()
    await engine.setup('http', args.profile, f'{args.url}?delay={args.delay}', args.sessions)
    engine.delay_ns = int(args.delay * 1e9)
    # Short bursts must not mostly measure cold TCP connection setup. Warm
    # the connections usable by this session/concurrency profile first.
    warmup = max(4, min(args.sessions, 64)) if args.http_warmup is None else args.http_warmup
    await asyncio.gather(*(engine.emit(i, i % args.sessions) for i in range(warmup)))
    engine.peak_active = 0
    latencies = []
    completed, offered, rejected = 0, 0, 0
    scheduler = engine.bot.runtime.scheduler
    started = perf_counter()
    first_arrival = last_arrival = None
    rate = None
    if args.load == 'closed':
        async def producer(session):
            nonlocal completed, offered
            while perf_counter() - started < args.seconds:
                at = perf_counter()
                offered += 1
                await engine.emit(offered, session)
                completed += 1
                latencies.append((perf_counter() - at) * 1000)
        await asyncio.gather(*(producer(i) for i in range(args.sessions)))
    else:
        futures = []
        # Equal offered work and arrival schedule across all profiles.
        count = args.load_events
        rate = max(1., min(args.sessions, 64) / args.delay * 1.5)
        for i in range(count):
            if args.load == 'open':
                target = started + i / rate
                if target > perf_counter():
                    await asyncio.sleep(target - perf_counter())
            at = perf_counter()
            if first_arrival is None:
                first_arrival = at
            last_arrival = at
            offered += 1
            result = engine.admit(i, i % args.sessions)
            if asyncio.iscoroutine(result):
                result = await result
            if result is None:
                rejected += 1
            else:
                futures.append(result)
                result.add_done_callback(lambda future, at=at: latencies.append((perf_counter() - at) * 1000))
        await asyncio.gather(*futures)
        await asyncio.sleep(0)
        completed = len(futures)
    duration = perf_counter() - started
    assert offered == completed + rejected
    await engine.close(completed + warmup)
    return [{'case': f'{args.load}_{args.delay}_{args.sessions}', 'sessions': args.sessions,
             'delay_ms': args.delay * 1000, 'offered': offered, 'completed': completed,
             'rejected': rejected, 'seconds': duration, 'throughput': completed / duration,
             'p95_ms': percentile(latencies, .95), 'p99_ms': percentile(latencies, .99),
             'requested_arrival_rate': rate if args.load == 'open' else None,
             'arrival_window_seconds': None if first_arrival is None else last_arrival - first_arrival,
             'warmup_events': warmup, 'peak_active': engine.peak_active, 'verified_http_calls': engine.calls}]


async def soak(args):
    import psutil
    from core import Bot, Envelope
    process = psutil.Process()
    bot = Bot()
    gate = asyncio.Event()
    calls = 0
    @bot.hook()
    async def work(ctx):
        nonlocal calls
        await gate.wait()
        calls += 1
    result = {'source': fingerprint(args.source), 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cpu_affinity': args.cpu_affinity, 'priority': args.priority,
              'requested_seconds': args.seconds, 'samples': []}
    started = perf_counter()
    async with bot:
        cycle = 0
        while perf_counter() - started < args.seconds:
            burst_started = perf_counter()
            gate.clear()
            sessions = (1, 16, 64)[cycle % 3]
            count = (128, 512, 4096)[cycle % 3]
            futures = [bot.runtime.submit(Envelope('soak', {'i': i}, session_id=str(i % sessions)), reject=True) for i in range(count)]
            await asyncio.sleep(0)
            assert bot.runtime.scheduler.active == sessions
            gate.set()
            await asyncio.wait_for(asyncio.gather(*futures), 30)
            del futures
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gc.collect()
            scheduler = bot.runtime.scheduler
            assert scheduler.idle and scheduler.buffered_bytes == 0
            assert not scheduler._lanes and not scheduler._ready and not scheduler._event_work
            assert not scheduler._runnable_set and not bot.runtime._owner_events
            assert not scheduler._active_sessions and not scheduler._adapter_active and not scheduler._adapter_queued
            assert not bot.runtime.tasks.snapshot()
            tracked = {'futures': 0, 'contexts': 0, 'work': 0}
            for obj in gc.get_objects():
                kind = type(obj).__name__
                if kind == 'Future': tracked['futures'] += 1
                elif kind == 'Context' and type(obj).__module__ == 'core.Context': tracked['contexts'] += 1
                elif kind == '_Work': tracked['work'] += 1
            assert tracked['contexts'] == tracked['work'] == 0
            result['samples'].append({'cycle': cycle, 'elapsed_seconds': perf_counter() - started,
                                      'burst_seconds': perf_counter() - burst_started,
                                      'rss_bytes': process.memory_info().rss, 'calls': calls, **tracked,
                                      'queued': scheduler.queued, 'active': scheduler.active,
                                      'estimated_bytes': scheduler.buffered_bytes,
                                      'lanes': len(scheduler._lanes), 'ready_adapters': len(scheduler._ready),
                                      'tasks': len(bot.runtime.tasks.snapshot()),
                                      'event_tasks': len(scheduler._event_work),
                                      'owners': sum(bot.runtime._owner_events.values())})
            save(args.output, result)
            if cycle % 5 == 0:
                print(f'Soak {perf_counter() - started:.0f}/{args.seconds:.0f}s: cycle={cycle}, budget/owners/tasks=0', flush=True)
            cycle += 1
            await asyncio.sleep(min(10., max(0., args.seconds - (perf_counter() - started))))
    result['seconds'] = perf_counter() - started
    result['passed'] = True
    result['final_source'] = fingerprint(args.source)
    result['source_unchanged'] = result['source']['sha256'] == result['final_source']['sha256']
    save(args.output, result)
    print(f'Soak completed: {result["seconds"]:.1f}s, {calls} events', flush=True)


async def server():
    from aiohttp import web
    pool = ThreadPoolExecutor(max_workers=64)
    barrier = threading.Barrier(65)
    initial = [pool.submit(barrier.wait) for _ in range(64)]
    barrier.wait()
    for future in initial: future.result()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(pool)
    def wait(seconds):
        at = perf_counter_ns()
        while (remaining := seconds - (perf_counter_ns() - at) / 1e9) > 0:
            sleep(remaining)
        return perf_counter_ns() - at
    async def handle(request):
        delay = float(request.query.get('delay', '.005'))
        assert .005 <= delay <= .5
        duration = await asyncio.to_thread(wait, delay)
        return web.Response(text='hello', headers={'X-Service-Delay-Ns': str(duration)})
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        print(json.dumps({'url': f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'}), flush=True)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        pool.shutdown()


def run(args):
    import random
    baseline = args.baseline.resolve()
    manifest = json.loads((baseline / 'baseline-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((baseline / name).read_bytes()).hexdigest() == expected, name
    candidate = args.source.resolve()
    source = fingerprint(candidate)
    result = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'environment': {'python': sys.version, 'os': platform.platform(), 'cpu': platform.processor()},
              'baseline_source': fingerprint(baseline), 'candidate_source': source,
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'method': {'repeats': args.repeats, 'metrics': 'real, immediate', 'trace': False,
                         'gc': True, 'fresh_process_per_profile_round': True, 'order': 'alternating'},
              'samples': [], 'summary': []}
    result['environment']['packages'] = {name: version(name) for name in ('aiohttp', 'psutil', 'websockets')}
    result['method'].update(suite=args.suite, events=args.events, warmup=args.warmup,
                           seconds=args.seconds, loads=args.loads, load_events=args.load_events,
                           cases=args.cases, delays=args.delays, sessions=args.session_counts,
                           http_warmup=args.http_warmup, cpu_affinity=args.cpu_affinity, priority=args.priority)
    first_round = 0
    retained_samples = set()
    if args.resume:
        previous = json.loads(args.resume.read_text(encoding='utf-8'))
        interrupted = not previous.get('finished_at_utc')
        if interrupted:
            assert args.resume_interrupted and args.suite == 'normal', 'interrupted normal runs require --resume-interrupted'
        else:
            assert previous['source_unchanged']
        assert previous['candidate_source'] == source and previous['baseline_source'] == result['baseline_source']
        assert previous['environment'] == result['environment']
        for key in ('suite', 'events', 'warmup', 'seconds', 'loads', 'load_events'):
            assert previous['method'][key] == result['method'][key], f'changed measurement: {key}'
        for key in ('cases', 'delays', 'sessions', 'http_warmup', 'cpu_affinity', 'priority'):
            if key in previous['method']:
                assert previous['method'][key] == result['method'][key], f'changed measurement: {key}'
        assert previous['method'].get('cpu_affinity') == args.cpu_affinity, 'changed CPU affinity'
        assert previous['method'].get('priority') == args.priority, 'changed process priority'
        observed_cases = {row['case'] for sample in previous['samples'] for row in sample['cases']}
        expected_cases = set(args.cases) if args.suite == 'normal' else {
            f'{load}_{delay}_{sessions}' for load in args.loads for delay in args.delays for sessions in args.session_counts}
        assert observed_cases == expected_cases, 'changed case selection'
        if interrupted:
            assert previous['method']['repeats'] == args.repeats, 'changed planned round count'
            for sample in previous['samples']:
                key = (sample['round'], sample['profile'])
                assert type(key[0]) is int and 0 <= key[0] < args.repeats and key[1] in ('before', 'after')
                assert key not in retained_samples, 'duplicate retained sample'
                assert {row['case'] for row in sample['cases']} == expected_cases
                assert len(sample['cases']) == len(expected_cases)
                assert sample['source'] == result['baseline_source' if key[1] == 'before' else 'candidate_source']
                retained_samples.add(key)
            first_round = next((r for r in range(args.repeats)
                                if any((r, p) not in retained_samples for p in ('before', 'after'))), args.repeats)
        else:
            first_round = previous['method']['repeats']
            assert first_round < args.repeats
        result['started_at_utc'] = previous['started_at_utc']
        result['samples'] = previous['samples']
        result['resumed_from'] = {'file': str(args.resume), 'rounds': first_round,
                                  'runner_sha256': previous['runner_sha256'],
                                  'file_sha256': hashlib.sha256(args.resume.read_bytes()).hexdigest(),
                                  'interrupted': interrupted,
                                  'resumed_at_utc': datetime.now(timezone.utc).isoformat()}
    service = None
    if args.suite != 'normal':
        server_command = [sys.executable, '-B', str(Path(__file__).resolve()), '--server']
        if args.cpu_affinity:
            server_command += ['--cpu-affinity', *map(str, args.cpu_affinity)]
        if args.priority:
            server_command += ['--priority', args.priority]
        service = subprocess.Popen(server_command,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        url = json.loads(service.stdout.readline())['url']
    else:
        url = ''
    profiles = [('before', baseline, 'default'), ('after', candidate, 'default')]
    if args.suite != 'normal': profiles.insert(1, ('before_tuned', baseline, 'tuned'))
    try:
        for repeat in range(first_round, args.repeats):
            for label, source_root, profile in (profiles if repeat % 2 == 0 else profiles[::-1]):
                if (repeat, label) in retained_samples:
                    continue
                jobs = [(None, None, None)] if args.suite == 'normal' else [
                    (delay, sessions, load) for load in args.loads for delay in args.delays for sessions in args.session_counts]
                for delay, sessions, load in jobs:
                    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', '--source', str(source_root),
                               '--suite', args.suite, '--events', str(args.events), '--warmup', str(args.warmup),
                               '--profile', profile, '--cases', *args.cases, '--seconds', str(args.seconds)]
                    if args.cpu_affinity:
                        command += ['--cpu-affinity', *map(str, args.cpu_affinity)]
                    if args.priority:
                        command += ['--priority', args.priority]
                    if delay is not None:
                        command += ['--url', url, '--delay', str(delay), '--sessions', str(sessions),
                                    '--load', load, '--load-events', str(args.load_events)]
                        if args.http_warmup is not None:
                            command += ['--http-warmup', str(args.http_warmup)]
                    with tempfile.TemporaryDirectory(prefix='Tiffany elastic ') as temporary:
                        child = subprocess.run(command, cwd=temporary, capture_output=True, text=True, encoding='utf-8',
                                               timeout=600, env=dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1'))
                    if child.returncode:
                        raise RuntimeError(child.stderr[-7000:] + child.stdout[-1000:])
                    sample = json.loads(child.stdout.strip().splitlines()[-1])
                    sample.update(profile=label, round=repeat)
                    result['samples'].append(sample)
                    save(args.output, result)
                    print(f'Round {repeat + 1}/{args.repeats}: {label} {load or "normal"} {delay or ""} {sessions or ""} verified', flush=True)
        for label, _, _ in profiles:
            selected = [row for sample in result['samples'] if sample['profile'] == label for row in sample['cases']]
            for case in dict.fromkeys(row['case'] for row in selected):
                rows = [row for row in selected if row['case'] == case]
                result['summary'].append({'profile': label, 'case': case,
                    **{key: {'median': statistics.median(row[key] for row in rows),
                             'min': min(row[key] for row in rows), 'max': max(row[key] for row in rows)}
                       for key in rows[0] if isinstance(rows[0][key], (int, float))}})
        if args.suite == 'normal':
            for case in args.cases:
                a, b = [next(row for row in result['summary'] if row['case'] == case and row['profile'] == label)
                        for label in ('before', 'after')]
                ratios = {key: b[key]['median'] / a[key]['median'] for key in ('throughput', 'p95_us', 'p99_us', 'rss_bytes')}
                paired = []
                for repeat in range(args.repeats):
                    pair = [next(row for sample in result['samples'] if sample['round'] == repeat and sample['profile'] == label
                                 for row in sample['cases'] if row['case'] == case) for label in ('before', 'after')]
                    paired.append(pair[1]['throughput'] / pair[0]['throughput'])
                rng = random.Random(20261005)
                boot = sorted(statistics.median(rng.choices(paired, k=len(paired))) for _ in range(4000))
                interval = [boot[100], boot[3899]]
                minimum_seconds = min(row['seconds'] for sample in result['samples'] for row in sample['cases'] if row['case'] == case)
                inconclusive = interval[0] < .97 < interval[1]
                result.setdefault('acceptance', []).append({'case': case, 'ratios': ratios,
                    'paired_throughput_ratios': paired, 'paired_median_95_percent_interval': interval,
                    'minimum_case_seconds': minimum_seconds,
                    'passed': not inconclusive and minimum_seconds >= 2 and ratios['throughput'] >= .97 and ratios['p95_us'] <= 1.05 and ratios['p99_us'] <= 1.10 and ratios['rss_bytes'] <= 1.05,
                    'throughput_inconclusive': inconclusive})
        else:
            for after in (row for row in result['summary'] if row['profile'] == 'after'):
                before = next(row for row in result['summary'] if row['profile'] == 'before_tuned' and row['case'] == after['case'])
                ratios = {key: after[key]['median'] / before[key]['median'] for key in ('throughput', 'p95_ms', 'p99_ms')}
                no_rejections = after['rejected']['max'] == before['rejected']['max'] == 0
                same_concurrency = after['peak_active'] == before['peak_active']
                result.setdefault('acceptance', []).append({'case': after['case'], 'ratios': ratios,
                    'no_rejections': no_rejections, 'same_concurrency': same_concurrency,
                    'passed': no_rejections and same_concurrency and ratios['throughput'] >= .95 and ratios['p95_ms'] <= 1.10 and ratios['p99_ms'] <= 1.10})
        result['source_unchanged'] = fingerprint(candidate)['sha256'] == source['sha256']
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        save(args.output, result)
    finally:
        if service:
            service.terminate()
            try: service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
            service.stdout.close()
            service.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path, default=ROOT / '.build-cache/elastic/before')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/results/archive/elastic-development/tiffany-elastic-normal.json')
    parser.add_argument('--suite', choices=('normal', 'http'), default='normal')
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--resume', type=Path, help='Extend a completed run with identical sources and measurement settings.')
    parser.add_argument('--resume-interrupted', action='store_true', help='Retain verified normal samples and complete missing profiles after an interruption.')
    parser.add_argument('--events', type=int, default=60000)
    parser.add_argument('--warmup', type=int, default=1000)
    parser.add_argument('--cases', nargs='+', choices=NORMAL, default=list(NORMAL))
    parser.add_argument('--delays', nargs='+', type=float, default=[.005, .05, .5])
    parser.add_argument('--session-counts', nargs='+', type=int, default=[1, 16, 64, 256])
    parser.add_argument('--loads', nargs='+', choices=('closed', 'open', 'burst'), default=['closed'])
    parser.add_argument('--load-events', type=int, default=64)
    parser.add_argument('--http-warmup', type=int, help='Override automatic connection warmup (4 reproduces initial measurements).')
    parser.add_argument('--cpu-affinity', nargs='+', type=int, help='Pin controller, workers and local HTTP service to these logical CPUs.')
    parser.add_argument('--priority', choices=('above-normal',), help='Set owned measurement processes to Windows above-normal priority.')
    parser.add_argument('--seconds', type=float, default=2.5)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--soak', action='store_true')
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--url', default='')
    parser.add_argument('--delay', type=float, default=.005)
    parser.add_argument('--sessions', type=int, default=64)
    parser.add_argument('--load', default='closed')
    parser.add_argument('--profile', default='default')
    args = parser.parse_args()
    if args.resume_interrupted and not args.resume:
        parser.error('--resume-interrupted requires --resume')
    for name in ('events', 'repeats', 'load_events', 'sessions', 'seconds'):
        if getattr(args, name) <= 0:
            parser.error(f'--{name.replace("_", "-")} must be positive')
    if args.warmup < 0 or any(s < 1 for s in args.session_counts) or any(d < .005 or d > .5 for d in args.delays):
        parser.error('invalid warmup, session count or service delay')
    if args.http_warmup is not None and args.http_warmup < 1:
        parser.error('--http-warmup must be positive')
    logging.basicConfig(level=logging.ERROR)
    if args.cpu_affinity:
        import psutil
        psutil.Process().cpu_affinity(args.cpu_affinity)
    if args.priority:
        if os.name != 'nt':
            parser.error('--priority above-normal is supported on Windows only')
        import psutil
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    if args.server:
        asyncio.run(server())
    elif args.worker or args.soak:
        sys.path.insert(0, str(args.source.resolve()))
        if args.soak:
            asyncio.run(soak(args))
        else:
            rows = asyncio.run(normal_worker(args) if args.suite == 'normal' else http_worker(args))
            print(json.dumps({'cases': rows, 'source': fingerprint(args.source)}))
    else:
        run(args)


if __name__ == '__main__':
    main()
