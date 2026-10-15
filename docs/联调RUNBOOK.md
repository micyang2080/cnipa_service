# 联调 RUNBOOK（2026-09-01 · 用户执行）

> 目标：一次跑通「登录复用 → 查询 → 详情 → 附图」全链路。
> **铁律**：每个命令**只跑一次**。失败**不要重跑**——把报错 + `debug_dir` 交给 Agent 离线修（每步都已落盘截图/HTML/元素清单，离线足够定位）。

## 0. 命令形式（重要 · PowerShell 直接可用）

**输入文件直接做参数**，不用 `<` 重定向（PowerShell 不支持 `<`，会报"为将来使用而保留"）：

```
python -X utf8 脚本名.py test_inputs\XX_*.json
```

该形式在 PowerShell / cmd / git-bash 下均可用；`|` 管道 tee 日志不受影响。MCP 内部走 stdin，不受影响。

## 0.5 已就绪（无需再做）

- v10 自动登录配置（有头 + 自带 Chromium 145 + 原生 UA + 零注入），昨晚已实测全链路成功（output4.txt）
- 查询脚本已支持 12 个筛选字段（申请号/分类号/决定号/案件编号/发明名称/专利类型/专利权人/请求人/合议组组长/主审/参审/代理机构）
- 每步 dump：`python/output/debug_steps/{脚本名}_{时间戳}/`，PNG + HTML + elements.txt 三件套
- 离线校验全绿：`py_compile` 5 个脚本通过、`offline_selftest.py` 53/53 PASS、`npm run build` + `test:smoke` 通过
- 输入文件已备好：`python/test_inputs/01~05_*.json`（样例：申请号 `200610004173.6`，案件编号 `4W107577`）

## 1. 登录/会话复用（最有价值的第一跑）

```bash
cd C:\git\Mcps\cnipa-mcp\python
python -X utf8 run_cnipa_login_pw.py test_inputs\01_login.json
```

预期：JSON `"success": true`。若 cookie 仍有效则秒过；若失效会自动走 SSO + 滑块（昨晚已自动通过）。
看日志关键字：`[login] cpquery 登录成功`、`session_valid: true`。

## 2. 列表检索 · 按申请号（核心联调步）

```bash
python -X utf8 run_cnipa_search_decisions_pw.py test_inputs\02_search_by_application_no.json
```

预期 JSON 字段：

| 字段 | 期望 |
|------|------|
| `results_status` | `found` |
| `filter_report` | `application_no: {ok: true, how: "label-input:申请号"}` 之类 |
| `items[0]` | 案件编号 `4W107577`、申请号 `200610004173.6`、含 `detail_url` |
| `total_text` | `共 N 条` |
| `table_headers` | 非空（列表表头） |

stderr 日志看：`[library] 选库 … 已验证选中`、`[filters] application_no=… → OK`。
**结果另存**：payload 里 `cache_file` 指向完整 JSON（含 raw_row）。

## 3. 列表检索 · 按案件编号（验证第二字段，可选）

```bash
python -X utf8 run_cnipa_search_decisions_pw.py test_inputs\03_search_by_case_no.json
```

预期同上，`filter_report` 里 `case_no: ok`。若第 2 步已 found，此步只为多验一个字段，可不跑。

## 4. 详情抓取（12 列核对）

先把第 2 步 `items[0].detail_url` 粘进 `test_inputs\04_detail.json` 的 `detail_url`（留空则用默认 `zhuanlisqh=200610004173&anjianbh=4W107577`），然后：

```bash
python -X utf8 run_cnipa_fetch_decision_detail_pw.py test_inputs\04_detail.json
```

预期：`summary` 含「12 列齐」；`missing_fields: []`。
若缺列：日志 `[detail] 报告列缺失 N 项: [...]`，凭 dump `01_detail_loaded` 的 elements.txt 离线补映射即可，**不用重跑**（`raw_fields` 已带全部原始键值对）。

## 5. pss 摘要附图

```bash
python -X utf8 run_cnipa_fetch_abstract_figure_pw.py test_inputs\05_figure.json
```

