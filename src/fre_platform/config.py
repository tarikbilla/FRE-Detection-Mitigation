"""Configuration management.

All settings come from environment variables (prefix ``FRE_``). For local
development they may also be read from a ``.env`` file; in production the
systemd unit loads ``/etc/fre-platform/fre.env``. Secrets (the MongoDB URI)
are never logged: use :func:`redact_uri` / :meth:`Settings.safe_dict`.
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Mapping

AIRSPY_HF_SAMPLE_RATES = (912000, 768000, 650000, 456000, 384000, 228000, 192000)
VALID_SOURCES = ("airspyhf", "simulated", "replay")
DEFAULT_CHANNELS = "DCF77:77500,DCF49:129100,HGA22:135600,DCF39:139000"

_URI_CRED_RE = re.compile(r"(mongodb(?:\+srv)?://)([^:/@\s]+):([^@\s]+)@")


class ConfigError(ValueError):
    """Raised for missing or invalid configuration."""


def redact_uri(uri: str | None) -> str:
    """Return ``uri`` with any password replaced by ``****``."""
    if not uri:
        return "<unset>"
    return _URI_CRED_RE.sub(r"\1\2:****@", uri)


def redact_text(text: str) -> str:
    """Redact every MongoDB connection-string password inside ``text``."""
    return _URI_CRED_RE.sub(r"\1\2:****@", text)


@dataclass(frozen=True)
class Channel:
    name: str
    freq_hz: float


def parse_channels(spec: str) -> tuple[Channel, ...]:
    chans: list[Channel] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            name, freq = part.split(":")
            chans.append(Channel(name.strip(), float(freq)))
        except ValueError as exc:
            raise ConfigError(f"invalid channel spec {part!r}; expected NAME:FREQ_HZ") from exc
    if not chans:
        raise ConfigError("FRE_CHANNELS must list at least one channel")
    names = [c.name for c in chans]
    if len(set(names)) != len(names):
        raise ConfigError("FRE_CHANNELS contains duplicate channel names")
    return tuple(chans)


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"invalid boolean {value!r}")


# Fields whose type cannot be inferred from the default value.
_SPECIAL_PARSERS = {
    "channels": parse_channels,
    "data_dir": Path,
    "log_dir": Path,
    "model_path": Path,
    "sim_seed": int,
    "mongodb_uri": str,
    "replay_path": str,
    "airspy_serial": str,
}


@dataclass(frozen=True)
class Settings:
    # --- MongoDB (secret URI is excluded from repr) ---
    mongodb_uri: str | None = field(default=None, repr=False)
    mongodb_db: str = "fre_research"
    mongodb_app_name: str = "fre-platform"
    mongodb_timeout_ms: int = 8000

    # --- station / dataset metadata ---
    station_id: str = field(default_factory=socket.gethostname)
    dataset_version: str = "2026.11-v1"
    experiment_id: str = "baseline-001"
    experiment_description: str = "Passive long-wave observation (receive-only)"
    antenna: str = "unspecified"
    location_label: str = "unspecified"

    # --- acquisition ---
    source: str = "airspyhf"
    replay_path: str | None = None
    sim_scenario: str = "normal"
    sim_seed: int | None = None
    center_freq_hz: float = 108000.0
    sample_rate_hz: int = 192000
    block_seconds: float = 1.0
    channels: tuple[Channel, ...] = field(default_factory=lambda: parse_channels(DEFAULT_CHANNELS))
    channel_cutoff_hz: float = 1200.0
    baseband_rate_hz: float = 4000.0
    receiver_model: str = "Airspy HF+ Discovery"
    airspy_binary: str = "airspyhf_rx"
    airspy_info_binary: str = "airspyhf_info"
    airspy_serial: str | None = None
    airspy_agc: bool = True
    airspy_attenuation: int = 0
    airspy_lna: bool = False

    # --- storage / upload ---
    data_dir: Path = Path("data")
    log_dir: Path | None = None
    model_path: Path = Path("models/fre_models.joblib")
    batch_size: int = 200
    upload_interval_s: float = 15.0
    max_backoff_s: float = 300.0
    max_buffer_rows: int = 200_000
    raw_every_n_blocks: int = 10
    measurement_every_n_blocks: int = 5
    psd_bins: int = 256
    save_iq_every_n: int = 0

    # --- detection thresholds ---
    baseline_window: int = 300
    baseline_min_blocks: int = 30
    freq_tol_hz: float = 20.0
    snr_min_db: float = 10.0
    jam_floor_rise_db: float = 10.0
    zscore_threshold: float = 6.0

    # --- logging / ops ---
    log_level: str = "INFO"
    log_format: str = "json"
    heartbeat_s: float = 60.0

    # ------------------------------------------------------------------
    def validate(self) -> "Settings":
        if self.source not in VALID_SOURCES:
            raise ConfigError(f"FRE_SOURCE must be one of {VALID_SOURCES}, got {self.source!r}")
        if self.source == "airspyhf" and self.sample_rate_hz not in AIRSPY_HF_SAMPLE_RATES:
            raise ConfigError(f"FRE_SAMPLE_RATE_HZ {self.sample_rate_hz} not supported by Airspy HF+ "
                              f"(valid: {AIRSPY_HF_SAMPLE_RATES})")
        if self.source == "replay" and not self.replay_path:
            raise ConfigError("FRE_REPLAY_PATH is required when FRE_SOURCE=replay")
        # < 0.5 s blocks slice the DCF77 1 s marker cycle and FSK bursts into
        # step transients that the detectors would misread as interference.
        if not 0.5 <= self.block_seconds <= 10:
            raise ConfigError("FRE_BLOCK_SECONDS must be between 0.5 and 10")
        if not 9_000 <= self.center_freq_hz <= 31_000_000:
            raise ConfigError("FRE_CENTER_FREQ_HZ must be within the 9 kHz - 31 MHz HF range")
        half_bw = 0.45 * self.sample_rate_hz
        for ch in self.channels:
            if abs(ch.freq_hz - self.center_freq_hz) + self.channel_cutoff_hz > half_bw:
                raise ConfigError(f"channel {ch.name} ({ch.freq_hz:.0f} Hz) is outside the usable "
                                  f"bandwidth around {self.center_freq_hz:.0f} Hz")
        if self.sample_rate_hz % int(self.baseband_rate_hz):
            raise ConfigError("FRE_SAMPLE_RATE_HZ must be an integer multiple of FRE_BASEBAND_RATE_HZ")
        if self.channel_cutoff_hz >= self.baseband_rate_hz / 2:
            raise ConfigError("FRE_CHANNEL_CUTOFF_HZ must be below the baseband Nyquist frequency")
        for name in ("batch_size", "max_buffer_rows", "raw_every_n_blocks",
                     "measurement_every_n_blocks", "psd_bins", "baseline_window", "baseline_min_blocks"):
            if getattr(self, name) < 1:
                raise ConfigError(f"FRE_{name.upper()} must be >= 1")
        if self.save_iq_every_n < 0:
            raise ConfigError("FRE_SAVE_IQ_EVERY_N must be >= 0")
        if not 0 <= self.airspy_attenuation <= 8:
            raise ConfigError("FRE_AIRSPY_ATTENUATION must be 0..8")
        if self.log_format not in ("json", "text"):
            raise ConfigError("FRE_LOG_FORMAT must be 'json' or 'text'")
        if self.mongodb_uri and not self.mongodb_uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ConfigError("FRE_MONGODB_URI must start with mongodb:// or mongodb+srv://")
        return self

    def safe_dict(self) -> dict:
        """Settings as a dict that is safe to log (URI password redacted)."""
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "mongodb_uri":
                v = redact_uri(v)
            elif f.name == "channels":
                v = [f"{c.name}:{c.freq_hz:g}" for c in v]
            elif isinstance(v, Path):
                v = str(v)
            out[f.name] = v
        return out

    @property
    def block_samples(self) -> int:
        return int(round(self.sample_rate_hz * self.block_seconds))

    @property
    def mongodb_configured(self) -> bool:
        return bool(self.mongodb_uri)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, env_file: str | os.PathLike | None = None,
                 **overrides) -> "Settings":
        """Build settings from ``env`` (default ``os.environ``) plus optional dotenv file.

        Variables already set in the process environment win over the dotenv
        file; keyword ``overrides`` (field names) win over everything.
        """
        if env is None:
            env_file = env_file or os.environ.get("FRE_ENV_FILE") or (".env" if Path(".env").is_file() else None)
            if env_file:
                path = Path(env_file)
                if not path.is_file():
                    raise ConfigError(f"env file {path} not found")
                try:
                    from dotenv import load_dotenv
                except ImportError as exc:  # pragma: no cover
                    raise ConfigError("python-dotenv is required to read env files") from exc
                load_dotenv(path, override=False)
            env = os.environ

        kw: dict = {}
        for f in fields(cls):
            raw = env.get(f"FRE_{f.name.upper()}")
            if raw is None or raw.strip() == "":
                continue
            raw = raw.strip()
            try:
                if f.name in _SPECIAL_PARSERS:
                    kw[f.name] = _SPECIAL_PARSERS[f.name](raw)
                    continue
                default = f.default if f.default is not MISSING else f.default_factory()  # type: ignore[misc]
                if isinstance(default, bool):
                    kw[f.name] = _bool(raw)
                elif isinstance(default, int):
                    kw[f.name] = int(raw)
                elif isinstance(default, float):
                    kw[f.name] = float(raw)
                else:
                    kw[f.name] = raw
            except ConfigError:
                raise
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"invalid value for FRE_{f.name.upper()}: {exc}") from None
        kw.update(overrides)
        return cls(**kw).validate()
