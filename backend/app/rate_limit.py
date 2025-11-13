import time
from collections import deque
from typing import Deque, Dict


class RateLimiter:
    """
    Very simple in-memory fixed-window limiter:
      - allow N requests per window_seconds per key (IP)
    """
    def __init__(self, limit_per_window: int, window_seconds: int = 60):
        self.limit = int(limit_per_window)
        self.window = int(window_seconds)
        self.store: Dict[str, Deque[float]] = {}

    def hit(self, key: str) -> bool:
        now = time.time()
        q = self.store.setdefault(key, deque())
        # purge old
        while q and (now - q[0]) > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
