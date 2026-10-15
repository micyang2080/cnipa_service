# cnipa_service 设计文档

> 日期：2026-09-07。基于 [cnipa_service方案.md](cnipa_service方案.md)、[cnipa_service_schema.sql](cnipa_service_schema.sql)、[接口契约.md](接口契约.md)。审阅通过后再开发。

## 1. 目标与边界

**做什么**：在专用 Windows（`192.168.100.229`）上提供 HTTP 异步任务服务，串行跑国知局 UIA 查询；调用方只投递/轮询，不碰本机键鼠。

**不做**：
- 不改原 cnipa-mcp 目录职责（可另挂薄 MCP 调本服务，产品侧可不挂）
- 不向最终用户收国知局账密（账密仅服务机本地）
- 不做多 worker 并行（键鼠互斥）
- 不在库里存绝对路径或完整附图 URL

## 2. 架构

```
技能 / 调用方
    │  https://ipc.wanhuida.com/cnipa/... + tokenID
    ▼ nginx → :8572
cnipa_service (FastAPI)
    ├── API：创建任务 / 查结果（去重）
    ├── MySQL cnipa_db：task + figure
    ├── Worker（单进程串行）：status 1→2→3/4/5
    └── UIA runners（复用现有 python/）
            └── 附图落盘 D:/cnipa_file/{application_no}/figure.png
                    ▲
nginx /cnipa_file ──┘  调用方拼 URL
```

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| HTTP | Python FastAPI + uvicorn | 与 UIA/契约同语言，便于进程内复用 |
| 库 | MySQL `cnipa_db` | 见 schema |
| Worker | 同进程后台线程/协程轮询 `status=1` | 单实例部署；启动加 `SELECT FOR UPDATE` 抢任务 |
| 自动化 | 现有 `run_cnipa_*_uia.py` + `cnipa_cache` + `cnipa_contract` | 服务内直接调，不经 MCP/stdio |
| 鉴权 | 请求头/参数 `tokenID` | 复用 kblib 同一套校验与同一批 token |
| 静态图 | 磁盘 `D:/cnipa_file` + nginx `/cnipa_file` | 已配置 |
| 对外入口 | nginx → 本机 `8572` | 外网 `https://ipc.wanhuida.com/cnipa/...` |

进程模型：**一个服务进程 = HTTP + 一个 worker**。禁止多实例抢同一队列（无分布式锁方案）。

## 3. 任务类型与执行流水线

| task_type | 含义 | Worker 步骤 |
|-----------|------|-------------|
| 1 | 决定检索 + 详情 + 附图 | search → 对每条 item 拉 detail → 对每个 `application_no` 拉附图 → 写 `result_json` + `cnipa_task_figure` |
| 2 | 仅摘要附图 | 按 `application_no` 拉附图 → 写 figure 表；`result_json` 可为空对象或简要回显 |

缓存：沿用 `cnipa_cache`；入参 `force_refresh` 透传。命中缓存仍算成功完成任务（可少开站点）。

超时：任务 `started_at` 起超过 `TASK_TIMEOUT_SEC`（默认建议 1800）→ `status=5`，`error_code=TASK_TIMEOUT`。Worker 侧对单次 UIA 子调用设硬超时。

重试：UIA 瞬时失败默认不自动重试整任务（避免重复键鼠）；可选仅对登录失败重试 1 次。失败写 `error_code`/`error_message`（对齐契约错误码）。

## 4. 查询键（去重）

`query_key = SHA256(canonical_json)`，长度 64 hex → `CHAR(64)`。

规范化规则：
1. `task_type` 参与键
2. type=1：`library_type`（缺省 `invalid`）+ filters 12 键（空串省略或统一 `""`）+ `max_items`/`page`（缺省归一）+ `force_refresh`（`true`/`false`）
3. type=2：规范化后的 `application_no`（含点）+ `force_refresh`
4. JSON：键排序、UTF-8、无多余空白

创建去重（事务内）：

| 已有 | 行为 |
|------|------|
| 同 `query_key` 且 status∈{3,4} | 返回该 `task_id`，不新建 |
| 同 `query_key` 且 status∈{1,2} | 返回该 `task_id`，不新建 |
| 同 `query_key` 且 status=5 | **新建**（允许失败后重查） |
| `force_refresh=true` | **新建**（即使有 3/4）；旧成功任务保留 |

`force_refresh=true` 时 `query_key` 仍按上表算（含 refresh 标志），与缓存优先键区分，避免误命中旧成功任务。

## 5. HTTP API

| 层 | 地址 |
|----|------|
| 外网 | `https://ipc.wanhuida.com/cnipa`（nginx 反代） |
| 本机 | `0.0.0.0:8572`（uvicorn） |

例：`https://ipc.wanhuida.com/cnipa/v1/tasks/123456`  
鉴权：每个接口带 `tokenID`（header 或 query），复用 kblib 校验与同一批 token。  
应用路由前缀：`/cnipa`（与 nginx 对外路径一致；或 nginx strip 前缀后应用挂 `/v1/...`——实施时与 nginx 映射对齐，对外契约以 `/cnipa/v1/...` 为准）。

统一响应壳：

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

`status∈{3,4}` 时填 `result`；`5` 时填错误字段。对外响应**不再**返回顶层 `figures` / `items` / `details`。

### 5.1 `POST /cnipa/v1/tasks`

创建任务。

**type=1 body：**

```json
{
  "tokenID": "...",
  "task_type": 1,
  "library_type": "invalid",
  "filters": { "...12键..." },
  "max_items": 50,
  "page": 1,
  "force_refresh": false
}
```

