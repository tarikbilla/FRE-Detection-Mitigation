"""Common types for receive-only IQ sources."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ns_to_datetime(ts_ns: int) -> datetime:
    """UTC datetime with microsecond precision from integer epoch nanoseconds."""
    return _EPOCH + timedelta(microseconds=ts_ns // 1000)


@dataclass
class IQBlock:
    """A contiguous block of complex baseband samples.

    ``ts_ns`` is the UTC epoch time (ns) of the first sample. Sources derive it
    from a wall-clock anchor plus the sample count, so consecutive blocks are
    spaced exactly ``len(samples) / sample_rate_hz`` apart unless samples were
    lost (then ``timestamp_reanchored`` is set).
    """

    samples: np.ndarray
    sample_rate_hz: int
    center_freq_hz: float
    ts_ns: int
    seq: int
    source: str
    dropped_blocks_before: int = 0
    timestamp_reanchored: bool = False
    truth: dict[str, str] | None = None  # simulation ground truth: channel -> label
    meta: dict = field(default_factory=dict)

    @property
    def ts(self) -> datetime:
        return ns_to_datetime(self.ts_ns)

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate_hz)


class IQSource(ABC):
    """Receive-only sample source. Implementations never transmit."""

    name: str = "base"

    @abstractmethod
    def device_info(self) -> dict:
        """Static metadata about the receiver (model, serial, firmware...)."""

    def start(self) -> None:  # pragma: no cover - default no-op
        pass

    def stop(self) -> None:  # pragma: no cover - default no-op
        pass

    @abstractmethod
    def read_block(self, timeout: float = 5.0) -> IQBlock | None:
        """Return the next block, or ``None`` on timeout. Raises ``EOFError`` when exhausted."""

    def stats(self) -> dict:
        return {}

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
