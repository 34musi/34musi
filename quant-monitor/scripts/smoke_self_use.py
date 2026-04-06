#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自用路线图 · 冒烟：不启动 uvicorn，用 FastAPI TestClient 走通核心路由与数据库。

用法（在 quant-monitor 目录下）::

    python scripts/smoke_self_use.py

环境变量 API_KEY 若与 .env 一致，会自动加入请求头（与线上行为一致）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)


def main() -> int:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    key = (get_settings().api_key or "").strip()
    headers = {"X-API-Key": key} if key else {}

    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"

    r = c.get("/meta/self-use")
    assert r.status_code == 200, r.text
    js = r.json()
    assert js.get("tool_mode") == "assist_only"
    assert js.get("automatic_trading_supported") is False

    r = c.get("/meta/disclaimer")
    assert r.status_code == 200, r.text

    r = c.get("/journal", headers=headers)
    assert r.status_code == 200, r.text

    payload = {
        "title": "smoke 测试记录",
        "body": "冒烟脚本自动写入，可删除。",
        "symbol": "600879",
        "attach_current_signal": False,
    }
    r = c.post("/journal", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    r = c.get(f"/journal/{eid}", headers=headers)
    assert r.status_code == 200, r.text
    r = c.delete(f"/journal/{eid}", headers=headers)
    assert r.status_code == 200, r.text

    print("[ok] health, meta/self-use, meta/disclaimer, journal create/get/delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
