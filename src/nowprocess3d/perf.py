"""每步性能记录：墙钟时间、CPU 时间、内存。"""
from __future__ import annotations

import os
import resource
import threading
import time
from contextlib import contextmanager

try:
    import psutil
    _proc = psutil.Process()
except ImportError:  # pragma: no cover
    psutil = None


def rss_mb() -> float:
    if psutil:
        return _proc.memory_info().rss / 2**20
    return float("nan")


def peak_rss_mb() -> float:
    # Linux 上 ru_maxrss 单位为 KiB；这是进程开始以来的峰值
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class _Sampler(threading.Thread):
    """后台每 interval 秒读一次 RSS，记录本阶段内的最大值。"""

    def __init__(self, interval=0.02):
        super().__init__(daemon=True)
        self.interval, self.peak, self._halt = interval, rss_mb(), threading.Event()

    def run(self):
        while not self._halt.wait(self.interval):
            self.peak = max(self.peak, rss_mb())

    def stop(self):
        self._halt.set()
        self.join()
        self.peak = max(self.peak, rss_mb())
        return self.peak


@contextmanager
def measure(record: dict, key: str):
    """在 record[key] 中写入：
    wall_s / cpu_s：墙钟与 CPU 时间（CPU 含所有线程）
    rss_before_mb / rss_after_mb：阶段前后常驻内存
    phase_peak_mb：本阶段内采样到的峰值（20 ms 采样，极短的尖峰可能漏掉）
    peak_rss_mb：进程启动以来的峰值（只增不减）
    """
    rss0 = rss_mb()
    sampler = _Sampler()
    sampler.start()
    t0, c0 = time.perf_counter(), time.process_time()
    try:
        yield
    finally:
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        record[key] = {
            "wall_s": round(wall, 4),
            "cpu_s": round(cpu, 4),
            "rss_before_mb": round(rss0, 1),
            "rss_after_mb": round(rss_mb(), 1),
            "phase_peak_mb": round(sampler.stop(), 1),
            "peak_rss_mb": round(peak_rss_mb(), 1),
        }


def host_info() -> dict:
    return {
        "hostname": os.uname().nodename,
        "cpu_count": os.cpu_count(),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "nslots": os.environ.get("NSLOTS"),
        "job_id": os.environ.get("JOB_ID"),
    }
