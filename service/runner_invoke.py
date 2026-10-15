# -*- coding: utf-8 -*-
"""子进程调用现有 UIA runner；不修改 runner 源码。"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from service import config

_proc_lock = threading.Lock()
_current_proc: Optional[subprocess.Popen] = None


def kill_current_uia() -> bool:
    """取消任务时打断正在跑的 UIA 子进程，避免继续抢鼠标。

    Windows 上用 taskkill /T /F 杀进程树（含子进程），比 proc.kill() 更干净。
    """
    global _current_proc
    with _proc_lock:
        proc = _current_proc
        _current_proc = None
    if proc is None or proc.poll() is not None:
        return False
    pid = getattr(proc, "pid", None)
    killed = False
    if pid and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            killed = True
        except Exception:
            pass
    try:
        proc.kill()
        killed = True
    except OSError:
        pass
    return killed


def kill_all_uia_runners() -> int:
    """应急：杀掉所有 run_cnipa_*_uia.py（不依赖当前 task 句柄）。"""
    if os.name != "nt":
        return 0
    n = 0
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "CommandLine like '%run_cnipa_%uia.py%'",
             "get", "ProcessId"],
            text=True,
            errors="replace",
            timeout=10,
        )
    except Exception:
        out = ""
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", line, "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            n += 1
        except Exception:
            pass
    return n


def _parse_last_json(stdout: str) -> Optional[dict]:
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def run_uia(script_name: str, args: dict, timeout_sec: int) -> Tuple[bool, dict, str]:
    global _current_proc
    root = Path(config.python_root())
    script = root / script_name
    if not script.is_file():
        return False, {}, f"SCRIPT_MISSING: {script}"

    input_dir = root / "output" / "uia_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    base = script_name.replace(".py", "").replace("run_", "")
    input_file = input_dir / f"{base}_{int(time.time() * 1000)}.json"
    input_file.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["CNIPA_CONNECTOR_ROOT"] = str(root)
    env["CNIPA_EMIT_JSON"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("CNIPA_FILE_ROOT", config.file_root())
    env.setdefault("CNIPA_FILE_URL_PREFIX", "/cnipa_file")
    # 与 service 启动 Chrome 同一 profile，供 runner 内 open_url_via_chrome 附加标签
    try:
        from service import chrome_bootstrap as _cb

        env.setdefault("CNIPA_CHROME_USER_DATA_DIR", _cb._user_data_dir())
        exe = _cb._chrome_exe()
        if exe:
            env.setdefault("CNIPA_CHROME_EXE", exe)
    except Exception:
        pass

    sk = config.dev_skip_stages(args.get("dev_skip_stages"))
    if sk:
        env["CNIPA_DEV_SKIP_STAGES"] = ",".join(sorted(sk))
    if config.dev_reuse_detail_cache():
        env["CNIPA_DEV_REUSE_DETAIL_CACHE"] = "1"
    if config.dev_reuse_search_cache():
        env["CNIPA_DEV_REUSE_SEARCH_CACHE"] = "1"

    try:
        proc = subprocess.Popen(
            [config.python_exe(), "-X", "utf8", str(script), str(input_file)],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with _proc_lock:
            _current_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            pid = proc.pid
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
            except Exception:
                proc.kill()
            stdout, stderr = proc.communicate()
            return False, {"success": False, "error": "TASK_TIMEOUT", "message": f"{script_name} timeout"}, "TASK_TIMEOUT"
        finally:
            with _proc_lock:
                if _current_proc is proc:
                    _current_proc = None
    except OSError as e:
        return False, {}, f"RUNTIME_ERROR: {e}"

    emitted = _parse_last_json(stdout or "")
    if emitted is not None:
        return True, emitted, ""

    if proc.returncode and proc.returncode < 0:
        return False, {"success": False, "error": "CANCELLED", "message": "UIA 进程被中止"}, "CANCELLED"

    err = (stderr or "").strip() or (stdout or "").strip()[-500:]
    return False, {}, err or f"exit {proc.returncode} no contract JSON"


def search_decisions(payload: dict, timeout_sec: int = 900) -> Tuple[bool, dict, str]:
    return run_uia("run_cnipa_search_decisions_uia.py", payload, timeout_sec)


def fetch_detail(payload: dict, timeout_sec: int | None = None) -> Tuple[bool, dict, str]:
    if timeout_sec is None:
        timeout_sec = config.detail_runner_timeout_sec()
    return run_uia("run_cnipa_fetch_decision_detail_uia.py", payload, timeout_sec)


def fetch_figure(payload: dict, timeout_sec: int = 600) -> Tuple[bool, dict, str]:
    return run_uia("run_cnipa_fetch_abstract_figure_uia.py", payload, timeout_sec)


def check_status(timeout_sec: int = 120) -> Tuple[bool, dict, str]:
    return run_uia("run_cnipa_check_status_uia.py", {}, timeout_sec)


def login(payload: Optional[dict] = None, timeout_sec: int = 300) -> Tuple[bool, dict, str]:
    return run_uia("run_cnipa_login_uia.py", payload or {}, timeout_sec)
