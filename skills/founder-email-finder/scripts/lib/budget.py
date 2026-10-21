"""Spend caps for the paid lanes. A cap of 0 means the lane is off."""
from __future__ import annotations

import threading


class Budget:
    def __init__(self, name: str, limit: int):
        self.name = name
        self.limit = max(0, int(limit or 0))
        self.used = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def take(self, units: int = 1) -> bool:
        """Reserve units. False (and nothing reserved) if it would go over the cap."""
        with self._lock:
            if self.used + units > self.limit:
                return False
            self.used += units
            return True

    def refund(self, units: int) -> None:
        with self._lock:
            self.used = max(0, self.used - max(0, units))

    def __repr__(self) -> str:
        return f"Budget({self.name}: {self.used}/{self.limit})"
