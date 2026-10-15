# -*- coding: utf-8 -*-
"""MinerU HTTP /file_parse 客户端（供 cnipa_ocr 在 WinRT 不可用时回退）。

对齐 lit-similar-cases/scripts/test_mineru_pdf.py 与 OCR 识别demo/run_mineru_mvp.py：
POST {api}/file_parse，multipart files + backend=pipeline|vlm-http-client。
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import mimetypes
import os
import re
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

_ROOT = Path(__file__).resolve().parent
_DEFAULT_API = "http://192.168.100.30:29000"


def _ini_paths() -> list[Path]:
    env = (os.environ.get("CNIPA_CONFIG") or "").strip()
    out = []
    if env:
        out.append(Path(env))
    out.append(_ROOT.parent / "service" / "config.ini")
    out.append(Path(r"D:\cnipa_service\service\config.ini"))
    return out


def load_ocr_settings() -> dict[str, Any]:
    """[ocr] + 环境变量。backend: auto|winrt|mineru。"""
    cfg = {
        "backend": "mineru",
        "mineru_url": _DEFAULT_API,
        "mineru_backend": "pipeline",
        "mineru_server_url": "",
        "mineru_lang": "ch",
        "mineru_timeout_sec": 180,
        "mineru_parse_method": "auto",
    }
    cp = configparser.ConfigParser()
    for p in _ini_paths():
        if p.is_file():
            try:
                cp.read(str(p), encoding="utf-8")
            except Exception:
                cp.read(str(p))
            break
    if cp.has_section("ocr"):
        sec = cp["ocr"]
        for k in list(cfg):
            if k in sec and str(sec.get(k, "")).strip():
                v = sec.get(k).strip()
                if k == "mineru_timeout_sec":
                    try:
                        cfg[k] = int(v)
                    except ValueError:
                        pass
                else:
                    cfg[k] = v
    # 环境变量覆盖
    if os.environ.get("CNIPA_OCR_BACKEND"):
        cfg["backend"] = os.environ["CNIPA_OCR_BACKEND"].strip().lower()
    if os.environ.get("MINERU_API_URL"):
        cfg["mineru_url"] = os.environ["MINERU_API_URL"].strip().rstrip("/")
    if os.environ.get("MINERU_BACKEND"):
        cfg["mineru_backend"] = os.environ["MINERU_BACKEND"].strip()
    if os.environ.get("MINERU_SERVER_URL"):
        cfg["mineru_server_url"] = os.environ["MINERU_SERVER_URL"].strip()
    cfg["backend"] = str(cfg["backend"]).strip().lower()
    cfg["mineru_url"] = str(cfg["mineru_url"]).rstrip("/")
    return cfg


def _str_bool(v: bool) -> str:
    return "true" if v else "false"


def _form_data(
    *,
    backend: str,
    lang: str,
    parse_method: str,
    server_url: Optional[str],
    return_md: bool = True,
    return_middle_json: bool = True,
    return_content_list: bool = True,
    return_images: bool = False,
) -> list[tuple[str, str]]:
    data: list[tuple[str, str]] = [
        ("lang_list", lang),
        ("backend", backend),
        ("parse_method", parse_method),
        ("formula_enable", "false"),
        ("table_enable", "true"),
        ("image_analysis", "false"),
        ("return_md", _str_bool(return_md)),
        ("return_middle_json", _str_bool(return_middle_json)),
        ("return_model_output", "false"),
        ("return_content_list", _str_bool(return_content_list)),
        ("return_images", _str_bool(return_images)),
        ("response_format_zip", "true"),
        ("return_original_file", "false"),
        ("start_page_id", "0"),
        ("end_page_id", "99999"),
    ]
    if server_url:
        data.append(("server_url", server_url))
    return data


def file_parse(
    file_path: str | Path,
    *,
    api_url: Optional[str] = None,
    backend: Optional[str] = None,
    server_url: Optional[str] = None,
    lang: Optional[str] = None,
    parse_method: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    out_dir: Optional[str | Path] = None,
) -> Path:
    """同步 /file_parse，解压到 out_dir/extracted，返回 job 目录。"""
    if requests is None:
        raise RuntimeError("需要 requests：pip install requests")

    settings = load_ocr_settings()
    file_path = Path(file_path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    api = (api_url or settings["mineru_url"]).rstrip("/")
    backend = backend or settings["mineru_backend"] or "pipeline"
    server_url = server_url if server_url is not None else (settings["mineru_server_url"] or None)
    if server_url == "":
        server_url = None
    lang = lang or settings["mineru_lang"] or "ch"
    parse_method = parse_method or settings["mineru_parse_method"] or "auto"
    timeout_seconds = int(timeout_seconds or settings["mineru_timeout_sec"] or 180)

    if str(backend).endswith("http-client") and not server_url:
        raise ValueError("vlm-http-client 需要 mineru_server_url / MINERU_SERVER_URL")

    if out_dir is None:
        digest = hashlib.md5(file_path.read_bytes()).hexdigest()[:16]
        out_dir = Path(tempfile.gettempdir()) / "cnipa_mineru" / f"{file_path.stem}_{digest}"
    job_dir = Path(out_dir).expanduser().resolve()
    extract_dir = job_dir / "extracted"
    if extract_dir.is_dir() and any(extract_dir.rglob("*_middle.json")):
        return job_dir

    job_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    with file_path.open("rb") as fh:
        resp = requests.post(
            f"{api}/file_parse",
            files=[("files", (file_path.name, fh, mime))],
            data=_form_data(
                backend=backend,
                lang=lang,
                parse_method=parse_method,
                server_url=server_url,
            ),
            timeout=timeout_seconds,
        )

    ct = resp.headers.get("Content-Type", "") or ""
    if "application/json" in ct:
        err = job_dir / "response_error.json"
        err.write_text(resp.text[:80000], encoding="utf-8")
        resp.raise_for_status()
        raise RuntimeError(f"MinerU 返回 JSON 而非 ZIP: {err}")

    resp.raise_for_status()
    zip_path = job_dir / "result.zip"
    zip_path.write_bytes(resp.content)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(extract_dir)
    return job_dir


def _bbox_tuple(bbox: Any) -> tuple[int, int, int, int]:
    if not bbox or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return (0, 0, 0, 0)
    return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))


def _image_size(path: str | Path) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image
        with Image.open(str(path)) as im:
            w, h = im.size
            if w > 0 and h > 0:
                return (int(w), int(h))
    except Exception:
        pass
    return None


def _page_scale(
    page: dict, img_size: Optional[tuple[int, int]]
) -> tuple[float, float]:
    """page_size → 截图像素：sx=img_w/page_w, sy=img_h/page_h。"""
    if not img_size:
        return 1.0, 1.0
    ps = page.get("page_size") if isinstance(page, dict) else None
    if not isinstance(ps, (list, tuple)) or len(ps) < 2:
        return 1.0, 1.0
    try:
        pw, ph = float(ps[0]), float(ps[1])
    except (TypeError, ValueError):
        return 1.0, 1.0
    if pw <= 1 or ph <= 1:
        return 1.0, 1.0
    iw, ih = float(img_size[0]), float(img_size[1])
    # 已接近像素坐标则不缩放（避免二次放大）
    if abs(iw - pw) < 2 and abs(ih - ph) < 2:
        return 1.0, 1.0
    return iw / pw, ih / ph


def _scale_bbox(
    bbox: Any, sx: float, sy: float
) -> tuple[int, int, int, int]:
    l, t, r, b = _bbox_tuple(bbox)
    if sx == 1.0 and sy == 1.0:
        return (l, t, r, b)
    return (
        int(round(l * sx)),
        int(round(t * sy)),
        int(round(r * sx)),
        int(round(b * sy)),
    )


def _as_text(val: Any) -> str:
    """MinerU 字段可能是 str / list / dict，统一成可 .strip 的字符串。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float, bool)):
        return str(val).strip()
    if isinstance(val, list):
        parts = [_as_text(x) for x in val]
        return "".join(p for p in parts if p).strip()
    if isinstance(val, dict):
        for k in ("content", "text", "title_content", "paragraph_content",
                  "table_body", "html", "md"):
            if k in val:
                t = _as_text(val.get(k))
                if t:
                    return t
        # 表格单元格等嵌套
        for v in val.values():
            if isinstance(v, (dict, list, str)):
                t = _as_text(v)
                if t:
                    return t
        return ""
    return str(val).strip()


