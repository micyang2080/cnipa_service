# -*- coding: utf-8 -*-
from __future__ import annotations

import configparser
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_INI = _ROOT / "service" / "config.ini"


def _load_ini() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if _INI.is_file():
        cp.read(str(_INI), encoding="utf-8")
    return cp


_INI_DATA = _load_ini()


def _get(section: str, key: str, default: str = "") -> str:
    env_key = f"CNIPA_{section.upper()}_{key.upper()}"
    if env_key in os.environ:
        return os.environ[env_key].strip()
    flat = f"CNIPA_{key.upper()}"
    if flat in os.environ and section == "server":
        return os.environ[flat].strip()
    if _INI_DATA.has_option(section, key):
        return _INI_DATA.get(section, key).strip()
    return default


def mysql_host() -> str:
    return os.environ.get("CNIPA_MYSQL_HOST") or _get("mysql", "host", "127.0.0.1")


def mysql_port() -> int:
    return int(os.environ.get("CNIPA_MYSQL_PORT") or _get("mysql", "port", "3306"))


def mysql_user() -> str:
    return os.environ.get("CNIPA_MYSQL_USER") or _get("mysql", "user", "cnipauser")


def mysql_password() -> str:
    return os.environ.get("CNIPA_MYSQL_PASSWORD") or _get("mysql", "password", "cnipa$6_5@2027")


def mysql_database() -> str:
    return os.environ.get("CNIPA_MYSQL_DATABASE") or _get("mysql", "database", "cnipa_db")


def listen_host() -> str:
    return os.environ.get("CNIPA_LISTEN_HOST") or _get("server", "host", "0.0.0.0")


def listen_port() -> int:
    return int(os.environ.get("CNIPA_LISTEN_PORT") or _get("server", "port", "8572"))


def file_root() -> str:
    return os.environ.get("CNIPA_FILE_ROOT") or _get("file", "root", r"D:\cnipa_file")


def task_timeout_sec() -> int:
    return int(os.environ.get("CNIPA_TASK_TIMEOUT_SEC") or _get("worker", "task_timeout_sec", "1800"))


def detail_runner_timeout_sec() -> int:
    return int(
        os.environ.get("CNIPA_DETAIL_RUNNER_TIMEOUT_SEC")
        or _get("worker", "detail_runner_timeout_sec", "1800")
    )


def poll_interval_sec() -> float:
    return float(os.environ.get("CNIPA_POLL_INTERVAL_SEC") or _get("worker", "poll_interval_sec", "2"))


def python_root() -> str:
    return os.environ.get("CNIPA_CONNECTOR_ROOT") or _get(
        "uia", "python_root", str(_ROOT / "python")
    )


def python_exe() -> str:
    return os.environ.get("CNIPA_PYTHON") or _get("uia", "python_exe", "python")


def ipsec_base_url() -> str:
    return (
        os.environ.get("KB_IPSEC_BASE_URL")
        or os.environ.get("CNIPA_IPSEC_BASE_URL")
        or _get("auth", "ipsec_base_url", "https://ips.wanhuida.cn/ip_sec/interface")
    ).rstrip("/")


def auth_disabled() -> bool:
    return (os.environ.get("CNIPA_AUTH_DISABLED") or _get("auth", "disabled", "0")).strip() in (
        "1",
        "true",
        "True",
    )


def repo_root() -> Path:
    return _ROOT


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def dev_skip_stages(extra=None) -> frozenset:
    raw = os.environ.get("CNIPA_DEV_SKIP_STAGES") or _get("dev", "skip_stages", "")
    parts = set()
    for p in raw.replace(";", ",").split(","):
        p = p.strip().lower()
        if p:
            parts.add(p)
    if extra:
        if isinstance(extra, (list, tuple)):
            for x in extra:
                parts.add(str(x).strip().lower())
        else:
            for p in str(extra).replace(";", ",").split(","):
                p = p.strip().lower()
                if p:
                    parts.add(p)
    return frozenset(parts)


def dev_reuse_detail_cache() -> bool:
    if "CNIPA_DEV_REUSE_DETAIL_CACHE" in os.environ:
        return _truthy(os.environ["CNIPA_DEV_REUSE_DETAIL_CACHE"])
    return _truthy(_get("dev", "reuse_detail_cache", "0"))


def dev_reuse_search_cache() -> bool:
    if "CNIPA_DEV_REUSE_SEARCH_CACHE" in os.environ:
        return _truthy(os.environ["CNIPA_DEV_REUSE_SEARCH_CACHE"])
    return _truthy(_get("dev", "reuse_search_cache", "0"))


def dev_only_figure() -> bool:
    if "CNIPA_DEV_ONLY_FIGURE" in os.environ:
        return _truthy(os.environ["CNIPA_DEV_ONLY_FIGURE"])
    return _truthy(_get("dev", "only_figure", "0"))
