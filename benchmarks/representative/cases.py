"""Workload definitions shared by the coordinator and Python workers."""
from benchmarks.compare import raw_event as baseline_event

FRAMEWORKS = ("tiffany", "nonebot", "astrbot", "koishi")
CASES = (
    {"name": "one_handler", "matching": 1},
    {"name": "ten_handlers", "matching": 10},
    {"name": "fifty_handlers", "matching": 50},
    {"name": "category_100", "matching": 1, "unmatched": 100},
    {"name": "category_1000", "matching": 1, "unmatched": 1000},
    {"name": "predicate_100", "matching": 1, "predicates": 100},
    {"name": "predicate_1000", "matching": 1, "predicates": 1000},
    {"name": "text_4k", "matching": 1, "text_bytes": 4096},
    {"name": "text_64k", "matching": 1, "text_bytes": 65536},
    {"name": "segments_20", "matching": 1, "segments": 20},
    {"name": "segments_200", "matching": 1, "segments": 200},
    {"name": "reads_16", "matching": 1, "reads": 16},
    *({"name": f"io_{n}", "matching": 1, "sessions": n, "http": True}
      for n in (1, 4, 16, 64)),
)
BURSTS = (
    {"name": "burst_single", "matching": 1, "sessions": 1, "burst": 128},
    {"name": "burst_multi", "matching": 1, "sessions": 16, "burst": 512},
)


def parts(case):
    if "text_bytes" in case:
        return ["hello" + "x" * (case["text_bytes"] - 5)]
    return ["hello"] * case.get("segments", 1)


def payload(case, index, session, *, pieces=None, text=None):
    raw = baseline_event(index, session)
    pieces = parts(case) if pieces is None else pieces
    raw["raw_message"] = "".join(pieces) if text is None else text
    raw["message"] = [{"type": "text", "data": {"text": part}} for part in pieces]
    return raw


def case_order(repeat):
    rest = list(CASES[1:])
    offset = repeat % len(rest)
    return [CASES[0], *(rest[offset:] + rest[:offset]), *BURSTS]
