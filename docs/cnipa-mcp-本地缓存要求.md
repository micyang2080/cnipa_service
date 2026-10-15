# cnipa-mcp · 本地持久缓存要求

> **面向**：`C:\git\Mcps\cnipa-mcp` 开发  
> **关联**：`docs/cnipa-mcp-技能侧契约要求.md`（契约 JSON）、`docs/接口契约.md`（v2）  
> **技能**：`patent-reexam-invalid-report`  
> **日期**：2026-09-03

---

## 0. 结论（是否就是你理解的那样）

**是。** 复审/无效**决定**是已发布的历史文书，内容稳定，**不需要 TTL**（与「按申请人查商标、状态会变」不同）。

MCP 应实现：

1. **检索 / 详情 / 附图** 三类结果**持久化到本地**；
2. 各工具**先查本地缓存，未命中再访问国知局**；
3. 命中缓存时仍返回与线上一致的**契约 JSON**（v2 固定 20 键），技能侧**零感知**差异。

另：**`cache_file`**（大结果落盘、供 `ingest --input`）与**持久缓存**是同一目录体系下的两个用途——持久文件即 `cache_file` 指向的对象；见 §6。

---

## 1. 设计原则

| 原则 | 说明 |
|------|------|
| **无过期** | 默认不设 TTL；决定号、案件号、申请号对应内容视为不变 |
| **缓存优先** | 默认 `force_refresh=false`：有有效缓存则**不打开** cpquery / pss |
| **契约一致** | 缓存文件内容 = `cnipa_contract.clean_*` 投影后的 JSON，与线上一致 |
| **键可复现** | 同一规范化入参 → 同一缓存路径（便于人工排查） |
| **附图按申请号** | 文件名含规范化 `application_no`（含点），与技能 `figures/{application_no}.png` 对齐 |
| **可关可刷** | 环境变量关缓存；入参 `force_refresh` 强制重抓并覆盖 |

---

## 2. 目录与根路径

默认根目录（可通过环境变量覆盖）：

```
{CNIPA_CACHE_ROOT}  默认 = {CNIPA_ROOT}/output/cnipa_cache
```

```
cnipa_cache/
├── search/                          # 检索契约 JSON
│   └── {library_type}/
│       └── {query_key}.json
├── detail/                          # 详情契约 JSON
│   └── {library_type}/
│       └── {application_no}__{case_no}.json
├── figures/                         # 摘要附图（二进制）
│   └── {application_no}.png       # 含点，如 200610004173.6.png
└── meta/                            # 可选：索引 / 统计，非必须
```

| 环境变量 | 含义 |
|----------|------|
| `CNIPA_CACHE_ROOT` | 缓存根目录 |
| `CNIPA_CACHE_DISABLED=1` | 全局跳过读/写缓存（联调、抓包用） |

---

## 3. 缓存键规则

### 3.1 规范化（三类共用）

- `application_no`：统一为 **13 位含点**（如 `200610004173.6`）；文件名中 `.` 保留（Windows 合法）。
- `library_type`：`invalid` | `reexam`。
- `filters`：键名按契约 12 项排序；值 `strip()`；空串省略；`application_no` / `case_no` 走同一规范化函数。

### 3.2 检索 `cnipa_search_decisions`

```
query_key = sha256( json.dumps({
  "library_type": "...",
  "filters": { ... 规范化后非空键值 ... },
  "max_items": N          // 纳入键：不同上限视为不同缓存
}, sort_keys=True) )[:16]

路径: search/{library_type}/{query_key}.json
```

**说明**：组合条件 AND 与站点一致；仅改 `page` 不改变语义时可忽略 `page`（内部自动翻页至 `max_items`）。

**边界**：

- `filters` 全空 → 不读不写缓存，直接 `FILTER_EMPTY`。
- 上次 `truncated: true` 且本次 `max_items` 更大 → 视为未命中，重查并覆盖。

### 3.3 详情 `cnipa_fetch_decision_detail`

```
路径: detail/{library_type}/{application_no}__{case_no}.json
```

- `library_type` 可由 `list_item.review_type` 推断（1→reexam，2→invalid）。
- 仅有 `case_no` 或仅有 `application_no` 时：文件名缺侧用 `_` 占位，如 `200610004173.6__.json`；**仍建议两端都有再写缓存**。
- 缓存内容：`{ "success": true, "detail": { ...20 键... } }`（契约投影后）。

### 3.4 附图 `cnipa_fetch_abstract_figure`

```
路径: figures/{application_no}.png
```

- 命中：若文件存在且 size > 0 → 成功返回；**可选** copy 到调用方 `save_path`（见 §5.3）。
- `NO_FIGURE`：**建议负缓存**——写 `figures/{application_no}.no_figure` 空标记文件；后续同号直接返回 `NO_FIGURE`，避免重复开 pss。人工删标记可重试。

---

## 4. 各工具流程

### 4.1 `cnipa_search_decisions`

```
入参校验
  → 若 CNIPA_CACHE_DISABLED：跳至线上下行
  → 算 query_key，读 search/.../query_key.json
  → 命中且 force_refresh=false：
        返回契约 JSON + from_cache=true + cache_file=该路径
  → 未命中：
        UIA/PW 检索国知局
        clean_search(payload) → 写 search/.../query_key.json
        返回 + from_cache=false + cache_file=该路径
```

### 4.2 `cnipa_fetch_decision_detail`

```
规范化 application_no / case_no / library_type
  → 读 detail/.../xxx.json
  → 命中且 force_refresh=false → 返回 + from_cache=true + cache_file
  → 未命中 → 下钻/OCR → clean_detail → 写入 → 返回
```

