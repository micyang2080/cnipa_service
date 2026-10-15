# cnipa-mcp · 技能侧契约要求（patent-reexam-invalid-report）

> **面向**：`C:\git\Mcps\cnipa-mcp` 开发  
> **对照**：MCP 现有 `docs/接口契约.md`（v1 草案）  
> **技能**：`patent-reexam-invalid-report` · case schema 见 `schema/reexam_invalid_case.schema.json`  
> **日期**：2026-09-03

---

## 0. 统一原则

1. **对外 case 对象键名与技能 schema 完全一致**（见 §4），技能 `ingest_cnipa_decision_result.py` **零映射**。
2. **日期统一**：业务字段一律 `YYYY.MM.DD`（如 `2018.12.21`）；站点原始 `20181221` 仅放 `raw`（若保留）。
3. **专利号统一**：字段名 `application_no`，**13 位含校验位点**（如 `200610004173.6`）；**禁止**对外主键用无点 `app_no`。
4. **请求人统一**：不论复审/无效，对外一律 `petitioner`（Word 模板列名仍是「无效请求人」，内容由该字段填）。
5. **每条 case 必带 `detail_url`**（摘要表「查看文件」超链）。
6. **列表 + 详情并集 = 报告 12 列**；列表能给的先在 `items[]` 给全，详情只做增强/补空。

---

## 1. `cnipa_search_decisions` · 检索入参（请扩展）

### 1.1 顶层

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `library_type` | `"invalid" \| "reexam"` | 是 | 无效决定库 / 复审决定库 |
| `filters` | object | 是 | 至少 1 个非空条件 |
| `max_items` | int | 否 | 默认 50，上限 500 |
| `page` | int | 否 | 从 1 起；或内部自动翻页至 `max_items` |

### 1.2 `filters`（对齐 cpquery「展开更多搜索项」，**全部支持**）

| 键名（定稿） | 类型 | 站点含义 | 备注 |
|--------------|------|----------|------|
| `application_no` | string | 申请号/专利号 | 含/不含点、CN 前缀均接受，MCP 规范化 |
| `ipc_class` | string | 分类号 | 如 `A61K31/137` |
| `decision_no` | string | 决定号 | 如 `38430` |
| `case_no` | string | 案件编号 | 如 `4W107577` |
| `invention_title` | string | 发明名称 | 支持模糊 |
| `patent_type` | string | 专利类型 | 见 §1.3 枚举 |
| `patentee` | string | 专利权人 | 无效库常用 |
| `petitioner` | string | 请求人 | 无效=无效宣告请求人；复审=复审请求人 |
| `panel_leader` | string | 合议组组长 | |
| `panel_examiner` | string | 主审员 | |
| `panel_member` | string | 参审员 | |
| `agency` | string | 代理机构 | |

### 1.3 可选枚举

```json
"patent_type": "invention" | "utility_model" | "design"
```

MCP 内部映射到站点中文选项（发明专利 / 实用新型 / 外观设计）。

### 1.4 组合规则

- 多条件 = 站点 **AND**（与国知局 UI 一致）。
- `application_no` 与 `case_no` 同时给：按站点 AND；若站点不支持 AND，在 `hint` 说明并优先 `application_no`。
- `filters` 全空 → 错误码 `FILTER_EMPTY`（见 §8）。

---

## 2. `cnipa_search_decisions` · 返回 `items[]`

每条 **直接可 ingest** 为 case（缺字段可 `""`，**键必须存在**）：

```jsonc
{
  "success": true,
  "library_type": "invalid",
  "total": 1,
  "truncated": false,
  "cache_file": "C:\\…\\optional.json",   // 可选：大结果落盘路径
  "items": [
    {
      "source": "cnipa",
      "review_type": 2,
      "case_no": "4W107577",
      "decision_no": "38430",
      "issue_date": "2018.12.21",
      "decision_date": "",
      "invention_title": "……",
      "petitioner": "李瑶",
      "patentee": "远大医药（天津）有限公司",
      "application_no": "200610004173.6",
      "request_date": "",
      "legal_basis": "",
      "decision_summary": "",
      "decision_result": "维持专利权有效",
      "panel_members": "",
      "figure_path": "",
      "detail_url": "https://cpquery.cponline.cnipa.gov.cn/detail/index?zhuanlisqh=…&anjianbh=4W107577&searchType=8",
      "patent_type": "发明专利",
      "ipc_class": "A61K31/137",
      "agency": ""
    }
  ]
}
```