预期：`figure_url` 非空、`figure_path` 指向 `output\figures\CN200610004173.6.png`（图片可打开）。
注意脚本会自动 `CN` 前缀（搜 `CN200610004173.6`），先勾「记住我的选择」再点「同意」。
若定位失败：`img_candidates` 已列出页面全部图片 URL，离线比对 03 步 dump 修选择器。

## 6. MCP 挂载验证（可选，最后做）

在 Cursor/GeniusWork 重挂 MCP（`node C:/git/Mcps/cnipa-mcp/build/index.js`），调一次 `cnipa_search_decisions`，参数同 `02_search_by_application_no.json`。

## 失败协议（重要）

1. **不重跑**。任何一步失败 → 停。
2. 收集三样：该步 stderr 日志、payload JSON（失败时也有 `debug_dir`）、`debug_steps/{脚本名}_{时间戳}/` 目录。
3. 发给 Agent：报错行 + dump 目录路径即可（dump 含整页 HTML 与可见元素清单，离线可修 90% 的选择器问题）。

## dump 文件名速查

| 文件 | 含义 |
|------|------|
| `01_pss_loaded` / `01_detail_loaded` / `01_after_login` | 页面已载入（此时渲染失败=瑞数壳，看 PNG） |
| `02_tab_selected` / `02_on_search_page` / `03_library_selected` | 导航/选库后 |
| `04_expanded_all_fields` | 已点「展开更多搜索项」——**所有筛选字段的 DOM 在这** |
| `05_filters_filled` | 填完条件（截图可看哪些框没填上） |
| `06_results_found / empty / timeout` | 查询后三种状态 |
| `FAIL_wrong_tab_not_search_page` | **查错页熔断**：未确认进入复审无效检索页（决定号/案件编号字段未出现）时拒绝提交查询，快速失败留现场（2026-09-01 修复：旧探测误判导致在「案件查询」tab 查错库返回共0条） |
| `FAIL_search_not_fired` | **网络门禁熔断**：点「查询」后 8s 内未见任何 xhr/fetch——按钮没点中（2026-09-01 修复：旧子串匹配误点页头「中国专利审查信息查询」按钮，页面没动却被默认「共0条」空态骗成 empty） |
| `FAIL_*` / `*_main.error.txt` | 失败现场 |

### 2026-09-01 手工对拍结论（seach1/result1 截图 OCR）

手工输入 `200610004173.6` 点查询 → 输入框变 `2006100041736`（**站点只去小数点，13 位不变**）→ 共检索到1条结果。而 run5 自动化 POST 的 `zhuanlisqh` 与之**逐字相同**仍显示默认空态「共0条」→ 高度怀疑响应被拦/出错，页面未刷新。据此改造：

- 申请号改**真实键盘逐字键入 + Tab 失焦**（`_type_like_human`），让站点自己去点；`filter_report.application_no.final` 记录输入框最终值；站点 mask 没跑则兜底填本地规范化值。
- `wait_search_results`/`extract_total_text` 补「共检索到N条结果」文案（N≥1=found）。
- 点查询前加 1s 拟人停顿。
- payload 新增 `search_api_status` / `search_api_resp_body`。重跑 02 后按下表判：

| 现象 | 结论 |
| --- | --- |
| `共检索到1条` | 成功，进入解析联调 |
| `共0条` + resp_body 是 HTML/4xx/5xx | 请求被瑞数/风控拦 → 换拟人策略 |
| `共0条` + resp_body JSON `total:0` | 服务端真判 0（对比 Cookie/头差异） |
| `共检索到0条` | 真 0 结果（条件太窄） |

### 2026-09-01 15:01 复盘：点击已确认发生，服务端回 HTTP 400

run6 payload（cnipa_299058d9e296）：`search_api_status=400`、响应体空、`filter_report` 键入路径完美（原样键入 `200610004173.6` → 输入框最终 `2006100041736`，站点自己去点）、页面无错误 toast、停在默认共0条。**请求发出✓ 值正确✓ 服务端拒✗**。console.log 看不到日志是 PowerShell tee 陈旧问题，以 payload/debug_steps 为准。

判别实验（02b `manual_click: true`）：脚本不自动点查询，改由**人在自动化浏览器窗口里手工点**（门禁 2.5 分钟）——同一浏览器同一会话，唯一变量是“合成点击 vs 人手点击”。同时全程采集所有 `/api/` 响应状态（`api_calls`）与搜索响应头（`search_api_resp_headers`）：

