#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
停止 quant-monitor / uvicorn 服务（释放监听端口）。

用法（在 quant-monitor 目录下）::

    python stop.py
    python stop.py --port 8000
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time


def find_listening_pids(port: int) -> list[int]:
    if sys.platform == "win32":
        return _find_pids_windows(port)
    return _find_pids_unix(port)


def _find_pids_windows(port: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    pids: list[int] = []
    seen: set[int] = set()
    pattern = re.compile(rf":{port}\s+.*LISTENING\s+(\d+)\s*$")
    for line in out.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        pid = int(match.group(1))
        if pid not in seen:
            seen.add(pid)
            pids.append(pid)
    return pids


def _find_pids_unix(port: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except FileNotFoundError:
        pass
    except subprocess.CalledProcessError:
        return []

    try:
        out = subprocess.check_output(
            ["ss", "-ltnp"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    pids: list[int] = []
    seen: set[int] = set()
    for line in out.splitlines():
        if f":{port}" not in line or "LISTEN" not in line:
            continue
        for match in re.finditer(r"pid=(\d+)", line):
            pid = int(match.group(1))
            if pid not in seen:
                seen.add(pid)
                pids.append(pid)
    return pids


def kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False
    except OSError:
        return False


def kill_uvicorn_by_name() -> None:
    if sys.platform != "win32":
        return
    subprocess.run(
        ["taskkill", "/IM", "uvicorn.exe", "/F"],
        capture_output=True,
        text=True,
    )


def _configure_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="停止 quant-monitor uvicorn 服务")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="要释放的端口（默认 8000）",
    )
    args = parser.parse_args()
    port = args.port

    print(f"正在停止占用 {port} 端口的 quant-monitor / uvicorn 进程...")

    pids = find_listening_pids(port)
    if not pids:
        print(f"{port} 端口当前无监听进程。")
    else:
        for pid in pids:
            print(f"结束 PID {pid}")
            kill_pid(pid)

    kill_uvicorn_by_name()

    time.sleep(0.5)
    remaining = find_listening_pids(port)
    print()
    if not remaining:
        print(f"{port} 端口已释放。")
        return 0

    print(
        f"仍有进程占用 {port}（PID: {', '.join(map(str, remaining))}）。"
        "请以管理员身份再运行，或在任务管理器中结束对应 python.exe。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