### 2.1 字段说明

| 键 | 说明 |
|----|------|
| `review_type` | `1`=复审，`2`=无效 |
| `issue_date` | 卡片「发文日」；**不是**决定日 |
| `decision_date` | 列表通常无，留空；详情补 |
| `application_no` | **含点**；search → detail → figure 的连接键 |

### 2.2 废弃（勿再对外暴露）

| 旧键 | 改为 |
|------|------|
| `app_no` | `application_no`（含点） |
| `title` | `invention_title` |
| `conclusion` | `decision_result` |
| `invalid_requester` | `petitioner` |
| `reexam_requester` | `petitioner` |

---

## 3. `cnipa_fetch_decision_detail` · 入参 / 返回

### 3.1 入参

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `application_no` | string | 建议 | 对拍校验 |
| `case_no` | string | 建议 | 页内导航入口 |
| `detail_url` | string | 可选 | 有则直接打开 |
| `list_item` | object | 可选 | search 的 item 原样传入；卡片精确值优先于 OCR |

### 3.2 返回 `detail`

```jsonc
{
  "success": true,
  "detail": {
    "source": "cnipa",
    "review_type": 2,
    "case_no": "4W107577",
    "decision_no": "38430",
    "decision_date": "2018.12.21",
    "invention_title": "……",
    "petitioner": "李瑶",
    "patentee": "远大医药（天津）有限公司",
    "application_no": "200610004173.6",
    "request_date": "2018.07.24",
    "legal_basis": "专利法第22条第2款，第22条第3款",
    "decision_summary": "……决定要点全文……",
    "decision_result": "维持专利权有效",
    "panel_members": "任晓兰（组长）刘婷婷（主审）刘静（参审）",
    "figure_path": "",
    "detail_url": "https://cpquery.cponline.cnipa.gov.cn/detail/index?…"
  }
}
```

- `decision_date`：决定书落款日；缺则回填 `issue_date`。
- `panel_members`：单行或 `\n` 分行均可。

---

## 4. 技能 case 对象（权威键名）

与 `schema/reexam_invalid_case.schema.json` 一致：

| 键 | 报告列 | 必填（ingest） |
|----|--------|----------------|
| `source` | — | `"cnipa"` |
| `review_type` | — | 建议 |
| `case_no` | 案件编号 | 是 |
| `decision_date` | 决定日 | 建议 |
| `invention_title` | 发明名称 | 是 |
| `petitioner` | 无效请求人 | 建议 |
| `patentee` | 专利权人 | 建议 |
| `application_no` | 专利号 | 是 |
| `request_date` | 无效请求日 | 建议 |
| `legal_basis` | 法律依据 | 建议 |
| `decision_summary` | 决定要点 | 建议 |
| `decision_result` | 审查结论 | 是 |
| `panel_members` | 合议组成员 | 建议 |
| `figure_path` | 附图 | 附图工具写入 |
| `detail_url` | 查看文件 | 是 |

扩展（报告不展示，可保留）：`decision_no`、`issue_date`、`patent_type`、`ipc_class`、`agency`。

---

## 5. `cnipa_fetch_abstract_figure` · 附图（用法 A/B 公用）

### 5.1 入参

| 参数 | 说明 |
|------|------|
| `application_no` | **含点** `200610004173.6`；CN/ZL/无点可接受，MCP 规范化，**回显含点** |
| `save_path` | 建议绝对路径：`{work_dir}/artifact/reexam-runs/{run_id}/figures/{application_no}.png` |

**无前置依赖**（用法 B：知产宝已有 case，只补图）。

### 5.2 返回（成功）

```jsonc
{
  "success": true,
  "application_no": "200610004173.6",
  "figure_path": "C:\\…\\200610004173.6.png",
  "method": "saveas"
}
```