| 02b 结果 | 结论 → 下一步 |
| --- | --- |
| 手工点击 → 200 + 数据 | 合成点击被识别 → 加拟人化（鼠标轨迹/事件序列） |
| 手工点击 → 仍 400 | 自动化浏览器档案/会话被标记 → 重建 profile / `force_login` 重登 |
| `api_calls` 其它 api 均 200、仅 search 400 | 请求级差异（对比手工 URL 瑞数签名长度/格式） |

### 2026-09-01 15:15 复盘：手工点击也 400 → 环境被标记，改 CDP 接管真实 Chrome

run7（cnipa_21869c035f45，15:13）：`api_calls` 记录 **5 次 search 全 400**（脚本自动 1 次 + 用户在自动化浏览器里手工补点 4 次）→ **排除“合成点击被识别”**，是自动化浏览器环境/会话被瑞数标记。响应 `content-type: text/html`、`content-length: 6`、`server: ******`（瑞数隐藏）→ WAF 层拒绝，非应用错误。被动采集已加：`search_api_req_headers`（cookie 只记长度）、`cookie_inventory`（cnipa.gov.cn 域 cookie 元数据，无值）。

**判别/修复实验（CDP 接管）**：`launch_browser_session` 已内置 `CNIPA_CDP_URL` 接管分支（复用已打开的 cpquery 标签页，`detach=True` 不关用户浏览器、不写 session 文件）。步骤：真实 Chrome 带 `--remote-debugging-port=9222` + 独立 `--user-data-dir` 启动 → 手工登录并停在复审无效查询页 → `$env:CNIPA_CDP_URL='http://localhost:9222'` 后跑 02。真实 Chrome 无自动化标记，瑞数 VM 按真实浏览器跑 XHR 签名。若 CDP 下 200 → 架构定型为“接管真实 Chrome”；若仍 400 → 比对 cookie_inventory/请求头与用户日常浏览器差异（顺带确认用户手工查询用的是哪个浏览器）。

### 2026-09-01 15:28 复盘：CDP 接管真实 Chrome 仍 400 → 嫌疑收窄为 ① CDP 检测 / ② Chrome 104 过老

run8（cnipa_5e21057b00de，debug 目录 search_20260901_152614）：`browser_mode=cdp`（成功接管用户手工登录好的真实 Chrome 标签页）、键入路径完美（`200610004173.6` → 框内 `2006100041736`）、cookie 面正常（SESSION×2 + 瑞数 dX1xbeyMT58WO/P 双 cookie，httpOnly 属性符合预期）——**search 依旧 400**（`text/html`、`content-length: 6`、`server: ******`，与 run6/7 完全同一签名）。`search_api_req_headers` 揭示关键线索：UA = **Chrome/104.0.5112.102**（2022 年的版本，本机 Chrome 主程序太老）。

排除至此：输入值✗、点击✗（人手点也 400）、自动化浏览器 profile/cookie✗（真实 Chrome 手工登录也 400）。剩余嫌疑两条：

1. **瑞数检测到 CDP attach**（开启 DevTools 协议即污染 JS 环境/停止喂合法签名，业内著名行为）→ 给出的 `hHp4Kgam` 是假签名；
2. **本机 Chrome 104 太老/指纹异常**，服务端按版本拒绝。

零成本判别（脚本已退出、CDP 已断开，浏览器窗口还开着）：

- **测试 1**：在那个还开着的 CDP Chrome 窗口里**手工点一次「查询」**。出结果 → 嫌疑①成立（attach 期间被检测，detach 后恢复）；仍 0 条 → 嫌疑②方向加重。
- **测试 2**：确认用户**平时手工查询能出结果**用的哪个浏览器，在该浏览器 F12 控制台执行 `navigator.userAgent` 记录版本。

后续分支：嫌疑①成立 → 02b（manual_click）+ `$env:CNIPA_CDP_URL` 复核「attach 状态下人手点击」，并调研免 CDP 的接管方案；嫌疑②成立 → 换现代 Chrome/Edge 通道（`CNIPA_USE_CHROME=1` 指向新版 Chrome 或 msedge channel）重试。

### 2026-09-01 15:58 复盘：机器上其实有两套 Chrome →「日常能查的是 125，不是 104」，嫌疑②复活

