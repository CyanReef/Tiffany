"""Four representative frameworks, broad workloads, sequential isolated workers.

Run from the repository: python -m benchmarks.representative.run
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from time import perf_counter

from benchmarks.compare import positive_integer
from benchmarks.compare_expanded import ROOT, fingerprint, save
from benchmarks.representative.cases import FRAMEWORKS, case_order
from benchmarks.representative.sources import tiffany_sources

HELPERS = ["benchmarks/compare.py", "benchmarks/compare_expanded.py", "benchmarks/expanded_engines.py",
           *[f"benchmarks/representative/{name}" for name in
             ("__init__.py", "cases.py", "engines.py", "worker.py", "koishi.cjs", "run.py", "server.py", "sources.py")]]


def child_sample(command, cwd, env, framework, repeat):
    import psutil
    started = perf_counter()
    child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8")
    stream, errors = queue.Queue(), []

    def read_stdout():
        for line in child.stdout:
            stream.put(line)
        stream.put(None)

    def read_stderr():
        for line in child.stderr:
            errors.append(line)

    threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in threads:
        thread.start()
    sample = {"framework": framework, "round": repeat, "cases": []}
    peak, process = 0, psutil.Process(child.pid)
    active_case = None
    try:
        while perf_counter() - started < 1200:
            try:
                peak = max(peak, process.memory_info().rss)
            except psutil.NoSuchProcess:
                pass
            try:
                line = stream.get(timeout=.01)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # AstrBot/NoneBot initialization may write informational banners.
                continue
            phase = record.get("phase")
            if phase == "case_start":
                active_case, peak = record["case"], 0
            elif phase == "ready":
                sample["component_ready_seconds"] = perf_counter() - started
            elif phase == "case_result":
                row = record["result"]
                assert row["name"] == active_case
                row["sampled_peak_rss_bytes"] = max(peak, row["rss_after_bytes"], row.get("rss_before_bytes", 0))
                sample["cases"].append(row)
            elif phase == "complete":
                sample.update({key: value for key, value in record.items() if key != "phase"})
        else:
            raise TimeoutError(f"{framework} worker exceeded 20 minutes")
        code = child.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=2)
        if code or "runtime" not in sample:
            raise RuntimeError(f"{framework} exit={code}, last case={active_case}\n{''.join(errors)[-9000:]}")
        return sample
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


def summarize(samples):
    result = []
    excluded = {"matching", "unmatched", "predicates", "text_bytes", "segments", "reads", "sessions", "burst"}
    for framework in FRAMEWORKS:
        selected = [sample for sample in samples if sample["framework"] == framework]
        for name in dict.fromkeys(case["name"] for sample in selected for case in sample["cases"]):
            cases = [case for sample in selected for case in sample["cases"] if case["name"] == name]
            row = {"framework": framework, "case": name, "samples": len(cases)}
            for key in cases[0]:
                if key in excluded or not isinstance(cases[0][key], (int, float)) or isinstance(cases[0][key], bool):
                    continue
                values = [case[key] for case in cases]
                row[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
            result.append(row)
    return result


def run(args):
    source_hash = fingerprint(tiffany_sources(ROOT), ROOT)
    helper_hash = fingerprint([ROOT / name for name in HELPERS], ROOT)
    snapshot = ROOT / ".build-cache" / f"expanded-source-{source_hash[:16]}"
    if not snapshot.exists():
        for path in tiffany_sources(ROOT):
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    assert fingerprint(tiffany_sources(snapshot), snapshot) == source_hash
    locks = ("benchmarks/requirements-expanded.lock", "benchmarks/koishi/package-lock.json")
    result = {"schema_version": 3, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                              "tiffany_source_sha256": source_hash, "benchmark_sha256": helper_hash,
                              "source_files": [p.relative_to(ROOT).as_posix() for p in tiffany_sources(ROOT)],
                              "dependency_locks_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in locks}},
              "method": {"frameworks": list(FRAMEWORKS), "repeats": args.repeats, "events_per_case": args.events,
                         "warmup_per_case": args.warmup, "gc_enabled": True, "logging": "ERROR", "llm": False,
                         "platform_network": False, "rss_sampling_interval_ms": 10,
                         "io": "shared localhost HTTP, >=5ms perf_counter deadline via high-resolution time.sleep in 64 prestarted threads; pooled clients limit 64",
                         "io_sessions": [1, 4, 16, 64], "tiffany_io_limits": "global and adapter set to session count",
                         "burst": "unmodified framework defaults; blocked business handlers, 100ms stable observation then release",
                         "timing": "prebuilt equal text/chunks, fresh native protocol containers and native parsing/dispatch through handler completion; no JSON decode/network login/send",
                         "latency": "closed-loop one serial producer/session, no external arrival queue",
                         "cold_start": "process launch to offline message component ready, excludes platform login/full application",
                         "case_order": "fresh process/framework/round, baseline first, rotate others, bursts last",
                         "protocols": {"tiffany": "OneBot v11", "nonebot": "OneBot v11", "astrbot": "OneBot v11", "koishi": "Satori/MockBot"}},
              "samples": [], "summary": []}
    if args.resume:
        old = json.loads(args.output.read_text(encoding="utf-8"))
        assert all(old["environment"][key] == value for key, value in result["environment"].items()), "resume environment changed"
        assert old["method"] == result["method"], "resume methodology changed"
        result = old
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    server = subprocess.Popen([str(args.server_python), "-B", str(ROOT / "benchmarks/representative/server.py")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    first_line = queue.Queue()
    threading.Thread(target=lambda: first_line.put(server.stdout.readline()), daemon=True).start()
    try:
        line = first_line.get(timeout=20)
        if not line:
            raise RuntimeError(server.stderr.read())
        server_info = json.loads(line)
        url = server_info.pop("url")
        result["environment"]["io_server"] = server_info
        completed = {(sample["round"], sample["framework"]) for sample in result["samples"]}
        for repeat in range(args.repeats):
            offset = repeat % len(FRAMEWORKS)
            for framework in FRAMEWORKS[offset:] + FRAMEWORKS[:offset]:
                if (repeat, framework) in completed:
                    continue
                cases = case_order(repeat)
                if framework == "astrbot":
                    cases.append({"name": "sqlite_defaults", "matching": 1, "sqlite_defaults": True})
                options = {"framework": framework, "round": repeat, "events": args.events, "warmup": args.warmup,
                           "cases": cases, "io_url": url, "source_root": str(snapshot), "node_env": str(args.node_env.resolve())}
                script = ROOT / "benchmarks/representative" / ("koishi.cjs" if framework == "koishi" else "worker.py")
                command = [str(args.node), "--expose-gc", str(script), json.dumps(options)] if framework == "koishi" else [str(args.python), "-B", str(script), json.dumps(options)]
                with tempfile.TemporaryDirectory(prefix="Tiffany representative ") as temporary:
                    sample = child_sample(command, temporary, dict(env, ASTRBOT_ROOT=temporary, ASTRBOT_DISABLE_METRICS="1"), framework, repeat)
                assert len(sample["cases"]) == len(cases)
                if framework != "koishi":
                    assert sample["tiffany_source_sha256"] == source_hash
                result["samples"].append(sample)
                result["summary"] = summarize(result["samples"])
                save(result, args.output)
                baseline = sample["cases"][0]
                print(f"Round {repeat + 1}/{args.repeats}: {framework}, {len(cases)} cases verified; single {baseline['events_per_second']:,.0f} events/s", flush=True)
        assert fingerprint(tiffany_sources(ROOT), ROOT) == source_hash
        assert fingerprint([ROOT / name for name in HELPERS], ROOT) == helper_hash
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(result, args.output)
        print(f"Saved {args.output}", flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server.stdout.close()
        server.stderr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=positive_integer, default=1000)
    parser.add_argument("--warmup", type=positive_integer, default=200)
    parser.add_argument("--repeats", type=positive_integer, default=5)
    parser.add_argument("--python", type=Path, default=ROOT / ".build-cache/expanded-python-env/Scripts/python.exe")
    parser.add_argument("--server-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--node", type=Path, default=Path("C:/Program Files/nodejs/node.exe"))
    parser.add_argument("--node-env", type=Path, default=ROOT / ".build-cache/expanded-node-env")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results/representative/framework-comparison-representative.json")
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
