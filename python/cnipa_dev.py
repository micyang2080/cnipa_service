# -*- coding: utf-8 -*-
"""测试阶段：CNIPA_DEV_SKIP_STAGES / CNIPA_DEV_REUSE_* 环境变量。"""
from __future__ import annotations

import os


def _split(raw: str) -> set:
    out = set()
    for part in (raw or '').replace(';', ',').split(','):
        p = part.strip().lower()
        if p:
            out.add(p)
    return out


def skip_stages(extra=None) -> set:
    s = _split(os.environ.get('CNIPA_DEV_SKIP_STAGES', ''))
    if extra:
        if isinstance(extra, str):
            s |= _split(extra)
        else:
            for x in extra:
                s |= _split(str(x))
    return s


def should_skip(name: str, extra=None) -> bool:
    return name.strip().lower() in skip_stages(extra)


def reuse_detail_cache() -> bool:
    return os.environ.get('CNIPA_DEV_REUSE_DETAIL_CACHE', '').strip().lower() in (
        '1', 'true', 'yes')


def reuse_search_cache() -> bool:
    return os.environ.get('CNIPA_DEV_REUSE_SEARCH_CACHE', '').strip().lower() in (
        '1', 'true', 'yes')