测试 1/2 结果：CDP 断开后在 104 窗口手工点查询**仍不出结果**；用户日常查询能出结果的浏览器「关于」页显示 **125.0.6422.61**（更新报错 0x80004005，每用户装在 Administrator 目录、无权自更新）。机器侧证据（进程+注册表）：

- `C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe` = **125.0.6422.61**，52 个进程在跑，窗口标题「设置 - 关于 Chrome」——**用户日常浏览器就是它**（HKCU App Paths 指向它，快捷方式解析到它；用户原以为用的是 Program Files）；
- `C:\Program Files\Google\Chrome\Application\chrome.exe` = **104.0.5112.102**（Selenium 遗留，目录里还有没装成的 new_chrome.exe/135），7 个进程 = run8 的 CDP 实验窗口（9222 仍开）。

**修正 15:28 的结论**：嫌疑②（版本）没死——run8 用的 104 与日常 125 差了 21 个大版本；但 Playwright Chromium 145（新 UA）也 400，所以自动化检测嫌疑①同样活着。run8 里 104+全新 cdp-profile+调试口三个变量混在一起，**都怪不得单变量**。

**单变量判别实验（run9，挂在日常 125 + 默认 profile 上，只加调试口/attach 一个变量）**：

1. 两个 Chrome 全部退出（日常 125 有后台运行设置，确认托盘；退不干净可 `Stop-Process -Name chrome -Force`，注意先存工作）；
2. `Start-Process 'C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe' -ArgumentList '--remote-debugging-port=9223','--no-first-run','https://cpquery.cponline.cnipa.gov.cn'`（**不加** `--user-data-dir` → 默认 profile，登录态/指纹全真）；
3. 确认查询页已登录（默认 profile 应已登录过，否则手工登）；
4. `$env:CNIPA_CDP_URL='http://localhost:9223'` 后跑 02（`python python/run_cnipa_search_decisions_pw.py python/test_inputs/02_search_by_application_no.json`）；
5. 脚本退出后在同一窗口再**手工点一次查询**（检验 detach 后是否恢复）。

| run9 结果 | 定案 → 下一步 |
| --- | --- |
| 自动点 200+数据 | **104 是主因**，CDP 接管在 125 上可行 → 自动化通道切 125 二进制，联调继续 03/04/05 |
| 自动 400 / 断开后手工 200 | attach 期间被标记 → CDP 检测成立 → 转免 CDP 方案（OS 级键鼠驱动日常 Chrome，isTrusted 事件） |
| 自动 400 / 断开后手工仍 400 | 调试口/attach 被持久检测（与 Chromium 145 互证） → 同上转免 CDP 方案 |

### 2026-09-01 16:10 复盘：run9 命中第 3 行 → 一切 CDP 形态全灭，只剩「本地检测 vs 会话标记」最后一岔

run9（cnipa_29499ff676ec，debug 目录 search_20260901_160500）：环境验证干净——9223 监听、机器上只有 125 二进制在跑（请求 UA=`Chrome/125.0.0.0`）、默认 profile、已登录。结果：`browser_mode=cdp`、键入完美、search **仍 400**（同签名：text/html / 6 字节 / server ******）、`api_calls` **4×400**（脚本 1 + 用户手工 3），脚本退出后用户手工点、重输申请号再点**全都不出结果**。run8/run9 的 `hHp4Kgam` 签名形态完全一致（均 119 字符同结构）→ VM 在正常出签、服务端验签拒收 → 「检测到自动化后故意错密钥出假签」或「会话被标记」二选一。

**至此定案**：输入✗ 点击✗ 版本✗（104/125 都 400）profile✗（全新/日常默认都 400）会话 cookie✗（手工登录的日常会话也 400）——**Playwright/CDP 一切形态（自带 Chromium、接管 104、接管 125）都被拒**，该架构对此站判死刑。

最后一岔（对照实验 R1，零成本）：**完全退出当前 Chrome（9223 那个），用平时的方式正常打开（不加任何参数），手工查 200610004173.6**：