**type=2 body：**

```json
{
  "tokenID": "...",
  "task_type": 2,
  "application_no": "200610004173.6",
  "force_refresh": false
}
```

校验：type=1 时 filters 至少一非空，否则直接 HTTP 200 + 业务失败形态或 400（建议与契约一致返回 `FILTER_EMPTY` 且不入队）。

响应：上节壳，至少含 `task_id` + `status`。

### 5.2 `GET /cnipa/v1/tasks/{task_id}`

按 id 查。

### 5.3 `POST /cnipa/v1/tasks/lookup`

用与创建相同参数算 `query_key` 查最新一条（任意 status）；无则 `ok=false`。

### 5.4 完成时 `result`（查询接口组装）

库内仍可存 runner 原始 `items`+`details`；**GET/lookup/去重命中** 时由服务组装为简洁结构（见 [cnipa_service接口文档.md](cnipa_service接口文档.md)）。

**type=1 `result`：**

```json
{
  "library_type": "invalid",
  "total": 1,
  "truncated": false,
  "cases": [
    {
      "application_no": "200610004173.6",
      "case_no": "4W107577",
      "issue_date": "2018.12.28",
      "decision_date": "2018.12.21",
      "figure_path": "/200610004173.6/figure.png",
      "…其余固定键…"
    }
  ]
}
```

- `cases[]`：列表 + 详情合并；`issue_date`=发文日，`decision_date`=决定日  
- `figure_path`：相对 `D:/cnipa_file`，**以 `/` 开头**；无图 `""`；URL=`{域名}/cnipa_file{figure_path}`  
- 一申请号至多一张摘要附图；`cnipa_task_figure.rel_path` 同规则

**type=2 `result`：**

```json
{
  "application_no": "200610004173.6",
  "has_figure": 1,
  "figure_path": "/200610004173.6/figure.png"
}
```

检索 0 条：`status=4`，`result.cases=[]`。

技能轮询建议间隔：2～5s；上限由技能侧控制。

## 6. 数据与文件

表结构以 [cnipa_service_schema.sql](cnipa_service_schema.sql) 为准。

| 落盘 | 规则 |
|------|------|
| 根 | `D:/cnipa_file` |
| 路径 | `{application_no}/figure.png`（磁盘） |
| 库 | 只存 `rel_path`，**以 `/` 开头**，如 `/200610004173.6/figure.png` |
| URL | `{域名}/cnipa_file{rel_path}` |

Worker 写图：先写临时文件再 rename；同一申请号可覆盖。

本地 `cnipa_cache` 文件缓存可继续放原目录；与 MySQL 任务结果并存：任务表是对外真相，文件缓存加速 UIA。

## 7. 配置

| 项 | 来源 | 示例 |
|----|------|------|
| DB | env / ini | `192.168.100.229:6192` / `cnipauser` / `cnipa_db` |
| `CNIPA_FILE_ROOT` | env | `D:/cnipa_file` |
| `LISTEN` | env | `0.0.0.0:8572` |
| `TASK_TIMEOUT_SEC` | env | `1800` |
| 国知局账密 | 服务机 `account.ini` | 不入库 |
| token 校验 | kblib 同套 | 同一批 token |

密码等密钥不进仓库、不进 `result_json`。

## 8. 目录规划（本仓库）

```
D:/cnipa_service/
  docs/           # 方案/设计/契约/schema
  python/         # 现有 UIA + contract + cache（复用）
  service/        # 新建：api / worker / db / auth / query_key
  scripts/        # 启停、建库
  requirements 增 fastapi uvicorn pymysql 等
```

原 MCP `src/` 可保留不动；可选后续加「薄 MCP → HTTP」。

## 9. 技能侧（本期不做）

本期只交付 HTTP 服务。技能改 HTTP 轮询列为二期。

源码：`C:\git\Skills\patent-reexam-invalid-report`；部署：`C:\Users\yg\.GeniusWork\skills`。

## 10. 实施顺序

1. 建库执行 schema；确认 nginx `/cnipa_file` 与 `/cnipa` → `8572`
2. `service/`：DB、query_key、创建去重、查任务 API（可先 mock worker）
3. Worker：串行调现有 UIA；写 result_json（内部 items+details）+ figure 表；查询时组装 `cases`
4. token 鉴权对接 kblib
5. 真机：type=1 / type=2；去重、status=5 重建、force_refresh

## 11. 风险

| 风险 | 应对 |
|------|------|
| 桌面无查询页/未登录 | Worker 自动启动 Chrome + `account.ini` 登录（无人值守） |
| 桌面会话被锁/断 | 专用机保持登录桌面；失败 `status=5` |
| 任务卡死在 2 | 超时扫描把过期 2→5 |
| 多开服务抢任务 | 部署约定单实例；可选 DB 行锁 |
| 契约字段漂移 | result 经 `cnipa_contract` 投影后再入库 |
| 附图与决定不同站点 | type=1 流水线内顺序执行，总时长可能接近超时上限 |

## 12. 已确认

| # | 结论 |
|---|------|
| 1 | 本机端口 **8572**；外网 `https://ipc.wanhuida.com/cnipa/...`（nginx 反代） |
| 2 | `tokenID` 复用 kblib 同一套校验与同一批 token |
| 3 | type=1 查询返回 `result.cases`（列表+详情+figure_path 合并） |
| 4 | status=5 允许同键直接重建 |
| 5 | 本期不包含技能改造 |