### 5.3 失败

```jsonc
{
  "success": false,
  "error": "NO_FIGURE",
  "message": "详情页未检测到摘要附图区域",
  "hint": "该案件可能无附图，调用方按缺图处理"
}
```

### 5.4 用法 B 接线（知产宝 + 国知局附图）

```
对每个 case（application_no 来自知产宝）：
  ① cnipa_fetch_abstract_figure({ application_no, save_path })
  ② 成功 → 收集 { "200610004173.6": figure_path }
  ③ NO_FIGURE → 缺图容错
  ④ fetch_abstract_figures.py --figure-map <json文件>
```

---

## 6. 两种典型用法

```
用法 A（国知局一站式）：
  cnipa_search_decisions(filters…)
    → items[]
    → cnipa_fetch_decision_detail(list_item=item)  [可选增强]
    → cnipa_fetch_abstract_figure(application_no)
    → ingest_cnipa_decision_result.py

用法 B（知产宝决定 + 国知局附图）：
  iphouse patentreview → ingest_patent_review_result.py
    → 逐条 cnipa_fetch_abstract_figure
    → fetch_abstract_figures.py --figure-map …
    → make_reexam_invalid_report.py
```

---

## 7. 与知产宝字段对齐

| 技能键 | 知产宝 `parsed` | cnipa（定稿） |
|--------|-----------------|---------------|
| `case_no` | `decision_id` | `case_no` |
| `decision_date` | `decision_date` | `decision_date` |
| `invention_title` | `patent_name` | `invention_title` |
| `petitioner` | `invalidation_petitioner` / `reexam_petitioner` | `petitioner` |
| `patentee` | `patentee` | `patentee` |
| `application_no` | `application_number` | `application_no` |
| `request_date` | `request_date` | `request_date` |
| `legal_basis` | `legal_basis` | `legal_basis` |
| `decision_summary` | `decision_points` | `decision_summary` |
| `decision_result` | `decision_result` | `decision_result` |
| `panel_members` | `panel_members` | `panel_members` |
| `detail_url` | `detail_url` | `detail_url` |
| `figure_path` | —（走 cnipa） | `figure_path` |

---

## 8. 错误码（在现有基础上补充）

| 码 | 场景 |
|----|------|
| `FILTER_EMPTY` | `filters` 全空 |
| `TOO_MANY_RESULTS` | 超过 `max_items`；payload 带 `truncated: true` |
| `DETAIL_URL_MISSING` | 列表命中但无法构造 `detail_url` |

保留现有：`WINDOW_NOT_FOUND`、`A11Y_DISABLED`、`NEED_CREDENTIALS`、`CAPTCHA_FAIL`、`AUTH_REJECTED`、`NOT_LOGGED_IN`、`NO_RESULT`、`NO_FIGURE`、`PARSE_FAIL`、`TIMEOUT`、`INVALID_INPUT`、`RUNTIME_FAIL`。

---

## 9. MCP 改完后的验收（技能侧）

1. `filters` 12 项均可单独或组合检索（对齐 cpquery 展开项）。
2. `items[]` / `detail` 键名与 §4 一致，`application_no` 含点。
3. 回归：`200610004173.6` / `4W107577` → search + detail + figure 全链路。
4. 用法 B：知产宝 fixture + 仅 `cnipa_fetch_abstract_figure` → `fetch_abstract_figures.py --figure-map` → Word 附图列有图或空（`NO_FIGURE` 不阻断）。

---

## 10. 变更摘要（相对现 `接口契约.md`）

| 项 | 现草案 | 技能要求 |
|----|--------|----------|
| 检索 filters | 仅 `application_no`、`case_no` | **12 项**（§1.2） |
| 列表键名 | `app_no`、`title`、`conclusion` 等 | **§2 定稿键名** |
| 请求人 | `invalid_requester` / `reexam_requester` | 统一 **`petitioner`** |
| 专利号 | 无点 `app_no` | **`application_no` 含点** |
| 日期 | `20181221` | **`YYYY.MM.DD`** |
| detail | 12 列键已基本对齐 | 补 `source`、`review_type`、`detail_url`、`figure_path` |
