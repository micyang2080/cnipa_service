# cnipa-patent-review-mcp

国知局（CNIPA）MCP：**纯检索层** — SSO 登录、cpquery 复审/无效决定查询、pss 摘要附图。

与 `iphouse-mcp`（知产宝）并列；**复审无效检索报告**技能 `patent-reexam-invalid-report` 编排调用。

## 平台要求（必读）

**仅支持 Windows。** 现行实现依赖 pywinauto(UIA) + 桌面键鼠驱动本机 Chrome，macOS／Linux **不可用**。

非 Windows 上任意工具调用会立刻返回契约错误（不启动 Python）：

```json
{
  "success": false,
  "error": "UNSUPPORTED_OS",
  "message": "当前操作系统为 darwin，cnipa-mcp 仅支持 Windows（依赖 pywinauto/UIA 桌面自动化）",
  "hint": "请在 Windows 上的 GeniusWork 中使用本 MCP；macOS/Linux 暂不支持"
}
```

## 架构边界

| 本 MCP | **不属于**本 MCP |
|--------|------------------|
| login / search / detail / 附图 | Word 报告生成 |
| 列表与详情 JSON 落盘 | bootstrap / ingest / check_complete |

## 工具

| 工具 | 作用 |
|------|------|
| `cnipa_login` | SSO 登录 + 滑块；持久化 `storage_state` |
| `cnipa_search_decisions` | cpquery 列表检索（无效/复审库 + 筛选字段） |
| `cnipa_fetch_decision_detail` | 单条详情 + `detail_url` |
| `cnipa_fetch_abstract_figure` | pss 按 `CN{申请号}` 取摘要附图 |

## 安装

```bash
cd C:\git\Mcps\cnipa-mcp
npm install
npm run build

cd python
pip install -r requirements.txt
playwright install chromium
```

## 环境变量

v10 起默认值即为正确配置：**有头浏览器 + Playwright 自带 Chromium 145 + 原生 UA + 零注入脚本**，无需设置任何变量即可自动登录（含滑块自动通过）。可选变量仅用于覆盖：

| 变量 | 说明 |
|------|------|
| `CNIPA_CREDENTIALS_PATH` | `cnipa_credentials.json` 路径 |
| `CNIPA_SESSION_DIR` | Playwright session 目录 |
| `CNIPA_ARTIFACT_ROOT` | 未设 session_dir 时默认 `{root}/cnipa_session` |
| `CNIPA_CDP_URL` | （v10 前的兜底手段）接管已登录 Chrome，如 `http://127.0.0.1:9222` |
| `CNIPA_USER_DATA_DIR` | 持久化 Chrome 配置目录（默认 `{session_dir}/chrome_profile`） |
| `CNIPA_PERSISTENT_PROFILE` | `1`=用持久化配置（默认），`0`=每次临时浏览器 |
| `CNIPA_CONNECTOR_ROOT` | 覆盖 Python 脚本目录 |
| `CNIPA_DEFAULT_TIMEOUT_MS` | 默认超时（毫秒） |
| `CNIPA_DEBUG_DUMP` | `1`=每步落盘截图/HTML/元素清单到 `output/debug_steps/`（默认开）；`0`=关闭 |
| `CNIPA_SSO_COOLDOWN_SEC` | 同一 session 文件两次 SSO 登录的最小间隔（默认 600 秒） |
| `GENIUSWORK_PYTHON_ROOT` | 指定 Python 可执行文件目录 |

凭证格式（与技能包一致）：

```json
{
  "login_type": "natural",
  "username": "手机号",
  "password": "密码"
}
```

## Cursor / GeniusWork MCP 配置

**本地开发**：

- 命令：`node`
- 参数：`["C:/git/Mcps/cnipa-mcp/build/index.js"]`
- 环境变量：`CNIPA_CREDENTIALS_PATH=...`（其余均可省略，v10 默认即为正确配置）

### CDP 接管手动登录（仅兜底，v10 后通常不需要）

v10 已实现全自动登录（有头 + 原生指纹 + 滑块自动通过），优先直接 `cnipa_login`。
仅在自动登录被风控且需人工拖滑块救场时，用调试端口接管：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="C:\git\Skills\patent-reexam-invalid-report\secrets\cnipa_chrome"
```

在该窗口完成 cpquery 登录，然后设 `CNIPA_CDP_URL=http://127.0.0.1:9222`，直接调 `cnipa_search_decisions`，**无需 cnipa_login**。

## 测试

```bash
npm run test:smoke          # MCP 工具清单（离线）
cd python && python -X utf8 offline_selftest.py   # 纯函数离线自测（不联网）
```

## 相关

- 需求：`C:/git/Skills/patent-reexam-invalid-report/REQUIREMENTS.md` §6
- 知产宝：`../iphouse-mcp`（patentreview 模糊检索）