| R1 结果 | 定案 → 下一步 |
| --- | --- |
| 出结果 | 检测在**本地**（调试口/attach 触发，去掉端口即恢复）→ 免 CDP 方案：pywinauto/UIA + SendInput 驱动日常 Chrome（事件 isTrusted、JS 无感知、UIA 树可读结果），Playwright 退居离线工具 |
| 不出结果 | 检测在**服务端会话级**（该登录态被标记）→ 查恢复条件（退出重登/换账号/冷却期），期间任何自动化（含 OS 级）都会 400，先弄清标记的生命周期 |

### 2026-09-01 16:14 复盘：R1 出结果 → 本地检测定案，架构转免 CDP（OS 层 UIA + SendInput）

R1：完全退出 9223 实例，**正常方式**启动日常 125（先开 Jira 再开 cpquery、登录、查询）→ **能查出结果**。定案：检测在**本地**（调试口/attach 触发，去掉即恢复），会话/账号/profile 全干净，服务端无持久标记。Playwright/CDP 对此站正式出局。

**新架构（免 CDP）**：pywinauto(UIA) + SendInput 驱动用户日常 Chrome——真实键盘鼠标事件 `isTrusted=true`、请求由浏览器自己正常发出、页面 JS 与瑞数无从感知、调试口永远不出现。

工程要点（probe 实测 `output/uia_probe/cpquery_tree_20260901_161559.txt`）：
1. Chrome 默认**不开**渲染无障碍树——顶层窗口只暴露 45 节点（标签栏/工具栏/地址栏），`Document` 空 → 需从 `Chrome_RenderWidgetHostHWND` 子窗口入口触发，或自动化启动时加 `--force-renderer-accessibility`（普通启动参数、非调试口、页面不可见）；
2. SendInput 特性 = 目标窗口须前台焦点 → 自动化运行期间用户别抢键鼠（秒级）；
3. 带参数启动需 Chrome 未运行（同 profile 单例机制）→ 流程定为「确保 Chrome 全退 → 带参数启动默认 profile（登录态保留）→ 跑 → 留现场」；
4. `python/uia_probe_readonly.py` = 只读探测器（零点击零输入零网络），后续选择器的地基。

> 离线验证三件套（不访问国知局）：`offline_selftest.py`（逻辑自检）、`offline_selector_check.py`（用 debug_steps 真实 HTML 回放选择器）、`python -m py_compile <脚本>`。改完选择器先跑回放再联调。

### 2026-09-01 16:35 复盘：a11y 树打通 + 结果区结构定案 → UIA runner 就绪

用户带 `--force-renderer-accessibility` 重启日常 125 后，只读 probe 两次成功：`cpquery_tree_20260901_162941.txt`（223 节点，空表单）→ `cpquery_tree_20260901_163521.txt`（**242 节点，用户手工查询后的完整结果区**）。MSAA 触发/渲染宿主子窗口都已不需要，一个启动参数解决。

控件结构定案（选择器全部有实测坐标背书）：
- 表单：标签是无名 Text（`'申请号/专利号：'`），输入框是无名 Edit，**同行右侧最近几何配对**（垂直中心差≤40px、Edit.left>Text.right）；按钮有名：Button `查询`/`重置`，MenuItem `展开更多搜索项`；
- 库切换：RadioButton `复审决定库`/`无效决定库`（对应输入 `library_type`）；页签：TabItem `复审无效审查决定查询`（找不到申请号框时自动点它）；
- 总数：`Text '共查询到 '` + `Edit(N)` + `Text ' 条结果'`，分页区另有 `Text '共N条'`；
- 结果卡片：卡片头 = `发明专利 `/`实用新型 `/`外观设计 `（含尾空格），同行右侧 Text = 发明名称；字段是纯 Text 键值对（决定号/发文日/申请号/专利号/专利权人/申请日/分类号/审查结论/无效宣告请求人），**案件编号例外**：标签 `'案件编号： '` 值在下一个 Text（`'4W107577'`）；页脚 `主办单位：` 等用字段白名单排除；
- 登录态：MenuItem `欢迎您，杨光`。

交付物（均已离线自检通过：`py_compile` + 解析器 selftest 逐字段对拍 163521 实测树）：
- `python/cnipa_uia.py` — UIA 动作层（窗口定位/几何配对/真实键鼠/读总数/截图）；
- `python/run_cnipa_search_decisions_uia.py` — 02 等价 runner：窗口→登录检查→(自动切 Tab)→重置→选库→填申请号(回读校验，不一致不点查询)→点查询→等总数变化→解析卡片→截图+平铺树 dump→`python/output/uia_runs/run_<ts>/payload.json`。自检：`--selftest`。