def _line_text_from_spans(line: dict) -> str:
    parts = []
    for sp in line.get("spans") or []:
        if not isinstance(sp, dict):
            continue
        t = _as_text(sp.get("content") if sp.get("content") is not None
                     else sp.get("text"))
        if t:
            parts.append(t)
    return "".join(parts).strip()


def _blocks_for_page(page: dict) -> list:
    """优先 para_blocks，避免与 preproc 重复；都空再试 discarded。"""
    para = page.get("para_blocks") or []
    if para:
        return list(para)
    pre = page.get("preproc_blocks") or []
    if pre:
        return list(pre)
    return list(page.get("discarded_blocks") or [])


def items_from_middle_json(
    data: dict,
    img_size: Optional[tuple[int, int]] = None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """middle.json → [(text, (l,t,r,b)), ...]，框缩放到截图像素（若给 img_size）。"""
    items: list[tuple[str, tuple[int, int, int, int]]] = []
    for page in data.get("pdf_info") or []:
        if not isinstance(page, dict):
            continue
        sx, sy = _page_scale(page, img_size)

        def _box(raw: Any) -> tuple[int, int, int, int]:
            return _scale_bbox(raw, sx, sy)

        for block in _blocks_for_page(page):
            if not isinstance(block, dict):
                continue
            lines = block.get("lines") or []
            if lines:
                for line in lines:
                    if not isinstance(line, dict):
                        continue
                    text = _line_text_from_spans(line)
                    if not text:
                        text = _as_text(line.get("text") or line.get("content"))
                    if not text:
                        continue
                    items.append(
                        (text, _box(line.get("bbox") or block.get("bbox")))
                    )
                continue
            t = _as_text(block.get("text") if block.get("text") is not None
                         else block.get("content"))
            if t:
                items.append((t, _box(block.get("bbox"))))
                continue
            for sub in block.get("blocks") or []:
                if not isinstance(sub, dict):
                    continue
                for line in sub.get("lines") or []:
                    if not isinstance(line, dict):
                        continue
                    text = _line_text_from_spans(line) or _as_text(
                        line.get("text") or line.get("content"))
                    if text:
                        items.append(
                            (text, _box(line.get("bbox") or sub.get("bbox")
                                        or block.get("bbox")))
                        )
                st = _as_text(sub.get("text") if sub.get("text") is not None
                              else sub.get("content"))
                if st and not (sub.get("lines") or []):
                    items.append((st, _box(sub.get("bbox") or block.get("bbox"))))
    return items


def _page_scales_from_middle(
    data: dict, img_size: Optional[tuple[int, int]]
) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for i, page in enumerate(data.get("pdf_info") or []):
        if not isinstance(page, dict):
            continue
        idx = page.get("page_idx")
        try:
            key = int(idx) if idx is not None else i
        except (TypeError, ValueError):
            key = i
        out[key] = _page_scale(page, img_size)
    return out


class _TableHTMLParser(HTMLParser):
    """<table> → rows: list[list[str]]。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_tr = False
        self._in_cell = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_tr = True
            self.rows.append([])
        elif self._in_tr and tag in ("td", "th"):
            self._in_cell = True
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            if self.rows:
                cell = re.sub(r"\s+", " ", "".join(self._parts)).strip()
                self.rows[-1].append(cell)
        elif tag == "tr":
            self._in_tr = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._parts.append(data)


def _html_table_to_items(
    html: str,
    bbox: Any,
    *,
    sx: float = 1.0,
    sy: float = 1.0,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """表格 HTML → 带框行（标签列 / 值列分行，y 按行均分 bbox）。"""
    if not html or "<" not in html:
        return []
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    rows = [r for r in parser.rows if any(c.strip() for c in r)]
    if not rows:
        return []
    l, t, r, b = _scale_bbox(bbox, sx, sy)
    if r <= l:
        r = l + 100
    if b <= t:
        b = t + max(24 * len(rows), 40)
    n = len(rows)
    items: list[tuple[str, tuple[int, int, int, int]]] = []
    mid_x = (l + r) // 2
    gap = max(12, (r - l) // 40)  # 标签右缘与值左缘留缝，供 join_table_rows 对齐
    _INLINE_LAB = re.compile(
        r'^(决定要点|法律依据)\s*[：:．.]\s*(.*)$', re.S)

    for i, row in enumerate(rows):
        y0 = t + int((b - t) * i / n)
        y1 = t + int((b - t) * (i + 1) / n)
        if y1 <= y0:
            y1 = y0 + 12
        cells = [c.strip() for c in row if c and c.strip()]
        if not cells:
            continue
        if len(cells) == 1:
            m = _INLINE_LAB.match(cells[0])
            if m:
                lab, body = m.group(1), (m.group(2) or "").strip()
                items.append((lab, (l, y0, mid_x - gap, y1)))
                if body:
                    items.append((body, (mid_x + gap, y0, r, y1)))
            else:
                items.append((cells[0], (l, y0, r, y1)))
            continue
        lab, val = cells[0], cells[1]
        if lab:
            items.append((lab, (l, y0, mid_x - gap, y1)))
        if val:
            items.append((val, (mid_x + gap, y0, r, y1)))
        for extra in cells[2:]:
            items.append((extra, (mid_x + gap, y0, r, y1)))
    return items


def items_from_content_list(
    data: Any,
    *,
    img_size: Optional[tuple[int, int]] = None,
    page_scales: Optional[dict[int, tuple[float, float]]] = None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    items: list[tuple[str, tuple[int, int, int, int]]] = []
    scales = page_scales or {}
    default_sx, default_sy = 1.0, 1.0
    if img_size and len(scales) == 1:
        default_sx, default_sy = next(iter(scales.values()))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            try:
                pidx = int(node.get("page_idx", 0) or 0)
            except (TypeError, ValueError):
                pidx = 0
            sx, sy = scales.get(pidx, (default_sx, default_sy))
            ntype = str(node.get("type") or "")

            # 表格：caption + table_body HTML（pipeline 正文常在这里）
            if ntype == "table" or node.get("table_body"):
                for cap in node.get("table_caption") or []:
                    ct = _as_text(cap)
                    if ct:
                        items.append((ct, _scale_bbox(node.get("bbox"), sx, sy)))
                body = node.get("table_body")
                if isinstance(body, str) and body.strip():
                    items.extend(
                        _html_table_to_items(body, node.get("bbox"), sx=sx, sy=sy)
                    )
                elif body is not None and not isinstance(body, str):
                    bt = _as_text(body)
                    if bt:
                        items.append((bt, _scale_bbox(node.get("bbox"), sx, sy)))

            text = _as_text(
                node.get("text") if node.get("text") is not None
                else node.get("content"))
            if text and "bbox" in node and ntype != "table":
                items.append((text, _scale_bbox(node.get("bbox"), sx, sy)))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(data)
    return items


def _merge_ocr_items(
    primary: list[tuple[str, tuple[int, int, int, int]]],
    extra: list[tuple[str, tuple[int, int, int, int]]],
) -> list[tuple[str, tuple[int, int, int, int]]]:
    seen = {(t, b) for t, b in primary}
    out = list(primary)
    for t, b in extra:
        key = (t, b)
        if key in seen:
            continue
        # 跳过已被 caption 覆盖的短重复
        if any(t == pt and abs(b[1] - pb[1]) < 8 for pt, pb in primary):
            continue
        seen.add(key)
        out.append((t, b))
    out.sort(key=lambda it: (it[1][1], it[1][0]))
    return out


def items_from_job_dir(
    job_dir: str | Path,
    img_size: Optional[tuple[int, int]] = None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    root = Path(job_dir) / "extracted"
    if not root.is_dir():
        return []
    middle_data = None
    items: list[tuple[str, tuple[int, int, int, int]]] = []
    for p in sorted(root.rglob("*_middle.json")):
        try:
            middle_data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = items_from_middle_json(middle_data, img_size=img_size)
        if items:
            break
    page_scales = (
        _page_scales_from_middle(middle_data, img_size)
        if isinstance(middle_data, dict) else None
    )
    # 必须合并 content_list：表格正文在 table_body HTML，middle 常只有表题
    for p in sorted(root.rglob("*content_list*.json")):
        # 优先非 v2 或都读；同内容去重
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        cl_items = items_from_content_list(
            data, img_size=img_size, page_scales=page_scales)
        if cl_items:
            items = _merge_ocr_items(items, cl_items)
            break
    if items:
        return items
    for p in sorted(root.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            return [(ln, (0, 0, 0, 0)) for ln in lines]
    return []


def ocr_image_mineru(img_path: str, timeout: Optional[int] = None) -> list:
    """图片走 MinerU，返回与 cnipa_ocr.ocr_image 相同结构（像素框）。"""
    img_size = _image_size(img_path)
    job = file_parse(img_path, timeout_seconds=timeout)
    items = items_from_job_dir(job, img_size=img_size)
    safe = []
    for tx, bx in items:
        t = _as_text(tx)
        if t:
            safe.append((t, bx if isinstance(bx, tuple) else _bbox_tuple(bx)))
    if not safe:
        raise RuntimeError(f"MinerU 无可用文本: {job}")
    return safe
