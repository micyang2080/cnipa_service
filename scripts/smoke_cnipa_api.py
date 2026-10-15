# -*- coding: utf-8 -*-
"""本机联调：先查桌面环境，再创建任务并轮询。

  python scripts/smoke_cnipa_api.py --token YOUR_TOKEN --env-only
  python scripts/smoke_cnipa_api.py --token YOUR_TOKEN --type 2 --app 200610004173.6
  python scripts/smoke_cnipa_api.py --token YOUR_TOKEN --type 1 --case 4W107577
  python scripts/smoke_cnipa_api.py --token YOUR_TOKEN --get 123
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

DEFAULT_BASE = "http://127.0.0.1:8572/cnipa"


def _headers(token: str) -> dict:
    return {"tokenID": token, "Content-Type": "application/json"}


def check_env(base: str, token: str) -> dict:
    r = requests.get(f"{base}/v1/env", headers=_headers(token), timeout=180)
    print("GET /v1/env ->", r.status_code)
    try:
        data = r.json()
    except ValueError:
        print(r.text[:500] if r.text else "(empty body)")
        if r.status_code == 404:
            sys.exit(
                "当前服务进程没有 /v1/env（旧进程）。请停掉后重新: python -m service"
            )
        sys.exit(f"env 响应非 JSON, HTTP {r.status_code}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def create(base: str, token: str, body: dict) -> dict:
    r = requests.post(f"{base}/v1/tasks", headers=_headers(token), json=body, timeout=60)
    print("POST /v1/tasks ->", r.status_code)
    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if r.status_code == 401:
        sys.exit("鉴权失败，检查 token 或服务端 auth.disabled")
    return data


def get_task(base: str, token: str, task_id: int) -> dict:
    r = requests.get(f"{base}/v1/tasks/{task_id}", headers=_headers(token), timeout=60)
    print(f"GET /v1/tasks/{task_id} ->", r.status_code)
    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default="", help="tokenID；鉴权关闭时可空")
    ap.add_argument("--type", type=int, choices=(1, 2), default=2)
    ap.add_argument("--app", default="")
    ap.add_argument("--case", default="")
    ap.add_argument("--decision", default="")
    ap.add_argument("--library", default="invalid", choices=("invalid", "reexam"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--get", type=int, default=0)
    ap.add_argument("--env-only", action="store_true", help="只检查桌面环境")
    ap.add_argument("--skip-env", action="store_true", help="跳过投任务前的环境检查")
    args = ap.parse_args()

    token = args.token or "dev"
    base = args.base.rstrip("/")

    h = requests.get(f"{base}/health", timeout=10)
    print("health ->", h.status_code, h.text)
    if h.status_code != 200:
        sys.exit("服务未就绪，先启动: python -m service")

    if args.get:
        get_task(base, token, args.get)
        return

    if not args.skip_env or args.env_only:
        env = check_env(base, token)
        ready = bool((env.get("env") or {}).get("ready") or (env.get("env") or {}).get("success"))
        if args.env_only:
            sys.exit(0 if ready else 2)
        if not ready:
            print(
                "\n当前 ready=false（正常）。将继续创建任务，"
                "由 worker 自动启动 Chrome + account.ini 登录。\n"
            )

    if args.type == 2:
        if not args.app:
            sys.exit("type=2 需要 --app")
        body = {
            "tokenID": token,
            "task_type": 2,
            "application_no": args.app,
            "force_refresh": bool(args.force),
        }
    else:
        filters = {}
        if args.app:
            filters["application_no"] = args.app
        if args.case:
            filters["case_no"] = args.case
        if args.decision:
            filters["decision_no"] = args.decision
        if not filters:
            sys.exit("type=1 需要 --app / --case / --decision 至少一个")
        body = {
            "tokenID": token,
            "task_type": 1,
            "library_type": args.library,
            "filters": filters,
            "max_items": 10,
            "page": 1,
            "force_refresh": bool(args.force),
        }

    data = create(base, token, body)
    if not data.get("ok") and not data.get("task_id"):
        sys.exit(1)
    tid = data.get("task_id")
    if args.once or not tid:
        return

    print(f"\n轮询 task_id={tid}，间隔 {args.interval}s，Ctrl+C 取消任务并退出\n")
    try:
        while True:
            time.sleep(args.interval)
            data = get_task(base, token, int(tid))
            st = data.get("status")
            if st in (3, 4, 5):
                print("\n结束 status=", st)
                break
    except KeyboardInterrupt:
        print(f"\n取消 task_id={tid} ...")
        try:
            r = requests.post(
                f"{base}/v1/tasks/{tid}/cancel",
                headers=_headers(token),
                timeout=30,
            )
            print("POST cancel ->", r.status_code, r.text[:300])
        except Exception as e:
            print("cancel failed:", e)
        sys.exit(130)


if __name__ == "__main__":
    main()
