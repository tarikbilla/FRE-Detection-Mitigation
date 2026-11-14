"""Airspy HF+ receive-only source.

Runs the vendor ``airspyhf_rx`` tool with ``-r stdout`` and reads interleaved
32-bit float I/Q (``complex64``). The Airspy HF+ is a receiver only; this
module never sends anything to the radio other than tuning/gain settings.

A reader thread keeps draining the pipe so the device never stalls; if the
processing thread falls behind, the oldest queued block is dropped and
counted (``dropped_blocks``) instead of blocking the USB stream. If the
process dies (USB unplug, device busy...), it is restarted with exponential
backoff.
"""
from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import threading
import time

import numpy as np

from ..config import Settings
from .base import IQBlock, IQSource

log = logging.getLogger(__name__)

# If the sample-count clock and the wall clock disagree by more than this,
# samples were lost inside the pipe/USB stack: re-anchor the timestamps.
REANCHOR_THRESHOLD_S = 0.5


def read_device_info(binary: str = "airspyhf_info", timeout: float = 10.0,
                     model: str = "Airspy HF+ Discovery") -> dict:
    """Parse ``airspyhf_info`` output. Must be called while the device is idle.

    The whole Airspy HF+ family (HF+, HF+ Dual Port, HF+ Discovery) uses the
    same libairspyhf interface; ``model`` records which one is attached.
    """
    info: dict = {"model": model, "family": "Airspy HF+", "driver": "libairspyhf/airspyhf_rx",
                  "receive_only": True}
    exe = shutil.which(binary)
    if not exe:
        info["error"] = f"{binary} not found"
        return info
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        info["error"] = str(exc)
        return info
    patterns = {
        "library_version": r"library version:\s*(\S+)",
        "serial": r"S/N:\s*(\S+)",
        "part_id": r"Part ID:\s*(\S+)",
        "firmware": r"Firmware Version:\s*(.+)",
        "sample_rates": r"Available sample rates:\s*(.+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, out)
        if m:
            info[key] = m.group(1).strip()
    if "serial" not in info:
        info["error"] = "no Airspy HF+ device detected"
    return info


class AirspyHFSource(IQSource):
    name = "airspyhf"

    def __init__(self, cfg: Settings, queue_blocks: int = 8):
        self.cfg = cfg
        self.block_samples = cfg.block_samples
        self.block_bytes = self.block_samples * 8  # complex64
        self._q: queue.Queue[IQBlock] = queue.Queue(maxsize=queue_blocks)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._info: dict | None = None
        self.dropped_blocks = 0
        self.restarts = 0
        self.reanchors = 0
        self._pending_drops = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def device_info(self) -> dict:
        if self._info is None:
            self._info = read_device_info(self.cfg.airspy_info_binary, model=self.cfg.receiver_model)
        return dict(self._info)

    def command(self) -> list[str]:
        c = self.cfg
        cmd = [c.airspy_binary, "-f", f"{c.center_freq_hz / 1e6:.6f}", "-a", str(c.sample_rate_hz), "-r", "stdout"]
        if c.airspy_serial:
            cmd += ["-s", c.airspy_serial]
        cmd += ["-g", "on" if c.airspy_agc else "off", "-t", str(c.airspy_attenuation),
                "-m", "on" if c.airspy_lna else "off"]
        return cmd

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if shutil.which(self.cfg.airspy_binary) is None:
            raise RuntimeError(f"{self.cfg.airspy_binary} not found in PATH")
        self.device_info()  # query before streaming; the device is exclusive once open
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="airspyhf-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=10)

    def read_block(self, timeout: float = 5.0) -> IQBlock | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stats(self) -> dict:
        return {"dropped_blocks": self.dropped_blocks, "restarts": self.restarts, "reanchors": self.reanchors,
                "queue_depth": self._q.qsize()}

    # ------------------------------------------------------------------
    def _kill(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _read_exact(self, stream, buf: bytearray) -> bool:
        view = memoryview(buf)
        pos = 0
        while pos < len(buf):
            n = stream.readinto(view[pos:])
            if not n:
                return False
            pos += n
        return True

    def _enqueue(self, block: IQBlock) -> None:
        try:
            self._q.put_nowait(block)
        except queue.Full:
            try:
                self._q.get_nowait()
                with self._lock:
                    self.dropped_blocks += 1
                    self._pending_drops += 1
            except queue.Empty:
                pass
            self._q.put_nowait(block)

    def _stderr_pump(self, proc: subprocess.Popen) -> None:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode(errors="replace").strip()
            if text:
                log.debug("airspyhf_rx: %s", text)

    def _run(self) -> None:
        backoff = 1.0
        buf = bytearray(self.block_bytes)
        block_dur_ns = int(self.block_samples * 1e9 / self.cfg.sample_rate_hz)
        while not self._stop.is_set():
            started = time.monotonic()
            cmd = self.command()
            log.info("starting receiver", extra={"cmd": " ".join(cmd)})
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            except OSError as exc:
                log.error("cannot start %s: %s", cmd[0], exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            threading.Thread(target=self._stderr_pump, args=(self._proc,), daemon=True).start()

            anchor_ns: int | None = None
            samples_since_anchor = 0
            while not self._stop.is_set():
                if not self._read_exact(self._proc.stdout, buf):
                    break
                now_ns = time.time_ns()
                reanchored = False
                if anchor_ns is None:
                    anchor_ns = now_ns - block_dur_ns  # first sample of this block
                    samples_since_anchor = 0
                ts_ns = anchor_ns + int(samples_since_anchor * 1e9 / self.cfg.sample_rate_hz)
                expected_end = ts_ns + block_dur_ns
                if abs(now_ns - expected_end) > REANCHOR_THRESHOLD_S * 1e9 and samples_since_anchor > 0:
                    anchor_ns = now_ns - block_dur_ns
                    samples_since_anchor = 0
                    ts_ns = anchor_ns
                    reanchored = True
                    self.reanchors += 1
                samples_since_anchor += self.block_samples
                with self._lock:
                    drops, self._pending_drops = self._pending_drops, 0
                samples = np.frombuffer(bytes(buf), dtype="<c8").copy()
                self._enqueue(IQBlock(samples=samples, sample_rate_hz=self.cfg.sample_rate_hz,
                                      center_freq_hz=self.cfg.center_freq_hz, ts_ns=ts_ns, seq=self._seq,
                                      source=self.name, dropped_blocks_before=drops,
                                      timestamp_reanchored=reanchored,
                                      meta={"timestamp_method": "wallclock_anchor+sample_count"}))
                self._seq += 1

            rc = self._proc.poll()
            self._kill()
            if self._stop.is_set():
                break
            self.restarts += 1
            if time.monotonic() - started > 60:
                backoff = 1.0
            log.warning("receiver stream ended; restarting", extra={"returncode": rc, "backoff_s": backoff,
                                                                   "restarts": self.restarts})
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)
