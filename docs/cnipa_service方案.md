# cnipa_service 方案（草案）

> 日期：2026-09-07。解决本机 Chrome+键鼠抢焦点问题：UIA 跑在专用机，对外只提供 HTTP 异步任务。

## 1. 目标

| 角色 | 做什么 |
|------|--------|
| **cnipa_service** | 部署在专用 Windows（`192.168.100.229`），串行执行国知局查询；HTTP 投递/查结果 |
| **复审无效技能** | 调 HTTP：创建任务 → 轮询结果；不暴露本机桌面自动化 |
| **cnipa-mcp** | 可选薄封装（开发用）；产品侧可不挂 Agent |

用户日常电脑不再被键鼠抢走。

## 2. 部署与库

| 项 | 值 |
|----|----|
| 应用机 | `192.168.100.229`（本机可用 `localhost`） |
| 代码目录 | `D:/cnipa_service`（本服务工程；原 `cnipa-mcp` 目录不动） |
| 附图磁盘根 | `D:/cnipa_file` |
| nginx | URL 前缀 `/cnipa_file` → `D:/cnipa_file`（已配置） |
| 库名 | `cnipa_db` |
| 账号 | `cnipauser` |
| 密码 | `cnipa$6_5@2027` |
| 存储 | **MySQL**（任务/去重/结果）；附图写 `D:/cnipa_file/...`，库只存 `rel_path`；调用方 URL=`{域名}/cnipa_file/{rel_path}` |

国知局账密仅保存在服务机本地（如 `account.ini`），不进业务库、不向最终用户索要（除非产品改为一人一号）。

## 3. HTTP 接口（概要）

鉴权：请求带 `tokenID`，校验方式对齐知识库服务（kblib）。

### 3.1 创建任务

- **决定检索（+ 详情 + 附图）**：filters（约 12 项）+ `library_type` 等 → 入队 → 返回 `task_id`
- **仅摘要附图**：`application_no` → 入队 → 返回 `task_id`

创建前按**规范化查询键**去重：

1. 已有相同键且 `status=3/4` → 直接返回该 `task_id`（可立即拉结果）
2. 已有相同键且 `status=1/2` → 返回已有 `task_id`（不新建）
3. 否则 INSERT 新任务

### 3.2 查询结果

- 按 `task_id` 查；或按与创建时相同的参数（算同一查询键）查
- 响应含 **`status`（数字）**；未完成则调用方间隔轮询

### 3.3 status 码

| status | 含义 |
|--------|------|
| 1 | 排队中 |
| 2 | 执行中 |
| 3 | 成功（有结果） |
| 4 | 成功但无命中 / 无附图 |
| 5 | 失败 |

## 4. 执行模型

- **单 worker 串行**消费队列（`status=1` → `2` → `3/4/5`），避免键鼠并行争抢
- 专用机无人值守；任务超时 → `status=5`
- 现有 UIA runner / `cnipa_cache` 沉到服务进程内复用

## 5. 技能侧用法

1. `POST` 创建任务（带 token、查询条件）
2. 轮询结果直到 `status ∈ {3,4,5}`
3. `status=3`：取 cases；附图用 `{域名}/cnipa_file{figure_path}` 下载到 `{work_dir}/figures/`
4. `status=4`：按无结果/无图处理；`5`：报错结束

技能源码：`C:\git\Skills\patent-reexam-invalid-report`；部署：`C:\Users\yg\.GeniusWork\skills`。

## 6. 附图静态目录（229 + nginx）

- 磁盘根：`D:/cnipa_file`；nginx：`/cnipa_file` → 该目录（已配置）
- 落盘建议：`D:/cnipa_file/{application_no}/figure.png`
- 库字段 `rel_path` 示例：`/200610004173.6/figure.png`（相对 `D:/cnipa_file`，**以 `/` 开头**，不要带 `/cnipa_file` 前缀）
- 调用方拼 URL：`{域名}/cnipa_file{rel_path}`
- **不存**本机绝对路径、不存完整 URL

## 7. 建表

见同目录 [cnipa_service_schema.sql](cnipa_service_schema.sql)。

## 8. 待细化（实施时）

- 具体 HTTP 路径与 JSON 字段（对齐现有契约 20 键）
- token 校验具体调用 kblib 哪段逻辑
- 对外域名
- 任务超时、重试次数、轮询建议间隔
