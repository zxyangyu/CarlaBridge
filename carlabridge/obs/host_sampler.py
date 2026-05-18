"""Host resource sampling for system_metrics (psutil + optional GPUtil).

All values are normalized to 0–100 for the frontend Telemetry panel:
  - cpu / mem: utilization percent
  - gpu: max GPU load across devices (0 if unavailable)
  - net: bandwidth vs a configurable cap (default 100 Mbps combined)
"""

from __future__ import annotations

import logging
import time

import psutil

log = logging.getLogger(__name__)

# Combined send+recv at this rate maps to net=100%.
_DEFAULT_NET_CAP_MBPS = 100.0


class HostSampler:
    """Sample host CPU/MEM/GPU/NET at ~1 Hz (call ``sample()`` once per period)."""

    __slots__ = ("_net_cap_bps", "_last_net_bytes", "_last_sample_time")

    def __init__(self, *, net_cap_mbps: float = _DEFAULT_NET_CAP_MBPS) -> None:
        if net_cap_mbps <= 0:
            raise ValueError("net_cap_mbps must be positive")
        self._net_cap_bps = net_cap_mbps * 1_000_000 / 8
        self._last_net_bytes: int | None = None
        self._last_sample_time: float | None = None
        # Prime cpu_percent so the first non-blocking read is meaningful.
        psutil.cpu_percent(interval=None)

    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        elapsed = (
            now - self._last_sample_time if self._last_sample_time is not None else 1.0
        )
        self._last_sample_time = now

        cpu = float(psutil.cpu_percent(interval=None))
        mem = float(psutil.virtual_memory().percent)
        gpu = self._sample_gpu()
        net = self._sample_net(elapsed)

        return {
            "cpu": round(min(100.0, max(0.0, cpu)), 1),
            "gpu": round(min(100.0, max(0.0, gpu)), 1),
            "mem": round(min(100.0, max(0.0, mem)), 1),
            "net": round(min(100.0, max(0.0, net)), 1),
        }

    def _sample_gpu(self) -> float:
        try:
            import GPUtil  # type: ignore[import-untyped]
        except ImportError:
            return 0.0
        try:
            gpus = GPUtil.getGPUs()
        except Exception:
            log.debug("GPUtil.getGPUs failed", exc_info=True)
            return 0.0
        if not gpus:
            return 0.0
        return max(float(g.load) for g in gpus) * 100.0

    def _sample_net(self, elapsed_s: float) -> float:
        try:
            counters = psutil.net_io_counters()
        except Exception:
            log.debug("net_io_counters failed", exc_info=True)
            return 0.0
        total_bytes = counters.bytes_sent + counters.bytes_recv
        if self._last_net_bytes is None:
            self._last_net_bytes = total_bytes
            return 0.0
        delta = max(0, total_bytes - self._last_net_bytes)
        self._last_net_bytes = total_bytes
        bps = delta / max(elapsed_s, 0.001)
        return (bps / self._net_cap_bps) * 100.0


__all__ = ["HostSampler"]