下一步：用户实跑 UIA 版 02（运行期间键鼠别抢焦点）；通过后按此通道迁移 03 案件号/04 详情/05 附图，最后把 MCP 挂到 UIA 版脚本上。

### 2026-09-01 16:45 复盘：02 首跑即通 ✅ + 两处瑕疵定位修复（离线闭环）

**run_20260901_164253：免 CDP 架构端到端首跑成功**——真实键鼠键入申请号、点查询、总数 0→1、截图+树 dump+payload 全落盘，全程 14.4s，瑞数零感知（无 400、无验证码）。流程链路定案，不再有架构悬念。证据：`python/output/uia_runs/run_20260901_164253/payload.json`。

首跑暴露两处解析/回读瑕疵，均已根因定位并修复（**未再实跑，待 03 一次实跑同时验证两修复**）：

1. **幻影第二张卡 + 表单标签污染**（payload 出 2 张卡，卡 1 的 decision_no 被写成 `'关键词：'`、app_no 被写成 `'cancel'`）：
   - 根因：`win.descendants()` 把整页内容**枚举了两遍**（tree_flat.txt 中 7-110 行与 113-220 行完全相同含坐标）→ 第二份副本的表单标签盖进卡 1 字段、并拼出幻影卡 2；
   - 修复（`run_cnipa_search_decisions_uia.py::parse_cards`）：① `(name, rect)` 精确去重 `_dedup_items`；② y 门槛——以 `共查询到` 总数条 top 为界，只解析其**下方**文本（表单标签永远在上方），关键字段白名单 KEY_MAP 兜底页脚；
   - 离线闭环：对抗 selftest（含幻影副本+表单污染序列 → 恰好 1 张卡、11 字段全对）；`--replay run_20260901_164253/tree_flat.txt` 真实树回放 → **98 条文本 → 1 张干净卡**。

2. **申请号回读为空**（step type_app_no typed=''，走 warn-and-continue 有惊无险）：
   - 根因：几何配对命中的是**外层容器 Edit**（无值）；真正持值的是内层 input Edit，但其 accessible name 是 antd 清除图标名 `'cancel'`，**不是值** → 旧 `edit_value` 拿 window_text 当值用会读空甚至读成 `'cancel'` 造成误拦截；
   - 修复（`cnipa_uia.py::edit_value`）：只走 LegacyIAccessible.value 值通道，自身读不到下钻子 Edit 再读；window_text 永久禁用作值源。卡片里 app_no=`2006100041736`（站点对 `200610004173.6` 的规范化）证明该通道有效，待下次实跑回读确认。

工程兜底：`run_20260901_164022` 崩溃只留空目录无任何证据 → runner `__main__` 已加崩溃落盘（`python/output/uia_runs/last_crash.txt`：时间戳+traceback，exit 9）。

下一动作：用户点一次「展开更多搜索项」→ 只读 probe 抓展开态树（03 案件号检索需要「案件编号」字段，收起态表单没有）→ 写 03 runner → 一次实跑同时验证解析修复与回读修复。

### 2026-09-01 17:05 复盘：展开态树定案 → runner 升级支持 03 案件号（离线三连全过）

用户点「展开更多搜索项」后 probe 抓到 **367 节点**（`output/uia_probe/cpquery_tree_20260901_165804.txt`），展开态结构定案：

- **「案件编号」字段确认**：标签 @(1157,516) + Edit @(1231,512)，展开表单右列（决定号右侧）；表单共 22 字段，按钮区下移至 y836，「展开更多搜索项」↔「收起更多搜索项」是同一互斥按钮（按在场者判断状态，可做幂等展开）；
- **新风险（本次最大发现）**：表单撑高把结果卡片推出视口（卡片头 y1033 > 窗口底 1040）→ Chrome a11y 对**屏外文本回 0 矩形 @(0,0,0,0)**（虚拟化）→ 16:45 版解析器的 y 门槛会把卡片全部误杀。

