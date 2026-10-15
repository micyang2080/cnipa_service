# cnipa_service 接口文档（技能调用方）

> 基址（外网）：`https://ipc.wanhuida.com/cnipa`  
> 本机直连：`http://192.168.100.229:8572/cnipa`  
> 鉴权：与知识库相同，请求带 `tokenID`（Header / Query / JSON body 任一）。

## 调用流程

```
1. POST /v1/tasks          创建（或命中去重返回已有 task_id）
2. GET  /v1/tasks/{id}     轮询，建议间隔 2～5s
3. status ∈ {3,4,5} 结束
   - 3：取 `result.cases`（含 figure_path、decision_file_url）
   - 4：无命中 / 无附图
   - 5：失败，看 error_code / error_message
```

附图下载：`https://ipc.wanhuida.com/cnipa_file{figure_path}`  
例：`figure_path=/200610004173.6/figure.png` → `https://ipc.wanhuida.com/cnipa_file/200610004173.6/figure.png`  

决定书 PDF：`https://ipc.wanhuida.com{decision_file_url}`  
例：`decision_file_url=/cnipa_file/200610004173.6/4W107577.pdf`

## 鉴权

| 方式 | 示例 |
|------|------|
| Header | `tokenID: <token>` 或 `X-Token-ID: <token>` 或 `Authorization: Bearer <token>` |
| Query | `?tokenID=<token>` |
| Body | `"tokenID": "<token>"` |

失败：HTTP `401`，`{"code":4010,"msg":"..."}`。

## 统一响应壳

```json
{
  "ok": true,
  "task_id": 123,
  "status": 1,
  "task_type": 1,
  "progress": null,
  "error_code": null,
  "error_message": null,
  "result": null
}
```

| status | 含义 |
|--------|------|
| 1 | 排队中 |
| 2 | 执行中 |
| 3 | 成功（有结果） |
| 4 | 成功但无命中 / 无附图 |
| 5 | 失败 |

`status∈{3,4}` 时填 `result`；`5` 时看 `error_code` / `error_message`。

## 1. 创建任务

`POST /v1/tasks`  
完整 URL：`https://ipc.wanhuida.com/cnipa/v1/tasks`

### task_type=1 决定检索 + 详情 + 附图

```json
{
  "tokenID": "<token>",
  "task_type": 1,
  "library_type": "invalid",
  "filters": {
    "application_no": "200610004173.6",
    "invention_title": "",
    "patent_type": "",
    "ipc_class": "",
    "decision_no": "",
    "case_no": "",
    "patentee": "",
    "petitioner": "",
    "panel_leader": "",
    "panel_examiner": "",
    "panel_member": "",
    "agency": ""
  },
  "max_items": 10,
  "page": 1,
  "force_refresh": false
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `task_type` | 是 | `1` |
| `library_type` | 否 | `invalid`（默认）/ `reexam` |
| `filters` | 是 | 12 键，**至少 1 个非空** |
| `max_items` | 否 | 默认 10，上限 10；站点命中更多时 `truncated=true`，只处理前 10 |
| `page` | 否 | 默认 1 |
| `force_refresh` | 否 | `true` 强制新建并重查 |

`filters` 全空：HTTP 200，`ok=false`，`error_code=FILTER_EMPTY`，不入队。

### task_type=2 仅摘要附图

```json
{
  "tokenID": "<token>",
  "task_type": 2,
  "application_no": "200610004173.6",
  "force_refresh": false
}
```

### 去重

相同规范化查询键：

| 已有 status | 行为 |
|-------------|------|
| 1 / 2 / 3 / 4 | 返回已有 `task_id`，不新建 |
| 5 | 允许新建 |
| `force_refresh=true` | **必定新建**（跳过去重）；UIA 侧跳过文件缓存 |

## 2. 查询任务

`GET /v1/tasks/{task_id}`  
例：`https://ipc.wanhuida.com/cnipa/v1/tasks/123456`

也可 `?tokenID=`。

完成时示例（type=1）：

```json
{
  "ok": true,
  "task_id": 9,
  "status": 3,
  "task_type": 1,
  "progress": null,
  "error_code": null,
  "error_message": null,
  "result": {
    "library_type": "invalid",
    "total": 1,
    "truncated": false,
    "cases": [ { "…见下节固定键…", "figure_path": "/200610004173.6/figure.png" } ]
  }
}
```

**相对旧版**：不再返回顶层 `figures`，也不再返回 `result.items` / `result.details`。

## 3. 按条件查找

`POST /v1/tasks/lookup`  
Body 与创建相同（含 `task_type` 与查询字段），返回该查询键最新一条（含失败）。无则 `404`。

## 完成时 `result`（查询接口组装，不改库内原始 JSON）

### type=1

```json
{
  "library_type": "invalid",
  "total": 1,
  "truncated": false,
  "cases": [
    {
      "source": "cnipa",
      "review_type": 2,
      "case_no": "4W107577",
      "decision_no": "38430",
      "issue_date": "2018.12.28",
      "decision_date": "2018.12.21",
      "invention_title": "…",
      "petitioner": "李瑶",
      "patentee": "…",
      "application_no": "200610004173.6",
      "request_date": "2018.07.24",
      "legal_basis": "…",
      "decision_summary": "…",
      "decision_result": "…",
      "panel_members": "…",
      "figure_path": "/200610004173.6/figure.png",
      "detail_url": "https://…",
      "patent_type": "发明专利",
      "ipc_class": "C07C229/24",
      "agency": "",
      "decision_file_url": "/cnipa_file/200610004173.6/4W107577.pdf"
    }
  ]
}
```

- 列表 + 详情已合并为 `cases[]`（不再返回 items/details/figures）
- `issue_date`=列表发文日；`decision_date`=详情决定日
- `figure_path`=相对 `D:/cnipa_file` 且**以 `/` 开头**，无图为 `""`；下载 `{域名}/cnipa_file{figure_path}`
- `decision_file_url`=决定书 PDF（含 `/cnipa_file` 前缀），落盘 `D:/cnipa_file/{申请号}/{案号}.pdf`；失败为空串，不整单失败；打开 `{域名}{decision_file_url}`

### type=2

```json
{
  "application_no": "200610004173.6",
  "has_figure": 1,
  "figure_path": "/200610004173.6/figure.png"
}
```

## 技能侧伪代码

```
r = POST /cnipa/v1/tasks  body
if not r.ok and r.error_code: 按错误结束
id = r.task_id
loop:
  r = GET /cnipa/v1/tasks/{id}
  if r.status in (1, 2): sleep 2~5; continue
  if r.status == 3:
    for c in r.result.cases:
      ingest(c)
      if c.figure_path: 下载 {域名}/cnipa_file{c.figure_path}
  if r.status == 4: 按无结果/无图处理
  if r.status == 5: 报错 r.error_code / r.error_message
  break
```

## 取消任务

`POST /cnipa/v1/tasks/{task_id}/cancel`  
将排队/执行中任务标为失败（`CANCELLED`），并杀掉当前 UIA 子进程（停止抢鼠标）。

`GET /cnipa/health` → `{"ok": true}`（无需 token）

`GET /cnipa/v1/env`（需 token）→ 调 UIA 状态检查，看 Chrome / a11y / cpquery / 登录是否就绪：

```json
{
  "ok": true,
  "env": {
    "success": true,
    "ready": true,
    "chrome_found": true,
    "a11y_ok": true,
    "logged_in": true,
    "message": "..."
  }
}
```

`ready=false` 时：调用方仍可直接创建任务。Worker 会无人值守准备环境（自动启动 Chrome + 读 `account.ini` 登录），无需人工先开查询页。
