"""Independently check completed measurements before publication."""
import argparse
import json
import math
from pathlib import Path

from benchmarks.representative.cases import BURSTS, CASES, FRAMEWORKS, parts

ROOT = Path(__file__).resolve().parents[2]


def validate(data):
    assert data.get("finished_at_utc"), "measurement is unfinished"
    method = data["method"]
    expected_workers = {(repeat, framework) for repeat in range(method["repeats"]) for framework in FRAMEWORKS}
    actual_workers = [(sample["round"], sample["framework"]) for sample in data["samples"]]
    assert len(actual_workers) == len(expected_workers) and set(actual_workers) == expected_workers
    python_samples = [sample for sample in data["samples"] if sample["framework"] != "koishi"]
    assert len({sample["runtime"] for sample in python_samples}) == 1, "Python runtimes differ"
    assert len({json.dumps(sample["packages"], sort_keys=True) for sample in python_samples}) == 1, "Python packages differ"
    for framework in FRAMEWORKS:
        selected = [sample for sample in data["samples"] if sample["framework"] == framework]
        assert len({sample["runtime"] for sample in selected}) == 1
        assert len({json.dumps(sample["packages"], sort_keys=True) for sample in selected}) == 1
    configurations = {case["name"]: case for case in (*CASES, *BURSTS)}
    totals = {"timed_events": 0, "handler_calls": 0, "predicate_calls": 0, "http_calls": 0,
              "burst_submitted": 0, "burst_completed": 0, "burst_rejected": 0}
    summary_keys = set()
    for sample in data["samples"]:
        framework = sample["framework"]
        expected = dict(configurations)
        if framework == "astrbot":
            expected["sqlite_defaults"] = {"name": "sqlite_defaults", "matching": 1, "sqlite_defaults": True}
        names = [case["name"] for case in sample["cases"]]
        assert len(names) == len(expected) and set(names) == set(expected)
        assert names[0] == "one_handler" and sample["component_ready_seconds"] > 0
        for case in sample["cases"]:
            configuration = expected[case["name"]]
            assert all(case[key] == value for key, value in configuration.items())
            summary_keys.add((framework, case["name"]))
            if case.get("burst"):
                events = case["completed"]
                assert case["submitted"] == case["completed"] + case["rejected"] == configuration["burst"]
                assert 0 < case["blocked_handlers"] <= case["completed"]
                assert 1 <= case["max_same_session_overlap"] <= case["peak_active_handlers"] <= case["completed"]
                if framework == "tiffany":
                    assert case["native_active"] + case["native_queued"] == case["completed"] <= case["capacity"]
                    assert case["native_active"] == case["blocked_handlers"] <= case["adapter_limit"]
                    if "buffer_budget_bytes" in case:
                        assert case["buffer_budget_bytes"] > 0
                    assert case["max_same_session_overlap"] == 1 and case["observed_completion_fifo"]
                for key in ("submitted", "completed", "rejected"):
                    totals[f"burst_{key}"] += case[key]
            else:
                events = case["events"]
                assert events == method["events_per_case"]
                assert math.isclose(case["events_per_second"], events / case["elapsed_seconds"], rel_tol=1e-12)
                assert 0 <= case["latency_p50_us"] <= case["latency_p95_us"] <= case["latency_p99_us"] <= case["latency_max_us"]
                assert math.isclose(case["cpu_us_per_event"], case["cpu_seconds"] / events * 1e6, rel_tol=1e-12)
                assert all(math.isfinite(case[key]) for key in ("events_per_second", "cpu_seconds", "latency_p99_us"))
                if framework == "tiffany":
                    assert case["verified_metric_events"] == events + method["warmup_per_case"]
                if case.get("http"):
                    assert 5000 <= case["service_delay_min_us"] <= case["service_delay_p50_us"] <= case["service_delay_p95_us"]
                totals["timed_events"] += events
            assert case["verified_handler_calls"] == events * configuration["matching"]
            assert case["verified_characters"] == case["verified_handler_calls"] * len("".join(parts(configuration)))
            assert case["verified_unmatched_calls"] == 0
            assert case["verified_predicate_calls"] == events * configuration.get("predicates", 0)
            assert case["verified_http_calls"] == (events if configuration.get("http") else 0)
            assert case["sampled_peak_rss_bytes"] >= max(case["rss_after_bytes"], case.get("rss_before_bytes", 0)) > 0
            if framework == "astrbot":
                assert len(case["pipeline_stages"]) == 9
            totals["handler_calls"] += case["verified_handler_calls"]
            totals["predicate_calls"] += case["verified_predicate_calls"]
            totals["http_calls"] += case["verified_http_calls"]
        if sample.get("source_modules"):
            assert set(sample["source_modules"].values()) <= set(data["environment"]["source_files"])
    assert {(row["framework"], row["case"]) for row in data["summary"]} == summary_keys
    assert all(row["samples"] == method["repeats"] for row in data["summary"])
    # Compare stored aggregate values with raw measurements, not just shapes.
    from benchmarks.representative.run import summarize
    assert data["summary"] == summarize(data["samples"])
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "benchmarks/results/representative/framework-comparison-representative.json")
    args = parser.parse_args()
    print(json.dumps(validate(json.loads(args.input.read_text(encoding="utf-8"))), indent=2))
