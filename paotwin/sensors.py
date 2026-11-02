"""Real, read-only sensors of the computer the demo runs on.

Each reader returns ``Reading(value)`` or ``Reading(None, reason)``. Nothing
here is ever estimated: when the machine does not tell us a value, the
reading is missing and the twin shows "Mất tín hiệu".

Sources:
- CPU load: Windows GetSystemTimes (Linux: /proc/stat).
- RAM in use: Windows GlobalMemoryStatusEx (Linux: /proc/meminfo).
- Free space on the project's drive: shutil.disk_usage.
- Battery and charger: Windows GetSystemPowerStatus.
- Wi-Fi signal: ``netsh wlan show interfaces``.
- GPU: ``nvidia-smi --query-gpu=...``; its temperature limits from
  ``nvidia-smi -q -d TEMPERATURE`` (what the card itself reports).

CPU temperature and fan speed are not readable on this laptop without an
administrator-level driver, so the twin lists them as "Chưa có cảm biến".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Reading:
    value: object
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def missing(reason: str) -> Reading:
    return Reading(None, reason)


def _run(args: list[str], timeout: float = 5.0) -> str:
    kwargs = {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}
    done = subprocess.run(args, capture_output=True, timeout=timeout, check=False, **kwargs)
    if done.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} trả mã {done.returncode}")
    encoding = "mbcs" if WINDOWS else "utf-8"
    return done.stdout.decode(encoding, errors="replace")


# -- CPU ----------------------------------------------------------------------

class CpuLoad:
    """Percent of CPU time not idle since the previous call."""

    def __init__(self) -> None:
        self._previous: tuple[int, int] | None = None

    def _times(self) -> tuple[int, int]:
        """(idle, total) in arbitrary but consistent units."""
        if WINDOWS:
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                raise OSError("GetSystemTimes thất bại")

            def value(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            # Kernel time includes idle time.
            return value(idle), value(kernel) + value(user)
        with open("/proc/stat", encoding="ascii") as handle:
            fields = [int(x) for x in handle.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return idle, sum(fields[:8])

    def read(self) -> Reading:
        try:
            now = self._times()
        except (OSError, ValueError, IndexError) as exc:
            return missing(f"không đọc được thời gian CPU ({exc})")
        previous, self._previous = self._previous, now
        if previous is None:
            return missing("đang lấy mẫu đầu tiên")
        d_idle, d_total = now[0] - previous[0], now[1] - previous[1]
        if d_total <= 0:
            return missing("chưa đủ thời gian giữa hai mẫu")
        return Reading(round(100.0 * (1.0 - d_idle / d_total), 1))


# -- RAM ----------------------------------------------------------------------

def ram_used_percent() -> Reading:
    try:
        if WINDOWS:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return missing("GlobalMemoryStatusEx thất bại")
            total, free = status.ullTotalPhys, status.ullAvailPhys
        else:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0])
            total, free = info["MemTotal"], info["MemAvailable"]
    except (OSError, KeyError, ValueError) as exc:
        return missing(f"không đọc được bộ nhớ ({exc})")
    if total <= 0:
        return missing("tổng bộ nhớ bằng 0")
    return Reading(round(100.0 * (total - free) / total, 1))


# -- Disk ---------------------------------------------------------------------

def disk_free_percent(path: str) -> Reading:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return missing(f"không đọc được ổ {path} ({exc.__class__.__name__})")
    if usage.total <= 0:
        return missing("dung lượng ổ bằng 0")
    return Reading(round(100.0 * usage.free / usage.total, 1))


# -- Battery ------------------------------------------------------------------

def power_status() -> tuple[Reading, Reading]:
    """(battery percent, charger plugged: "co" / "khong")."""
    if not WINDOWS:
        return missing("chỉ đọc được trên Windows"), missing("chỉ đọc được trên Windows")
    import ctypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]

    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        reason = "GetSystemPowerStatus thất bại"
        return missing(reason), missing(reason)
    if status.BatteryFlag == 128:
        battery = missing("máy không có pin")
    elif status.BatteryLifePercent == 255:
        battery = missing("Windows không biết mức pin")
    else:
        battery = Reading(int(status.BatteryLifePercent))
    charger = {0: Reading("khong"), 1: Reading("co")}.get(
        status.ACLineStatus, missing("Windows không biết trạng thái sạc"))
    return battery, charger


# -- Wi-Fi --------------------------------------------------------------------

_PERCENT_LINE = re.compile(r":\s*(\d{1,3})\s*%\s*$")


def parse_wifi_signal(text: str) -> Reading:
    """The one ``Signal : NN%`` line of ``netsh wlan show interfaces``.

    The label is translated with Windows' language, the ``NN%`` value is not,
    so the line is found by its value. More than one Wi-Fi card: the first.
    """
    for line in text.splitlines():
        match = _PERCENT_LINE.search(line)
        if match:
            value = int(match.group(1))
            if 0 <= value <= 100:
                return Reading(value)
    lowered = text.lower()
    if "location" in lowered or "vị trí" in lowered:
        # Windows 11 24H2 and later hide Wi-Fi details from apps until
        # location access is allowed for desktop apps.
        return missing("Windows chặn đọc Wi-Fi (cần cho phép Vị trí cho ứng dụng máy tính)")
    return missing("không thấy Wi-Fi đang kết nối")


def wifi_signal_percent() -> Reading:
    if not WINDOWS:
        return missing("chỉ đọc được trên Windows")
    try:
        kwargs = {"creationflags": CREATE_NO_WINDOW}
        done = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                              timeout=5, check=False, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return missing(f"netsh không chạy được ({exc.__class__.__name__})")
    # netsh may exit non-zero with the explanation on stdout, so read both.
    text = (done.stdout + b"\n" + done.stderr).decode("mbcs", errors="replace")
    return parse_wifi_signal(text)


# -- GPU ----------------------------------------------------------------------

GPU_FIELDS = ("temperature.gpu", "utilization.gpu", "power.draw", "memory.used", "memory.total")


def find_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if WINDOWS:
        for candidate in (r"C:\Windows\System32\nvidia-smi.exe",
                          r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(candidate):
                return candidate
    return None


def _number(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]", ...


def parse_gpu_query(text: str) -> dict[str, Reading]:
    """One CSV line, no header, no units, in ``GPU_FIELDS`` order (first GPU)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        reason = "nvidia-smi không trả dữ liệu"
        return {name: missing(reason) for name in GPU_FIELDS}
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) != len(GPU_FIELDS):
        reason = "nvidia-smi trả dữ liệu sai định dạng"
        return {name: missing(reason) for name in GPU_FIELDS}
    out = {}
    for name, raw in zip(GPU_FIELDS, parts):
        value = _number(raw)
        out[name] = Reading(value) if value is not None else missing(f"card báo {raw}")
    return out


