"""Shared conditions for charts and their matching result tables."""

FIGURE_COUNT = 6
IO_SESSIONS = (1, 4, 16, 64)
PEAK_MEMORY_CASES = (
    ("one_handler", "单处理器基线"),
    ("predicate_1000", "1,000 个恒假规则"),
    ("segments_200", "200 段文本"),
    ("io_64", "64 会话 I/O"),
    ("burst_multi", "512 条突发"),
)
STORAGE_CASES = (
    ("one_handler", "配置写入缓存"),
    ("sqlite_defaults", "SQLite 默认配置缺失"),
)
