"""Spending-limit gate for concurrent paid calls.

Checking the limit only after a call finishes lets every call already in flight land on top of it (4 workers x ~$0.30 = about
$1 over). `Reserve.allows` also counts what the in-flight calls are expected to cost, so a run ends at or just under the limit."""

from __future__ import annotations

DEFAULT_RESERVE_USD = 0.35


class Reserve:
    def __init__(self, default: float = DEFAULT_RESERVE_USD) -> None:
        self.default = default
        self._costs: list[float] = []

    def note(self, cost: float | None, searched: bool = True) -> None:
        """Record a finished call. Calls that made no web search (SEC filing lookups) say nothing about what one costs."""
        if cost is not None and searched:
            self._costs.append(cost)

    @property
    def per_call(self) -> float:
        return sum(self._costs) / len(self._costs) if self._costs else self.default

    def allows(self, spent: float, inflight: int, cap: float | None) -> bool:
        """May one more call start? Never when the limit is already spent; always for the very first call (a tiny limit must still
        let one call through); otherwise only if the limit would hold after every running call finished."""
        if cap is None:
            return True
        if spent >= cap:
            return False
        if inflight == 0 and spent == 0:
            return True
        return spent + (inflight + 1) * self.per_call <= cap