_LIMIT = re.compile(r"^\s*(GPU Shutdown Temp|GPU Slowdown Temp|GPU Max Operating Temp|"
                    r"GPU Target Temperature)\s*:\s*(\S+)", re.MULTILINE)
_LIMIT_KEYS = {"GPU Shutdown Temp": "shutdown", "GPU Slowdown Temp": "slowdown",
               "GPU Max Operating Temp": "max_operating",
               "GPU Target Temperature": "target"}


def parse_gpu_limits(text: str) -> dict[str, float]:
    """Temperature limits the card reports. Missing ones ("N/A") are left out."""
    limits = {}
    for label, raw in _LIMIT.findall(text):
        value = _number(raw.rstrip("C"))
        if value is not None:
            limits.setdefault(_LIMIT_KEYS[label], value)
    return limits


class Gpu:
    def __init__(self) -> None:
        self.exe = find_nvidia_smi()

    def read(self) -> dict[str, Reading]:
        if self.exe is None:
            return {name: missing("không có nvidia-smi") for name in GPU_FIELDS}
        try:
            text = _run([self.exe, "--query-gpu=" + ",".join(GPU_FIELDS),
                         "--format=csv,noheader,nounits"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {name: missing(f"nvidia-smi lỗi ({exc})") for name in GPU_FIELDS}
        return parse_gpu_query(text)

    def limits(self) -> dict[str, float]:
        if self.exe is None:
            return {}
        try:
            return parse_gpu_limits(_run([self.exe, "-q", "-d", "TEMPERATURE"]))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return {}


def project_drive() -> str:
    """The root of the drive this project sits on (E:\\ on the laptop)."""
    anchor = Path(__file__).resolve().anchor
    return anchor or "/"


class Laptop:
    """Reads every laptop signal once. Values keyed by the twin's signal ids."""

    def __init__(self, drive: str | None = None) -> None:
        self.cpu = CpuLoad()
        self.gpu = Gpu()
        self.drive = drive or project_drive()
        self.cpu.read()  # first sample, so the next call has a difference

    def read(self) -> dict[str, tuple[Reading, float]]:
        """{signal id: (reading, unix time in ms when it was read)}."""
        out: dict[str, tuple[Reading, float]] = {}

        def put(key: str, reading: Reading) -> None:
            out[key] = (reading, time.time() * 1000.0)

        put("cpu_tai", self.cpu.read())
        put("ram_dung", ram_used_percent())
        put("o_dia_trong", disk_free_percent(self.drive))
        battery, charger = power_status()
        put("pin_muc", battery)
        put("cam_sac", charger)
        put("wifi_song", wifi_signal_percent())
        gpu = self.gpu.read()
        put("gpu_nhiet_do", gpu["temperature.gpu"])
        put("gpu_tai", gpu["utilization.gpu"])
        put("gpu_cong_suat", gpu["power.draw"])
        used, total = gpu["memory.used"], gpu["memory.total"]
        if used.ok and total.ok and total.value:
            put("gpu_vram", Reading(round(100.0 * used.value / total.value, 1)))
        else:
            put("gpu_vram", missing(used.reason or total.reason or "không đọc được VRAM"))
        return out
