"""Bounded per-identity buckets; expiration does at most eight removals."""
from collections import OrderedDict
from time import monotonic
from core import ServiceKey
from .model import Contribution


COMMANDS = ServiceKey("business.ratelimit.policy")


class RateLimiter:
    def __init__(self, config, *, clock=monotonic):
        self.config = config
        self.clock = clock
        self.buckets = OrderedDict()

    def allow(self, who):
        now = self.clock()
        buckets, config = self.buckets, self.config
        for _ in range(8):
            if not buckets:
                break
            key, (_, touched) = next(iter(buckets.items()))
            if now - touched < config.idle_ttl:
                break
            del buckets[key]
        current = buckets.get(who)
        if current is None:
            if len(buckets) >= config.max_buckets:
                return False
            tokens = float(config.burst)
        else:
            tokens = min(config.burst, current[0] + (now - current[1]) * config.rate)
        allowed = tokens >= 1
        buckets[who] = (tokens - 1 if allowed else tokens, now)
        buckets.move_to_end(who)
        return allowed


def install(scope, config):
    scope.service(COMMANDS, Contribution(limiter=RateLimiter(config)))
    return COMMANDS