runner 改造（02/03 一个脚本通吃，全部离线验证）：
1. **解析门槛重构**：y 门槛 → **枚举顺序门槛为主**（'共查询到' 在序列中的位置为界，UIA 枚举顺序跟 DOM 一致，表单标签含与卡片同名的 '案件编号：' 永远在界前）+ y 门槛降级为辅且**只约束实矩形**（0 矩形不受 y 过滤）；
2. **字段路由**：`filters.case_no` → 自动幂等展开（COLLAPSE_ITEM 在场则已是展开态，跳过）→ label_to_edit('案件编号')；`filters.application_no` 走原路径；
3. **回读比对改 `_alnum`**（案件号 '4W107577' 含字母，`_digits` 会抹字母致比对退化）；
4. **滚屏兜底**：`cnipa_uia.scroll_down`（pywinauto.mouse.scroll，wheel_dist 负值=下滚，isTrusted 真实滚轮）；总数>0 而卡片解析 0 时滚 5 格重采一次。

离线三连（不碰网站/窗口）：`py_compile` OK；selftest 三形态对抗（收起态污染 + **展开态 0 矩形 + 表单同名 '案件编号：'** + 幻影副本）全部「1 张卡 11 字段全对」；真实树双回放——164253 收起态 98 文本→1 卡（回归）、165804 展开态平铺 158 文本→1 卡（`*_flat.txt` 由层级 dump 转换）。

下一动作：用户实跑 03（案件号 4W107577）；payload 的 `type_case_no.match=true` 即回读修复的实跑证据，`case_no_match=true` 即检索+解析全链路证据。

### 2026-09-01 17:15 复盘：03 案件号实跑全链路成功 + 回读为空根因终案（双重 bug）

**03 实跑（run_20260901_171031，20.0s）**：window(logged_in) → reset → ensure_expanded(already=true，幂等检测生效) → radio(无效决定库 already) → type_case_no → click_query → total 0→1 → parse_cards **n=1、11 字段全对**（title/decision_no=38430/issue_date=20181228/app_no=2006100041736/patentee=远大医药/conclusion=维持有效/invalid_requester=李瑶…）→ `case_no_match=true`，`ok=true`。02/03 两条检索路径首跑即通，免 CDP 架构两连验证。唯一残留：`type_case_no typed='' match=false`（warn-and-continue 兜住，不影响结果）。

**回读根因终案**（uia_probe_readback 只读穷举，一次命中）——`edit_value` 恒空是**两个 bug 叠加**：

1. **键名大小写**：pywinauto `legacy_properties()` 返回的键是大写 `'Value'`（全量字典：`{'Name': 'cancel', 'Role': 42, 'State': 1048576, 'Value': '4W107577', …}`），旧代码取小写 `'value'` → 键不存在恒空。02/03 两轮回读为空的**共同根因**；卡片里 `app_no` 值正确只证明键入成功，不证明读回成功。
2. **下钻深度**：真 input Edit 在第 3 层（外层容器 Edit > Group > Group > input Edit），旧代码只查直接子级（全是 Group）→ 即便键名对了也读不到。

修复（`cnipa_uia.py::edit_value`）：读值通道 `get_value()` 优先（探针实测三通道均返回 '4W107577'：`get_value()`、`iface_value.CurrentValue`、`legacy_properties()['Value']`），legacy 兜底用大写键；下钻改 `descendants()` 过滤 Edit（容器子树小，代价可忽略）。

验证闭环（零点击零键入零请求）：`py_compile` OK；stub 单测 4/4（容器>Group>Group>Edit 穿透 / 无内层 Edit 返空不误报 / 自身持值直读 / 内层直读）；selftest 三形态 + 双真实树回放回归无碍；**活体只读验证**——对还开着的页面直接读：`案件编号 → '4W107577'`、`申请号 → ''`（未填字段正确返空）。修复即终验，无需为验证回读再实跑一轮。

附：内层 input Edit 的 accessible name 恒为 antd 清除图标名 `'cancel'`（仅输入非空时渲染）→ 未来若需「框内非空」的廉价信号，可直接用内层 Edit 在场性判断。

下一动作：04 详情页 / 05 附图迁移到 UIA 通道（详情链接定位、弹层文本读取、附图下载），之后 MCP 挂接。

