"""Standard-library scheduling contract shared by core and deployment."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    max_events: int = 8192
    buffer_budget_bytes: int = 64 * 1024 * 1024
    max_concurrency: int = 64

    def __post_init__(self):
        for name in ("max_events", "buffer_budget_bytes", "max_concurrency"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"scheduler.{name} must be a positive integer")
