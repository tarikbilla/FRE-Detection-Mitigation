import os
import shutil

import numpy as np
import pytest

from fre_platform.sources.airspyhf import AirspyHFSource, read_device_info
from fre_platform.sources.replay import ReplaySource
from fre_platform.sources.simulated import SCENARIOS, SimulatedSource


def test_simulated_blocks_shape_and_timestamps(cfg):
    src = SimulatedSource(cfg, scenario="normal", seed=1)
    a, b = src.read_block(), src.read_block()
    assert a.samples.dtype == np.complex64 and len(a.samples) == cfg.block_samples
    assert b.seq == a.seq + 1
    # sample-accurate spacing: exactly one block duration apart
    assert b.ts_ns - a.ts_ns == int(cfg.block_samples * 1e9 / cfg.sample_rate_hz)
    assert a.ts.tzinfo is not None


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s not in ("normal", "mixed")])
def test_scenario_labels(cfg, scenario):
    blk = SimulatedSource(cfg, scenario=scenario, seed=2).read_block()
    assert scenario in blk.truth.values()
    assert blk.truth["DCF77"] in ("normal", "jamming_wideband")


def test_simulation_max_blocks(cfg):
    src = SimulatedSource(cfg, scenario="normal", seed=1, max_blocks=2)
    src.read_block(), src.read_block()
    with pytest.raises(EOFError):
        src.read_block()


def test_unknown_scenario(cfg):
    with pytest.raises(ValueError):
        SimulatedSource(cfg, scenario="transmit")


def test_replay_roundtrip(cfg, tmp_path):
    x = (np.arange(cfg.block_samples * 2) % 7).astype(np.complex64)
    f = tmp_path / "rec.cf32"
    x.tofile(f)
    src = ReplaySource(cfg, path=f)
    b1, b2 = src.read_block(), src.read_block()
    assert np.array_equal(np.concatenate([b1.samples, b2.samples]), x)
    with pytest.raises(EOFError):
        src.read_block()


def test_airspy_command_is_receive_only(cfg):
    src = AirspyHFSource(cfg.__class__(**{**cfg.__dict__, "source": "airspyhf", "airspy_serial": "0xABC"}))
    cmd = src.command()
    assert cmd[0] == "airspyhf_rx" and "-r" in cmd and cmd[cmd.index("-r") + 1] == "stdout"
    assert cmd[cmd.index("-f") + 1] == "0.108000" and cmd[cmd.index("-s") + 1] == "0xABC"


@pytest.mark.hardware
@pytest.mark.skipif(os.environ.get("FRE_TEST_HARDWARE") != "1" or not shutil.which("airspyhf_rx"),
                    reason="set FRE_TEST_HARDWARE=1 with an Airspy HF+ attached")
def test_airspy_hardware_capture(cfg):
    info = read_device_info()
    assert "serial" in info, info
    hw = cfg.__class__(**{**cfg.__dict__, "source": "airspyhf"})
    src = AirspyHFSource(hw)
    src.start()
    try:
        blocks = [src.read_block(timeout=10) for _ in range(3)]
    finally:
        src.stop()
    assert all(b is not None and len(b.samples) == hw.block_samples for b in blocks)
    assert all(np.isfinite(b.samples).all() for b in blocks)
    assert blocks[1].ts_ns > blocks[0].ts_ns