### 2026-09-01 17:30 复盘：04 详情页 UIA 版离线就绪（首跑即探针）

**选型**：03 树 dump 证明结果卡在 a11y 树是纯 Text、无命名「详情」入口 → 放弃找卡片点击；继承 CDP 时代 04 的定案——**URL 直达** `detail/index?zhuanlisqh=<申请号>&anjianbh=<案件编号>&searchType=8`（cnipa_common.build_detail_url 同款拼装）。导航 = Ctrl+T 新标签 → 地址栏真实键入 → Enter（isTrusted），抓完 Ctrl+W 关标签还原检索现场（keep_tab 可保留）。

**新 runner**：`run_cnipa_fetch_decision_detail_uia.py`（窗口优先 cpquery 标签、退任一同 profile Chrome 窗口；瑞数壳+SPA 双延迟就绪探测：标题含站点关键字×连续2轮 或 文本量稳定×3轮，40s 超时也照常落盘；「使用说明」遮罩有 确定 才点）。**首跑即探针**：成败都全量落盘 detail.png / tree_flat.txt / all_texts.json / raw_pairs，解析 misses 凭 dump 离线补映射不重跑。

解析器（离线纯函数）兼容三排布：整段「标签：值」/ 带冒号空值+长文本紧随（决定要点类）/ 裸标签+邻接值；首个非空值优先天然抗双枚举副本。离线验证：py_compile OK + 对抗自测 4/4（整段 12 列全对；拆分+裸标签+噪声+双枚举 5 列关键值正确；空输入全缺不误报；URL 拼装逐字比对）。

输入：`test_inputs/04_fetch_detail.json`（4W107577 / 2006100041736，与 03 同案可对拍）。下一动作：用户实跑 04 → 若 missing_fields 非空，凭 tree_flat/all_texts 离线补标签映射；05 附图（pss 系统）在 04 结构落定后迁移。

### 2026-09-01 17:35 复盘：04 直入 URL 实跑翻车（用户点破）→ 改判「结果页点击」

**翻车证据**（run=detail_20260901_172905，elapsed 50.2s）：Ctrl+T 直敲 detail/index URL 后 40s 只渲染出壳——all_texts 仅 14 条：页头（国家知识产权局/中国及多国…）+ 面包屑「国内案件信息详情」+ **空的「申请号/专利号：」**+ Chrome 默认浏览器提示条；数据接口一个字段不吐，field_count=1，ok=false。结论：**详情数据加载依赖站内会话上下文，地址栏直入带不上**（CDP 时代 page.goto 能直入是因为它带着完整站点会话上下文导航，与裸开标签不等价）。

**用户指正**（正确）：在检索结果页上点卡片链接即可看详情——a11y 树里卡片虽是纯 Text 无 Button 角色，但按几何坐标真实点击即可，与人工完全同路径。

**04 重写为点击流**（`run_cnipa_fetch_decision_detail_uia.py`，mode=uia_click）：
1. require_window（结果页须为前台标签）→ locate_card_link：门后第一张卡，**首选类型徽标同排长标题**（视觉链接），备选「案件编号」值文本（兼容 标签：+邻值 拆分形态），兜底类型徽标；
2. 0 矩形屏外 → 真实滚轮 6 格×至多 2 轮带回重采；
3. `cnipa_uia.click_coords`（新增原语：set_focus + pywinauto.mouse 真实点击坐标中心）；
4. 就绪判定 = 解析出 ≥6/12 列（壳页恒 0~1 列，专治转圈壳；selftest G 样例复刻 17:29 壳页防误判）；40s 超时也照常落盘；
5. 还原现场 Alt+←（同标签路由返回结果页；若站点开新标签则无历史无副作用——**不用 Ctrl+W**，避免误关结果页）；
6. 与入参对拍 case_no_match / app_no_match。

离线验证：py_compile OK + selftest 7/7——A) **03 真实树回放定位 title 且 real=False（触发滚屏路径）**、B) 实矩形无标题→备选案件编号值、C) 标题实矩形命中、D) 无卡片返 None、E/F) 解析 12 列全对 + 拆分/裸标签/双枚举、G) 壳页不误判就绪。

下一动作：用户在结果页保持前台状态下实跑 04 点击版；若 missing_fields 非空凭 dump 离线补标签映射。
