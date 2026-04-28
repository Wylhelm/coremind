"""System statistics collection using psutil.

Each function is a blocking, side-effect-free observation of the host state.
Return values are JSON-serialisable scalars suitable for ``WorldEvent`` payloads.
"""

from __future__ import annotations

import time

import psutil  # type: ignore[import-untyped]  # psutil has no py.typed marker


def collect_cpu_percent(interval: float = 1.0) -> float:
    """Return the current CPU usage as a percentage.

    Blocks for *interval* seconds to compute a meaningful sample over time.

    Args:
        interval: Measurement window in seconds.  Use a smaller value in tests.

    Returns:
        CPU usage as a float in the range [0.0, 100.0].
    """
    return float(psutil.cpu_percent(interval=interval))


def collect_memory_percent() -> float:
    """Return the current virtual memory usage as a percentage.

    Returns:
        Memory usage as a float in the range [0.0, 100.0].
    """
    return float(psutil.virtual_memory().percent)


def collect_uptime_seconds() -> int:
    """Return the system uptime in whole seconds since last boot.

    Returns:
        Uptime as a non-negative integer.
    """
    return int(time.time() - psutil.boot_time())
