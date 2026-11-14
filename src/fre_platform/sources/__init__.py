"""Receive-only IQ sources."""
from __future__ import annotations

from ..config import Settings
from .base import IQBlock, IQSource, ns_to_datetime


def create_source(cfg: Settings, **kwargs) -> IQSource:
    if cfg.source == "airspyhf":
        from .airspyhf import AirspyHFSource
        return AirspyHFSource(cfg)
    if cfg.source == "simulated":
        from .simulated import SimulatedSource
        kwargs.setdefault("realtime", True)
        return SimulatedSource(cfg, **kwargs)
    if cfg.source == "replay":
        from .replay import ReplaySource
        return ReplaySource(cfg, **kwargs)
    raise ValueError(f"unknown source {cfg.source!r}")


__all__ = ["IQBlock", "IQSource", "create_source", "ns_to_datetime"]
