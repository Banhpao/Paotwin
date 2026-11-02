"""Starts the whole demo: broker, virtual devices, gateway, twin server.

Each part runs as its own process, as it would on separate machines, so
stopping one (the gateway, say) shows on the twin the way a real failure
would. Their output goes to this console and to ``nhat-ky/``.
Closing this window or pressing Ctrl+C stops every part. On Windows the
parts also sit in a job object that closes with this process, so they stop
even when this launcher is ended from outside (Task Manager, a supervisor).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import common

PARTS = [  # (name, module, port it must open before the next part starts)
    ("broker", "paotwin.mqtt", ("broker", "port")),
    ("mo-phong", "paotwin.sim", ("mo_phong_web", "port")),
    ("ban-sao", "paotwin.server", ("web", "port")),
    ("gateway", "paotwin.gateway", None),
]


class _KillOnClose:
    """Windows job object: every process assigned to it ends when this
    launcher ends, however it ends. Does nothing on other systems."""

    def __init__(self) -> None:
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        self.kernel32, self.handle = kernel32, job

    def add(self, process: subprocess.Popen) -> bool:
        if self.handle is None:
            return False
        return bool(self.kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))


def port_busy(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


def wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_busy(host, port):
            return True
        time.sleep(0.1)
    return False


def pump(name: str, process: subprocess.Popen, log_file) -> None:
    for raw in iter(process.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()
    code = process.wait()
    print(f"*** {name} đã dừng (mã {code})", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chạy toàn bộ demo bản sao số")
    parser.add_argument("--khong-mo-trinh-duyet", action="store_true",
                        help="không tự mở hai trang web")
    args = parser.parse_args(argv)
    net = common.network()

    ports = [("broker", net["broker"]), ("đường mạng ảo", net["duong_mang_ao"]),
             ("Modbus", net["modbus"]), ("trang bản sao", net["web"]),
             ("trang mô phỏng", net["mo_phong_web"])]
    busy = [f"{label} ({cfg['port']})" for label, cfg in ports
            if port_busy("127.0.0.1", cfg["port"])]
    if busy:
        print("Các cổng sau đang có chương trình khác dùng: " + ", ".join(busy))
        print("Có thể demo đang chạy ở cửa sổ khác. Đóng cửa sổ đó rồi chạy lại,")
        print("hoặc đổi cổng trong cau-hinh\\mang.json.")
        return 2

    common.LOG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    processes: list[tuple[str, subprocess.Popen]] = []
    job = _KillOnClose()   # kept alive for the launcher's whole life
    try:
        for name, module, wait_for in PARTS:
            log_file = open(common.LOG_DIR / f"{stamp}-{name}.log", "w", encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-m", module], cwd=common.ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=env)
            processes.append((name, process))
            job.add(process)
            threading.Thread(target=pump, args=(name, process, log_file), daemon=True).start()
            if wait_for is not None:
                section, key = wait_for
                if not wait_port("127.0.0.1", net[section][key], 15):
                    print(f"*** {name} không mở được cổng {net[section][key]}, dừng demo.")
                    return 1
        web, sim = net["web"], net["mo_phong_web"]
        twin_url = f"http://127.0.0.1:{web['port']}/"
        sim_url = f"http://127.0.0.1:{sim['port']}/"
        print()
        print(f"  Bản sao 3D:      {twin_url}")
        print(f"  Bảng mô phỏng:   {sim_url}")
        print("  Đóng cửa sổ này (hoặc Ctrl+C) để dừng tất cả.")
        print()
        if not args.khong_mo_trinh_duyet:
            webbrowser.open(twin_url)
            webbrowser.open(sim_url)
        while True:
            time.sleep(1)
            if all(p.poll() is not None for _, p in processes):
                return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for _name, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for _name, process in processes:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