### 4.3 `cnipa_fetch_abstract_figure`

```
规范化 application_no
  → 若存在 figures/{application_no}.no_figure → NO_FIGURE（from_cache=true）
  → 若存在 figures/{application_no}.png 且 force_refresh=false：
        figure_path = 缓存路径 或 copy 至 save_path（若提供）
        返回 success + from_cache=true
  → 未命中 → pss 抓图 → 写入 figures/{application_no}.png
        若 save_path 与缓存路径不同 → copy 到 save_path
        返回 + from_cache=false
```

---

## 5. 返回字段扩展（在 v2 契约之上）

各工具成功/失败响应均可增加（**技能可忽略**）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `from_cache` | bool | `true`=未访问国知局 |
| `cache_file` | string | 本次结果对应的**持久契约 JSON 路径**（search/detail）；figure 为图片路径 |
| `cache_key` | string | 可选，调试：`query_key` 或 `application_no__case_no` |

**Node `formatResult`**：已有 `cache_file` 展示；建议追加 `from_cache: true/false`。

### 5.1 入参扩展（三工具统一）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `force_refresh` | bool | `false` | `true` 时跳过读缓存，成功后**覆盖**写缓存 |

---

## 6. `cache_file` 与持久缓存的关系

| 概念 | 含义 |
|------|------|
| **持久缓存** | 跨 run、跨 MCP 重启；键见 §3 |
| **`cache_file`** | 响应里告诉调用方：**完整契约 JSON 或附图文件**的绝对路径 |

要求：

1. **每次** search/detail 成功（无论是否 `from_cache`）响应中带 `cache_file`；
2. `cache_file` **优先**指向 §2 持久路径，**不要**仅指向 `uia_runs/run_*/payload.json`（含遥测，非契约）；
3. 技能 Agent：`ingest_cnipa_decision_result.py --input {cache_file}`，**禁止** Read 超大 MCP tool-results 正文；
4. `uia_runs/` 仍保留 `payload.json` / `debug.json` 作排障，与 `cache_file` 分离。

---

## 7. 与技能侧接线

```
用法 A：
  cnipa_search_decisions → cache_file → ingest（items[]）
  cnipa_fetch_decision_detail → cache_file → ingest 合并 detail（可选）
  cnipa_fetch_abstract_figure → figure_path（缓存或 save_path）

用法 B（知产宝 + 附图）：
  逐条 cnipa_fetch_abstract_figure(application_no)
  → 命中 figures/{application_no}.png 时不访问 pss
  → fetch_abstract_figures.py --figure-map
```

技能**不**实现缓存逻辑；只消费 MCP 返回的契约 JSON / `figure_path` / `cache_file`。

---

## 8. 实现落点（MCP 代码）

| 模块 | 动作 |
|------|------|
| 新建 `python/cnipa_cache.py` | `cache_root()`、`search_path()`、`detail_path()`、`figure_path()`、`read_json()`、`write_json()`、`copy_figure()` |
| `cnipa_contract.py` | `write_search_cache` / `write_detail_cache` 写入 §2 路径并返回绝对路径 |
| `run_cnipa_search_decisions_uia.py` | `_finish` 前：查缓存短路；成功后 `write_search_cache`；设 `cache_file`、`from_cache` |
| `run_cnipa_fetch_decision_detail_uia.py` | 同上 |
| `run_cnipa_fetch_abstract_figure_uia.py` | 入口查 `figures/`；成功写 png；`NO_FIGURE` 写 `.no_figure` |
| `src/index.ts` | 三工具 schema 增加 `force_refresh`；`formatResult` 展示 `from_cache` |
| `docs/接口契约.md` | 增补 §缓存、`force_refresh`、`from_cache`、`cache_file` |

PW 轨道（若仍维护）与 UIA **共用** `cnipa_cache.py`，避免双实现。

---

## 9. 验收

1. **检索**：同一 `filters` 第二次调用 → `from_cache=true`，无浏览器/窗口操作日志。
2. **详情**：同一 `application_no`+`case_no` 第二次 → 命中 `detail/...json`。
3. **附图**：同一 `application_no` 第二次 → 命中 `figures/200610004173.6.png`；用法 B 联调通过。
4. **契约**：缓存 JSON 与线上一致，通过 `cnipa_contract.py --selftest` 及技能 `ingest_cnipa_decision_result.py --input {cache_file}`。
5. **刷新**：`force_refresh=true` 覆盖旧文件，`from_cache=false`。
6. **负缓存**：确认无附图案件第二次直接 `NO_FIGURE`，不打开 pss。
7. **回归样例**：`200610004173.6` / `4W107577` 全链路；删缓存目录后首跑写盘，二跑全命中。

---

## 10. 非目标（本期不做）

- 缓存容量上限 / LRU 淘汰（决定数据量可控，必要时人工删目录）。
- 分布式或多机共享（单机 MCP 进程本地盘即可）。
- 决定文书 PDF 本体缓存（仅结构化字段 + 摘要附图 PNG）。
- 商标/申请人等**可变**数据的 TTL 策略（不属于本 MCP 范围）。

---

## 11. 变更摘要（相对现 v2 契约）

| 项 | v2 现状 | 本要求 |
|----|---------|--------|
| `cache_file` | 可选、UIA 未写 | **成功必返**；指向持久契约 JSON |
| 缓存策略 | 无 | **先本地后线上** |
| TTL | — | **无** |
| 附图 | 每次抓 pss | **按申请号 PNG 持久化** |
| 入参 | — | 增加 `force_refresh` |
| 出参 | — | 增加 `from_cache`（可选 `cache_key`） |
