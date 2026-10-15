# core.py 滑块验证码代码问题修复记录

**文件**: `C:\git\Mcps\cnipa-mcp\python\src\utils\core.py`
**日期**: 2026-08-28

---

> **⚠️ 路径说明**
>
> 国知局（CNIPA）登录页实际走的主路径是 **`handle_cnipa_verify_captcha()`**，缺口靠 `.verify-img-panel` / `.verify-sub-block` 截图检测，**与易盾路径（`_get_yidun_images` / `handle_slider_captcha` 的 yidun 分支）无关**。
>
> 问题 1–5 主要描述的是易盾路径（`handle_slider_captcha`）的修复；问题 6–10 才是国知局 verify 主路径的修复。修改前请先确认你要改的是哪条路径。

---

> **⚠️ 登录流程权威说明（2026-08-31 更新）**
>
> **问题 37 + 38 是登录流程（`cnipa_common.py`）的最新真相**，覆盖了问题 32–36 的所有方案。
> 问题 32–36 中提到的 tysf 桥接、新标签 hack、返回系统优先等方案**均已废弃**，不要再按那些方案改代码。
> 当前登录流程以问题 38 v3 为准。
>
> **问题 39-42 是滑块缺口检测 + 登录后白屏的最新真相**：
> - 问题 39：fill 阈值 0.45→0.55，修复内容框落入空白区
> - 问题 40：content_offset 统一 + 亮度剖面精修，修复"差一点"系统误差
> - 问题 41：多方法共识门禁 + 精修保守化 + 松手稳定期 + SSO 白屏回退
> - 问题 42：v5 登录流程精简（去掉门户绕行）+ 三域 cookie 快照诊断
> - **问题 43：cpquery 自有 CAS 表单直接提交（绕过门户 SSO）** ← 当前最新

---

## 问题42：v5 登录流程精简 + 三域 cookie 快照诊断（2026-08-31）

**文件**: `C:\git\Mcps\cnipa-mcp\python\cnipa_common.py`

### 现象
v4 登录流程在门户处理上过于复杂（"返回系统"→cponline 导航→cpquery 多级回退），实际不可靠。
SSO 滑块通过后落到门户，goto cpquery 白屏，reload 仍白屏，最终失败。
无法判断 SSO session 在哪一环断裂。

### 根因
1. **门户绕行路径不可靠**：「返回系统」→ cponline 公开首页 → 顶栏导航 → cpquery，链路长且每步都可能失败
2. **SSO ticket 交换链不完整**：Playwright 自动化特征被识别，SSO 服务端改变行为（不执行 service 回跳）
3. **无诊断手段**：不知道 cookie 在哪一域缺失，无法定位问题

### 修复
**1. 登录流程精简（v5）**：
- 删除所有门户绕行逻辑（"返回系统"/cponline 导航）
- `_enter_cpquery_after_sso()` 简化为：
  - 1) 等自然回跳 cpquery（40 轮 × 500ms = 20s）
  - 2) 落到门户 → 直接 goto cpquery → 等验证（12 轮）
  - 3) cpquery 白屏 → reload 一次 → 等验证（30 轮）
  - 4) 全失败 → 报错 + 附带 cookie 快照诊断

**2. 三域 cookie 快照诊断**：
- 新增 `_snapshot_cookies_and_redirects(page, tag)` 函数
- 在滑块通过后立即抓取三域 cookie：
  - `sso.cponline.cnipa.gov.cn`
  - `tysfjk.cponline.cnipa.gov.cn`
  - `cpquery.cponline.cnipa.gov.cn`
- 同时抓取 performance navigation entries（跳转链）+ 资源数
- 新增 `_log_cookie_snapshot(info)` 函数，输出可读日志

**3. 诊断点增加 cookie 快照**：
- `06_slider_passed`：滑块通过后立即抓取（SSO 换票链完整性）
- `09_final_fail`：最终失败时抓取（定位缺失域）

### 实测对比
| 指标 | v4 | v5 |
|------|----|----|
| 门户处理 | 3 级回退（goto→返回系统→导航） | 直接 goto cpquery |
| 诊断能力 | 仅页面状态 | 页面状态 + 三域 cookie + 跳转链 |
| 代码复杂度 | 高（门户绕行逻辑 80+ 行） | 低（精简到 40 行） |

### 待验证
v5 仍是假设：反自动化检测绕过 + 直接 goto cpquery 能否解决白屏问题。
若仍失败，三域 cookie 快照将揭示 SSO ticket 在哪一域缺失，指导后续修复。

---

## 问题41：单方法缺口检测不可靠 + 登录后 cpquery 白屏（2026-08-31）

**文件**: `C:\git\Mcps\cnipa-mcp\python\src\utils\core.py` + `cnipa_common.py`

### 现象
实测 3 次拖拽：第1次暗背景未拖动，第2次 gap_x=215 仅 shape-hole 独赢→验证失败，第3次 3 方法一致→通过。
登录后 cpquery 白屏（body_len=0, html_len=39, 仅 1 资源）。

### 根因
1. **单方法检测无交叉验证**：仅 shape-hole 时 gap 位置可能偏 10-60px，闭环收敛到错误目标
2. **亮度精修过度**：原允许左移 ≤12px，实际可能移过头
3. **松手前稳定期过短**：40-80ms 易被服务器端轨迹分析识别为机器人
4. **SSO session 传播不足**：1200ms 等待不够，cpquery SPA 未初始化

### 修复
**core.py**：
1. 门禁从 `detect_methods == {'rel-dark'}` 改为 `len(detect_methods) < 2`——任何单方法检测都拒绝
2. 亮度精修：扫描范围 ±30px → ±15px，最大左移 12px → 5px，亮度差阈值 3 → 8
3. 松手前等待 40-80ms → 150-300ms
4. shape-hole 模板匹配添加亚像素抛物线插值精修

**cnipa_common.py**：
1. SSO 通过后等待 1200ms → 2500ms
2. cpquery reload 仍白屏时，新增回退：回门户 →「返回系统」→ cponline 导航进 cpquery
3. 新增常量 `SSO_PORTAL_URL`

### 实测对比
| 指标 | 修复前（第2次） | 修复后预期 |
|------|----------------|-----------|
| 方法数 | 1 (shape-hole) | ≥2 才拖动 |
| 精修幅度 | 219→215 (4px) | ≤5px 且 drop>8 |
| 松手等待 | 40-80ms | 150-300ms |
| 白屏回退 | 仅 reload | reload + 门户备选路径 |

---

## 问题40：content_offset 不一致 + 模板匹配系统偏左导致"差一点"（2026-08-31）

**文件**: `C:\git\Mcps\cnipa-mcp\python\src\utils\core.py`

### 现象
3 次拖拽闭环均报告误差 < 0.3px（完美对齐），但用户肉眼可见拼图块没有到达缺口位置，"还差一些"。

### 根因 1：content_offset 参考点不一致
缺口检测用 Otsu 轮廓的 `x0` 定义内容框（如 x0=3），模板匹配基于此计算 gap_x。
但拖拽对齐用 `_detect_piece_content_offset()` 独立检测偏移（如 variance 法检测到白边框 x=1）。
两者参考点不同 → 闭环"完美对齐"到一个**错误目标**。

**示例**：
- Otsu 轮廓 x0=3 → gap_x=249 基于"内容从 x=3 开始"
- variance 法 → content_offset=1 → 拖拽对齐基于"内容从 x=1 开始"
- 闭环对齐：piece_element_left + 1 = gap_css → piece 视觉内容（x=3 处）在 gap_css+2
- 实际效果：piece 偏右 2px（或等价地，目标偏左 2px）

### 根因 2：模板匹配峰值 ≠ 缺口真实左边缘
shape-hole 方法的模板匹配在 hole_map（高斯模糊减原图）上做 TM_CCORR_NORMED。
峰值对应阴影模式最匹配位置，但缺口视觉左边缘是**亮度骤降处**。
阴影可能向右延伸（penumbra），导致峰值偏左几像素。

### 修复
1. **content_offset 统一**：`_detect_yidun_gap_offset()` 返回三元组 `(gap_x, methods, content_offset_x0)`，
   其中 `content_offset_x0` 是 Otsu 轮廓的 x0（与 gap_x 同一参考点）。
   `handle_cnipa_verify_captcha()` 直接使用此值，不再调用 `_detect_piece_content_offset()`。

2. **亮度剖面左边缘精修**：模板匹配后，在峰值附近扫描背景亮度剖面，
   找到亮度梯度最大处（从亮到暗的骤降 = 缺口真实左边缘）。
   验证方向正确（左亮右暗）且偏移 ≤12px 后，使用精修位置。

3. **暗背景单方法门禁**：暗背景（brightness<100）下，单方法检测且 score<8.0 → 拒绝拖动，刷新重试。

### 返回值变化
```python
# 旧
gap_x, methods = _detect_yidun_gap_offset(bg, piece)

# 新
gap_x, methods, content_offset_x0 = _detect_yidun_gap_offset(bg, piece)
```

### 网上方案参考
- `puzzle-slider-captcha` (PyPI): 抛物线亚像素精修 matchTemplate 峰值
- `mehul-08/slider-captcha-solver`: 先 resize 背景到渲染尺寸再匹配，避免坐标转换
- 通用做法：缺口左边缘 = 亮度剖面骤降处，而非模板匹配峰值

---

## 问题39：Otsu 轮廓 fill 阈值过严导致内容框落入空白区域（2026-08-31）

**文件**: `C:\git\Mcps\cnipa-mcp\python\src\utils\core.py`

### 现象
滑块拖动「差了好几个厘米，肉眼可见」。日志显示 `拼图内容(中部默认) bbox=(0,70,60,60)`，缺口检测 gap_x=154，但真实缺口在 x≈211。

### 根因
拼图块截图（`.verify-sub-block`，61×200px）内部结构为：上部（y=1-56）和下部（y=138-199）各有一块 jigsaw 内容，中间（y=60-137）是大片空白灰色区域。

`_detect_yidun_gap_offset()` 中 Otsu 轮廓检测找到了正确的内容框 `(x=3, y=1, w=54, h=56, fill=0.478)`，但 fill 阈值代码写的是 `> 0.45`（注释写的是 `> 0.55`），导致正确轮廓被拒绝，回退到默认框 `(0, 70, 60, 60)`——完全落在空白区域。

空白区域做模板匹配时，在 x=154 处找到假阳性（背景纹理的偶然匹配），与真实缺口 x≈211 偏差 57 像素（约 2cm）。

### 修复
将两处 fill 阈值从 `0.45` 改为 `0.55`，与注释一致：
- 第 381 行（bright_pc 轮廓检测循环）
- 第 400 行（Otsu 轮廓检测循环）

```python
# BEFORE:
if fill > 0.45 or fill < 0.05:  # 注释写 0.55 但代码写 0.45

# AFTER:
if fill > 0.55 or fill < 0.05:  # 与注释一致
```

### 验证结果（静态测试图片）
| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 内容框 | `(0,70,60,60)` 空白区域 | `(3,1,54,56)` 实际拼图内容 |
| gap_x | 154（假阳性） | **209**（真实缺口） |
| 检测方法 | `{'rel-dark'}` 独赢→被门禁拒绝 | `{'rel-dark', 'shape-hole'}` 交叉验证通过 |
| 置信度 | 7.18（单方法） | **8.90**（双方法） |

### 教训
注释与代码不一致是隐蔽 bug 来源。代码审查时应特别关注注释中描述的意图与实际阈值/条件是否匹配。

---

## 问题38：SSO service 参数不生效 + Playwright 自动化检测（2026-08-31）【登录流程最终版】

**文件**: `C:\git\Mcps\cnipa-mcp\python\cnipa_common.py`

### 现象
SSO 滑块登录成功后，浏览器落到 `tysfjk.cponline.cnipa.gov.cn/portal/#/home/welcome`（统一门户），而非期望的 `cpquery.cponline.cnipa.gov.cn/chinesepatent/index`。

### 根因链
1. **SSO service 参数不生效**：`_sso_login_url_for_cpquery()` 构造 `https://sso.cponline.cnipa.gov.cn/login?service={CPQUERY_INDEX}`，但 SSO 服务端忽略 `service` 参数，登录后默认跳转到门户
2. **Playwright 被自动化检测识别**：诊断脚本 `diagnose_sso.py` 发现——
   - SSO 登录后 cookie `dxLxbeyMT58WO` 被正确设在 `cpquery.cponline.cnipa.gov.cn` 域
   - 诊断脚本中直接 goto cpquery **成功**（cookie 有效）
   - 但同一 Playwright 浏览器手动开新标签访问 cpquery 却无限转圈
   - **关键对比**：手工真实浏览器登录 → 成功到 cpquery；Playwright → 总是到门户
   - **结论**：SSO/cpquery 检测了 `navigator.webdriver=true` 等自动化特征，对 Playwright 改变行为

### 修复历程

**v1**（失败）：门户处理改为三级回退（直接 goto → 返回系统 → 兜底 SSO）。实测 goto cpquery 闪登录页后回门户，2b 未执行直接跳到 2c。

**v2**（失败）：调整优先级为「返回系统 → cponline 导航」优先。实测 cponline 导航也不可靠。

**v3**（基础）：添加反自动化检测绕过 + 恢复 goto cpquery 优先
- 新增 `_HIDE_AUTOMATION_SCRIPT`：`navigator.webdriver=false`、`window.chrome.runtime`、`permissions.query` 覆盖
- 新增 `_apply_hide_automation(page)`：在任何国知局页面访问前调用 `page.add_init_script()`
- `cnipa_sso_login()` 步骤 0 调用 `_apply_hide_automation(page)`
- 门户处理恢复为直接 goto cpquery 优先：
  - 2a) 直接 goto cpquery（诊断脚本验证 cookie 有效）
  - 2b) 「返回系统」→ cponline 导航（备选）

**v4**（当前）：增强反自动化 + 修复流程 bug + 滑块验证修复
- 反自动化脚本增强：新增 `navigator.plugins`（伪造 5 个插件）、`navigator.languages`（zh-CN 优先）、删除 `window.cdc_*` 变量（Playwright 指纹）、`Notification.permission` 覆盖、删除 `__playwright` 等 Playwright 特有属性
- 新增 `_JS_ERROR_COLLECTOR`：全局捕获 JS 错误和未处理 Promise 拒绝
- 修复流程 bug：步骤 2a 失败后若 URL 已在 cpquery 域，走步骤 4（白屏 reload）而不是直接报错
- 步骤 2a 改用 `networkidle` 等待 SPA 完全加载
- 白屏诊断：自动截图 `output/cpquery_blank.png` 并收集 JS 错误
- 滑块验证修复：元素偏移补偿 + 深色背景自适应

### 当前登录流程（v5，权威版）
1. `_apply_hide_automation(page)`（步骤 0，隐藏自动化特征 v4 + JS 错误收集器）
2. `page.goto(CPQUERY_INDEX)` → 等 3 秒（等自然跳 SSO）
3. 若已登录 → 直接进 cpquery
4. 若未出登录表单 → `page.goto(_sso_login_url_for_cpquery())`
5. SSO 仍无表单 → 报错（禁止回退 tysf）
6. 滑块通过 → cookie 快照（`_snapshot_cookies_and_redirects`）→ `_enter_cpquery_after_sso()`
7. `_enter_cpquery_after_sso()`（v5 精简版）：
   - 7a) 等自然回跳 cpquery（40 轮 × 500ms = 20s）
   - 7b) 落到门户 → 直接 goto cpquery → 等验证（12 轮）
   - 7c) cpquery 白屏 → reload 一次 → 等验证（30 轮）
   - 全失败 → 报错 + 附带 cookie 快照诊断
8. **已删除**：门户绕行（"返回系统"→cponline 导航→cpquery）

### 另一入口：`goto_cpquery_search_page()`（复审无效查询入口）
此函数独立于 `_enter_cpquery_after_sso()`，用于已登录后直接进入 cpquery 查询页。
- 主路径：`page.goto(CPQUERY_INDEX, wait_until='domcontentloaded')` → 等 2 秒
- 门户处理：「返回系统」→ cponline 导航（与 _enter_cpquery_after_sso 的 7b 类似）
- 备选：SSO service goto（`_sso_login_url_for_cpquery()`）→ 等 20 轮
- **v4 补充**：已在 cpquery 域但验证未通过 → reload + 等 20 轮（与 _enter_cpquery_after_sso 的步骤 8 对称）
- 最后：关闭使用说明弹窗 → 选「复审无效审查决定查询」菜单

### 已知风险
1. **`networkidle` 在 SPA 上可能久等/超时**：cpquery 是 SPA，可能有持续的网络请求（轮询、心跳），导致 `networkidle` 永远不触发。当前 timeout=60s/90s，若超时则降级到 `domcontentloaded`
2. **暗底加权是场景补丁**：`bg_brightness < 100` 时调整权重，长期可能不稳。如果 cpquery 更换验证码图片风格（如全暗背景），可能需要调整阈值或方法
3. **v4 反检测脚本的 `navigator.plugins` 模拟**：虽已补全 `refresh()`/`item()`/`namedItem()`，但 PluginArray 的 `length` 属性、Symbol.iterator 行为等仍可能与真实浏览器有差异
4. **`_persist_cpquery_redirect` 写 storage 是无效操作**：tysf 门户的 hash 路由不读 sessionStorage/localStorage 的 `redirectUrl` 等 key，不会造成副作用但也不起作用

### 待验证（v4，未实测通过）
v4 仍是假设：反自动化检测绕过（v4 8 项）+ 直接 goto cpquery 能否真正解决门户重定向/白屏问题。**未实测通过前不要当已闭环**。

若仍失败，需进一步调查：
- `navigator.plugins` 的 PluginArray 模拟是否被深度检测识破（`refresh()`/`item()`/`namedItem()` 行为）
- WebGL 指纹（`WEBGL_debug_renderer_info` 返回的 renderer/vendor）
- Canvas 指纹差异
- `cdc_*` 变量删除时机（init_script 在页面加载前执行，但页面 JS 可能在执行后再次检查）
- 其他 Playwright 特有指纹（如 `__pw_manual`、`__PW_inspect` 的 getter 行为）

### 补充修改（2026-08-31 09:20）
- 删除 `_enter_cpquery_after_sso()` 的 SSO 兜底 goto（原第 1127-1136 行）
- 原因：已知 `service` 参数经常不生效，会再闪一次门户；v3 流程应为「全失败 → 报错」

### v4 修复（2026-08-31 09:44）

**滑块验证修复**（`core.py`）：
1. **元素偏移补偿**：新增 `_detect_piece_content_offset()` 检测拼图块元素内可见 jigsaw 内容的实际起始列（alpha 通道 + 列方差双重策略），`_read_piece_offset()` 和 `start_offset` 都加上 `content_offset_css` 补偿
2. **深色背景自适应**：计算背景图平均亮度 `bg_brightness`，亮度 < 100 时提升 `edge`/`hollow` 权重（基于结构特征，不受亮度影响），降低 `shape-hole`/`rel-dark` 权重（依赖亮度对比），聚类半径从 18px 放宽到 22px

**登录流程修复**（`cnipa_common.py`）：
1. **增强反自动化脚本**（v4）：新增 `navigator.plugins`（伪造 5 个插件）、`navigator.languages`（zh-CN 优先）、删除 `window.cdc_*` 变量（Playwright 指纹）、`Notification.permission` 覆盖、删除 `__playwright` 等 Playwright 特有属性
2. **修复流程 bug**：步骤 2a（goto cpquery）失败后，如果 URL 已在 cpquery 域，走步骤 4（reload）而不是直接报错。原代码因 `_is_typortal_page` 检测为 False 跳过 2b 后直接 raise，永远到不了步骤 4
3. **步骤 2a 改用 `networkidle`**：从 `domcontentloaded` 改为 `networkidle`，等 SPA 完全加载
4. **白屏诊断**：goto cpquery 后如果 body 空，自动截图 `output/cpquery_blank.png` 并收集 JS 错误
5. **JS 错误收集器**：新增 `_JS_ERROR_COLLECTOR` init script，全局捕获 `error` 和 `unhandledrejection` 事件

**代码审核修复**（2026-08-31 10:11）：
1. **步骤 4 条件放宽**：原来要求 `_cpquery_body_empty(page)`（body < 40 字符）才 reload。如果 cpquery 加载了有内容但无效的页面（如错误提示），会跳过 reload 直接报错。改为：只要在 cpquery 域且验证未通过就 reload
2. **`goto_cpquery_search_page()` 补充 reload 兜底**：与 `_enter_cpquery_after_sso()` 对称，已在 cpquery 域但验证未通过 → reload + 等 20 轮
3. **`navigator.plugins` 模拟补全**：新增 `refresh()`、`item()`、`namedItem()` 方法，防止深度指纹检测
4. **`_enter_cpquery_after_sso()` 文档字符串更新**：从「返回系统优先」改为 v4 实际流程（直接 goto cpquery 优先）

**结构化诊断系统**（2026-08-31 10:15）：
- 新增 `_diagnose_page_state(page, tag)` 函数，在关键节点收集：
  - **反检测状态**：`navigator.webdriver`、`plugins.length`/`refresh()`/`item()`、`languages`、`chrome.runtime`、`cdc_*` 泄露、`__playwright` 泄露
  - **DOM 状态**：body 文本长度/前 500 字预览、HTML 长度、title、是否有登录表单/欢迎语/使用说明弹窗/安全异常
  - **JS 错误**：`__cnipa_js_errors` 最近 30 条
  - **Cookie**：cpquery 域 cookie 数量/名称/是否有 session 类 cookie
  - **网络**：performance API 检测零传输可疑请求
  - **页面可见性**：`document.hidden`、iframe 数量和 src
- 新增 `_log_diagnostic(info)` 函数：输出日志摘要 + 写完整 JSON 到 `output/diagnostic_{tag}.json`
- 诊断点（tag 命名）：
  - `01_initial_goto`：初始 goto cpquery 后
  - `06_slider_passed`：滑块通过后、进入 cpquery 前
  - `07a_goto_success`：goto cpquery 后验证通过
  - `07a_goto_failed`：goto cpquery 后验证未通过
  - `08_reload_success`：reload 后验证通过
  - `08_reload_failed`：reload 后验证未通过
  - `09_final_fail`：最终失败（报错前）
  - `goto_reload_done`：`goto_cpquery_search_page` reload 后
- **用途**：每次测试都会输出结构化诊断，无论成功失败都能分析原因

---

## 问题37：残留风险修复 + 死代码清理（2026-08-31）

**文件**: `C:\git\Mcps\cnipa-mcp\python\cnipa_common.py`

**修复内容**（已被问题 38 v3 部分覆盖，但以下清理仍有效）：
- SSO 无表单时禁止回退 tysf，直接报错
- goto cpquery 后等 3 秒再检查（给自然跳 SSO 的时间）
- 删除死代码：`_cpquery_sso_entry_urls()`、`_try_cpquery_sso_entries()`

---

## ~~问题36：登录入口错误导致落到门户~~（已废弃，见问题38）

---

## ~~问题35：「返回系统」→ cponline 公开首页 → 裸 goto 白屏~~（已废弃，见问题38）

---

## 问题34：拖过缺口且只试 1 次就退出（2026-08-28）

**现象**: 闭环修正后仍过缺口；`run_cnipa_login_pw` 默认 `max_slider_attempts=1`，失败即退出。

**修复**: 默认改 3 次；开环欠 12px；前向修正限 10px×0.85，过冲可回拉，最多 2 轮闭环 + 最终回拉。

**关键文件**: `python/src/utils/core.py`, `python/run_cnipa_login_pw.py`

---

## ~~问题33：门户已登录时 tysf 桥接无效~~（已废弃，见问题38）

---

## ~~问题32：登录后进门户，裸 goto cpquery 白屏~~（已废弃，见问题38）

---

## 问题31：开环拖动精度不够 → 引入闭环微调（2026-08-28）

**现象**: 缺口检测偏差已缩小到几像素，但开环拖动（固定欠冲 4px）仍不稳定——有时过、有时差，无法一致通过。

**根因**: 开环模型无法补偿检测残余误差和控件响应差异。固定欠冲像素值在不同背景/缺口位置上表现不一致。

**修复策略（开环粗拖 + 闭环微调）**:

1. **删除场景补丁**: 移除 `dark_bg` 分支、`_plateau_left` 平台左缘修正、固定欠冲 4px 等针对特定背景色的补丁。缺口检测回归通用信号（白描边 + 形状边缘 + 相对暗洞）。
2. **开环粗拖**: 拖到 `目标 − 8px`，故意略短，宁可不到勿过。
3. **闭环微调（不松鼠标）**: 通过 `page.evaluate` 读 `.verify-sub-block` 相对 `.verify-img-panel` 的实时 CSS 位置，计算残差 `err = gap_css - cur_offset`。
4. **最多 1 次修正**: 若 `|err| > tol(2px)`，做一次小步修正（限幅 ±15px），再读一次确认。不再二次修正。
5. **修正后松手**: 无论是否修正，最终 `mouse.up()`。

**新增函数**:
- `_read_piece_offset(page, panel, piece)`: JS evaluate 读拼图块相对面板的 CSS 偏移
- `_drag_to(page, ...)`: ease-out 曲线拖动（鼠标已按住）
- `_micro_drag(page, dx, y)`: 3-6 步小幅修正拖动

**关键原则**:
- 闭环采样间隔 ~80-150ms，模拟人手微调节奏
- 修正幅度限幅 ±15px，防止异常 DOM 读数导致大跳
- 每次验证码最多 1 次闭环修正，避免长时间滞留被识别为机器人
- 闭环失败（修正后仍超差）→ 松手 → 外层刷新重试

**检测层门禁**（同次提交）:
- `_detect_yidun_gap_offset` 返回值改为 `(gap_x, methods_set)`
- `handle_cnipa_verify_captcha` 增加门禁：当 `methods == {'rel-dark'}`（仅 rel-dark 独赢，无交叉验证）时，拒绝拖动、直接刷新
- 原因：闭环只能补偿 ≤15px 的检测误差；lone rel-dark 在深绿匀色底常偏 >15px，闭环无法替代检测

---

## 问题30：深绿匀色底拖过缺口（2026-08-28）

**现象**: 缺口在深绿底上，仅 `rel-dark` 命中，拖动过了缺口。

**根因**: 匀色暗底上相对暗分数呈平台，峰值偏右；lone `rel-dark` 权重偏高。

**修复**: 平台左缘 `_plateau_left`；暗底降权 lone rel-dark、加重 hollow；开环欠冲 4px。

**关键文件**: `python/src/utils/core.py`

---

## 问题29：图亮度/颜色导致有时准有时偏（2026-08-28）

**现象**: 亮雪景等图绿框吸到深色主体（狐狸），真缺口在亮区右上。

**根因**: 固定灰度阈值 + 绝对最暗兜底，随图内容变化。

**修复**: 自适应高亮百分位镂空；**相对对比度**（邻域亮−洞内暗）；边缘匹配；删除 soft 绝对暗度兜底。

---

**根因**: 多套互斥回退（尤其列梯度）总能吐一个 x，图不同时方法切换 → 偏差厘米级。

**修复**:
1. 截背景前 **隐藏** `.verify-sub-block`，减少左侧浮块干扰；
2. hollow/edge/dark **投票聚类**，无可靠候选返回 None 刷新；
3. **删除列梯度瞎回退**。

---

**正确站**: `cpquery.../chinesepatent/index`（欢迎您 + 使用说明）。  
**错误站**: `tysfjk.../portal/#/home/welcome`（账户管理侧栏，主区空白）。

**原因**: 自动化下 cpquery 白屏后改开**无回调**的 `TYSF_LOGIN_URL`，SSO 默认回统一门户，不是审查信息查询。

**修复**: 改开带 `redirectUrl` 的 tysf；登完若非 cpquery 则 **主动 goto CPQUERY_INDEX**（cookie 已有，等同手工再访 cpquery）。

---

**现象**: 横幅「Chrome 正受到自动测试软件的控制」+ index 全白，无登录表单。

**原因**: 新流程先打开 cpquery 期望跳转登录；自动化下 cpquery SPA 直接白屏且不跳 tysf，旧代码还干等 30s。

**修复**: 2.5s 内无登录表单则立刻改开 `TYSF_LOGIN_URL`；去掉 `--enable-automation`；`force_login` 不用旧 cookie/持久化 profile。

---

**流程**: 回跳 cpquery → **等渲染** → 弹窗「使用说明」点「确定」→ 再点「复审无效审查决定查询」。

**修复**: 加强 `dismiss_cpquery_usage_dialog`（等弹窗/点确定/等消失）；`cnipa_sso_login` 与 `goto_cpquery_search_page` 按此顺序执行。不连测登录。

---

## 问题24：滑块已通过但 cpquery 白屏（2026-08-28）

**现象**: 拖拽正确 → URL 到 `cpquery.../chinesepatent/index` → 整页空白；日志 `body=''`。

**根因**:
1. 旧流程直接打开 `TYSF_LOGIN_URL`，登录无业务站 returnUrl/ticket。
2. 滑块通过后立刻 `page.goto(CPQUERY_INDEX)`，打断 SSO 回跳换票 → 白屏。
3. 曾把「空 body + 无密码框」误判为登录成功（已撤销）。

**修复**（只改代码，不再连测）:
- 从 `CPQUERY_INDEX` 进入再登；
- 滑块后等自然回跳 cpquery，禁止强跳 index；
- 空白页不算成功。

**关键文件**: `python/cnipa_common.py`

---

## 问题23：缺口主方法应找「镂空白描边」而非面积匹配（2026-08-28）

**实测**: 真缺口 bright contour `fill≈0.03`（细白环）；实心亮斑 fill 高。拼图竖条 Otsu bbox 常拉长，按面积比对会落空，回退到错误的 white-ring/列梯度。

**修复**: 主方法按 bbox 尺寸+镂空度(fill<0.35)+内部暗度选轮廓；Otsu 阈值降到 0.08；白环 conf 下限 0.25。

---

**位置**: `_detect_yidun_gap_offset` + `human_drag_align_piece`

**实测**:
- Otsu ok；Canny `conf=inf` → 列梯度 `gap=283`
- 闭环：`offset=282.5 target=282.3 err=-0.2`（映射已正确）
- 仍「验证失败」→ 对齐到了错误缺口

**根因**: `matchTemplate(..., mask=border_ring)` 在纹理边缘模板上易出 `inf`；列梯度不可靠。

**修复**: 模板改为 Otsu 形状轮廓；环乘到模板后裸 match；增加 silhouette 最暗滑窗；列梯度仅作最后回退。

---

## 问题21：绿框看起来准却从未过关——根因是距离映射，不是再调缺口算法

**位置**: `handle_cnipa_verify_captcha()` 的 `drag = gap_css * scale`

**问题描述**:
Otsu 遮罩与绿框已可做到「差几像素」的视觉对齐，但多次实测**一次都没通过**。继续改 kernel/Canny/重试属于瞎蒙。

真正问题：验证看的是**拼图块最终 left**，不是截图绿框。旧逻辑用
`scale = travel / (panel_w - piece_w)`（约 1.067）把 gap 换成鼠标行程；若控件实际是 1:1（`piece.left ≈ mouseΔ`）或比例不同，会系统性偏几到十几 px，叠加检测残余误差 → 永远卡在容差外。随机 ±1px 与多次重试无法消掉这个模型误差。

**修复方案**:
新增 `human_drag_align_piece()`：按住滑块后，以 `piece.x - panel.x == gap_css` 为目标，粗拖 + 读 DOM 闭环微调（阻尼步进），**废弃 scale 蒙距离**。缺口算法只负责给出 `gap_css`；是否到位由拼图实时位置裁定。

**关键文件**: `python/src/utils/core.py`

---

## 问题1：human_drag 拖动轨迹过于线性

**位置**: `human_drag()` 函数（原第30-55行）

**问题描述**:
原代码使用 `page.mouse.move(end_x, end_y, steps=steps)` 进行拖动，Playwright 的 `mouse.move` 带 `steps` 参数时是**匀速直线**移动。这种轨迹特征极易被反爬系统识别为机器人行为，导致验证失败。

**修复方案**:
改为自定义非线性轨迹，使用 cubic ease-out 曲线（先快后慢）模拟真人拖动特征：
- 每一步按 `ease = 1 - (1 - t) ** 3` 计算位置
- 每步添加 ±1.2px 的随机 X 抖动和 ±0.6px 的 Y 抖动
- 延迟时间随进度递增（前快后慢）：`base_delay = 0.003 + 0.015 * t`

---

## 问题2：_check_captcha_success 失败判断选择器不可靠

**位置**: `_check_captcha_success()` 函数（原第420-426行）

**问题描述**:
原代码使用 `:has-text("验证失败")` 等 Playwright 伪类选择器作为 `wait_for_selector` 的参数。`:has-text()` 是 Playwright 特有的伪类，直接作为 CSS 选择器传入 `wait_for_selector` 可能匹配到不可预期的元素或抛出异常，导致失败判断不准确。

**修复方案**:
改用 `page.locator(f'text={fail_text}')` + `is_visible(timeout=800)` 的方式检测失败提示文本，语义更明确、更可靠。同时将检测文本调整为"验证失败"、"请重试"、"拖动失败"。

---

## 问题3：重试循环中未等待 DOM 稳定

**位置**: `handle_slider_captcha()` 重试循环（原第339-343行）

**问题描述**:
每次重试时直接获取 `slider_button.bounding_box()`，但页面 DOM 可能仍在动画或刷新中，获取到的坐标可能是动画中间状态，导致拖动起点不准确。

**修复方案**:
在每次重试循环开始时增加 `time.sleep(0.5)` 等待 DOM 稳定后再获取 bounding box。

---

## 问题4：拼图刷新顺序错误（关键 Bug）

**位置**: `handle_slider_captcha()` 重试逻辑（原第375-390行）

**问题描述**:
原代码在重试时**先检测缺口位置，再点刷新按钮**。但点击刷新后验证码图片会更换，缺口位置完全改变，导致之前检测的 `puzzle_offset` 失效，后续拖动必然失败。这是导致验证反复失败的**最关键 Bug**。

**修复方案**:
调整顺序为：先 `_click_refresh_captcha(page)` → 等待 1.2 秒 → 再 `_get_yidun_images()` 获取新图片 → 再 `_detect_yidun_gap_offset()` 检测新缺口位置。确保每次拖动使用的都是当前验证码的缺口数据。

---

## 问题5：缺口检测搜索范围和精度不足

**位置**: `_detect_yidun_gap_offset()` 函数（原第77-86行）

**问题描述**:
1. 搜索起点从 `x=0` 开始，但实际拼图缺口一般不会在背景图最左侧（滑块起始位置附近），从 0 开始可能把拼图本身和背景左侧区域错误匹配。
2. `step=2` 只检查偶数位置，精度为 ±2px，对高精度拼图验证码可能不够准确。

**修复方案**:
改为两阶段搜索：
1. **粗搜**：`step=2`，范围从 `pw`（拼图宽度）到 `bw - pw`，快速定位大致区域
2. **精搜**：在粗搜结果 ±2 像素范围内逐像素（`step=1`）精确搜索，确保亚像素级精度

---

## 问题6：handle_cnipa_verify_captcha scale 计算偏大

**位置**: `handle_cnipa_verify_captcha()` 函数（原第201-203行）

**问题描述**:
原代码 `scale = bar_box['width'] / panel_box['width']`，将滑块轨道全宽映射到面板宽度。但滑块按钮（`.verify-move-block`）本身有宽度，滑块左边缘起始于轨道左端，右边缘最多到轨道右端，**实际可移动行程 = `bar.width - move.width`**，而非 `bar.width`。用 `bar.width` 计算 scale 会导致拖动距离偏大，滑块拖过头。

**修复方案**:
```python
travel = bar_box['width'] - move_box['width']
scale = travel / panel_w
```

---

## 问题7：handle_cnipa_verify_captcha 失败刷新后未充分等待新图加载

**位置**: `handle_cnipa_verify_captcha()` 失败重试分支（原第216-217行）

**问题描述**:
拖动失败后调用 `_click_verify_refresh(page)` 刷新验证码，但只等 0.8 秒就进入下一轮循环截图。新验证码图片可能尚未加载完成，导致截图仍是旧图，缺口检测结果是旧位置，拖动必然失败。

**修复方案**:
刷新后等待时间从 0.8 秒增加到 1.5 秒，确保新图片加载完成后再截图检测。

---

## 问题8：_verify_captcha_passed 判断过严

**位置**: `_verify_captcha_passed()` 函数（原第243-259行）

**问题描述**:
原逻辑在以下情况会误判为失败：
- 拖完后滑块（`.verify-move-block`）仍然可见（页面动画尚未完成）
- `.verify-msg` 元素不存在或文案尚未更新（仍显示"向右滑动…"等初始文案）
- 此时函数直接 `return False`，导致实际可能成功的验证被误判为失败，触发不必要的刷新重试

**修复方案**:
重写判断逻辑，增加多层检测：
1. 先等 2 秒观察滑块是否消失（`wait_for_element_state('hidden')`）
2. 检查 `.verify-msg` 文案（成功/失败关键词）
3. 检查 `.verify-img-panel` 面板是否已淡出
4. 检查 verify 容器是否有 `success`/`pass` 样式类
5. 以上均无法确认时才返回 False 让外层重试

---

## 问题9：gap_x is None 分支刷新后缺少等待

**位置**: `handle_cnipa_verify_captcha()` 缺口识别失败分支（原第189-192行）

**问题描述**:
`gap_x is None` 时调用 `_click_verify_refresh(page)` 后直接 `continue`，没有等待新图加载。与问题7 属同一类遗漏——刷新后新验证码图片尚未加载，下一轮循环截图拿到的仍是旧图，缺口检测大概率再次失败，白白浪费一次重试机会。

**修复方案**:
在 `_click_verify_refresh(page)` 后补 `time.sleep(1.5)`，与拖动失败刷新后的等待时间保持一致。

---

## 问题10：异常分支刷新后等待时间与其他分支不一致

**位置**: `handle_cnipa_verify_captcha()` 异常处理分支（原第224-227行）

**问题描述**:
异常分支 `_click_verify_refresh(page)` 后只 `sleep(0.8)`，与问题7 修复后的拖动失败分支（1.5s）和问题9 修复后的缺口识别失败分支（1.5s）不一致。异常后同样需要重新加载验证码图片，0.8s 不足会导致下一轮截图拿到旧图。

**修复方案**:
将异常分支的 `time.sleep(0.8)` 改为 `time.sleep(1.5)`，确保三个刷新分支（正常失败/缺口识别失败/异常）的等待时间统一为 1.5s。

---

## 问题11：human_drag 拖动轨迹不符合真人行为（测试暴露）

**位置**: `human_drag()` 函数（原第48-69行）

**问题描述**:
实际测试发现拖动距离不稳定（有时不够、有时过头），原因：
1. 原方案用 ease-out + 随机抖动，但接近目标时仍有抖动，导致最终位置不精确
2. 真人拖动滑块时，要么"拖过头再回拉"，要么"快到目标时减速精确到位"，而不是在目标附近无效抖动

**修复方案（过冲回拉策略）**:
改为三阶段拖动，模拟真人行为：
1. **阶段1（快速拖动）**：拖到目标 80% 距离，加速曲线 `t^1.5`，模拟真人快速拖动
2. **阶段2（过冲）**：从阶段1终点连续过渡到 overshoot 位置（3-8px），模拟真人拖过头
3. **阶段3（回拉）**：减速回拉到精确目标位置，模拟真人发现过头后往回拖
4. 最后精确到位后松手

**阶段2公式修复**：原公式 `start_x + (overshoot_x - start_x) * ratio` 与阶段1终点不连续，改为 `phase1_end_x + (overshoot_x - phase1_end_x) * t`，确保轨迹连续无跳跃。

这样更符合真人拖动滑块的行为模式，避免无效抖动。

---

## 问题12：devicePixelRatio 未计算，高 DPI 屏幕距离翻倍（测试暴露，关键 Bug）

**位置**: `handle_cnipa_verify_captcha()` scale 计算（原第203-209行）

**问题描述**:
`screenshot()` 返回的是**设备像素**（如 Retina 2x 屏幕截图像素是 CSS 像素的 2 倍），但 `bounding_box()` 返回的是 **CSS 像素**。`gap_x` 从截图里检测出来是设备像素，直接当 CSS 像素用会导致拖动距离偏大 2 倍（或 1.5 倍、3 倍，取决于屏幕 DPI）。

这是测试中"第2次超过了缺口所在位置才停"的主要原因。

**修复方案**:
```python
from PIL import Image as _Img
_panel_img = _Img.open(io.BytesIO(bg_bytes))
dpr = _panel_img.width / panel_w  # 设备像素比
gap_css = gap_x / dpr             # 截图像素 → CSS 像素
drag = gap_css * scale + random.uniform(-1, 1)
```

---

## 问题13：drag 计算随机偏移量过大

**位置**: `handle_cnipa_verify_captcha()` drag 计算（原第209行）

**问题描述**:
`drag = gap_x * scale + random.uniform(-2, 3)` 在已经存在 DPI 偏差的基础上，又叠加了 -2~+3px 的随机偏移。当 DPI 偏差已经导致距离偏大 50px 时，再加 3px 影响不大；但当 DPI 修正后，这 ±2~3px 的随机偏移相对目标精度（±2px）来说就太大了。

**修复方案**:
DPI 修正后，随机偏移缩小到 `random.uniform(-1, 1)`，确保总偏差控制在 ±1px 以内。

---

## 问题14：缺口检测算法受左侧拼图块干扰（测试暴露，第1/3次"不到就松"主因）

**位置**: `_detect_yidun_gap_offset()` 函数

**问题描述**:
实测第1/3次"不到就松"的根因：verify 路径用 `.verify-img-panel` 面板截图做缺口匹配，但面板背景里常叠着左侧已就位的拼图块（就是那个要拖动的拼图在原位的影子）。用像素 diff 做模板匹配时，左侧拼图块区域的高 diff 会干扰匹配结果，导致 `gap_x` 偏小，拖动距离不够。

**修复方案（边缘检测法）**:
1. **主方法：边缘检测** - 缺口有明显边缘特征（深色边框），用 `ImageFilter.FIND_EDGES` 做边缘检测后匹配，比像素 diff 对拼图块叠加干扰更鲁棒
2. **搜索范围**：从 `pw`（拼图宽度）开始搜索，跳过左侧拼图块可能存在的区域
3. **回退机制**：如果边缘检测置信度低于 0.3，回退到像素 diff 法
4. **精搜微调**：边缘检测成功后在 ±3 像素范围内精搜

---

## 问题15：缺口检测算法不稳定（第2轮实测暴露，三轮迭代）

**位置**: `_detect_yidun_gap_offset()` 函数

**问题描述**:
第2轮实测3次全部失败，日志：dpr=1.00；第1次 gap=263→drag=237（拖过头太多），第2次 gap=61→drag=56（几乎没动），第3次 gap=145→drag=131（接近但差5mm）。

**迭代过程**:
1. **V1 边缘检测法（PIL FIND_EDGES）**：置信度常<0.3，回退到像素diff，结果不稳定
2. **V2 列梯度法**：只分析背景图垂直梯度，不依赖拼图块图像。但背景图本身可能有强烈垂直线条（建筑物、树干等），产生假峰
3. **V3 OpenCV Canny + matchTemplate**（最终方案）：网上成熟方案，已验证于QQ空间/腾讯滑块等多种验证码

**最终修复方案（V3: Canny + matchTemplate）**:
1. **主方法：cv2.Canny + cv2.matchTemplate**
   - 转灰度 → Canny边缘检测（阈值100/200）
   - 用拼图块alpha通道遮罩，只保留拼图形状内的边缘
   - **关键**：遮罩背景图左侧拼图块原始位置（`bg_edges[:, :search_start] = 0`），避免匹配到原位
   - `cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED, mask=alpha_mask)`
   - 置信度阈值 ≥0.25 认为有效
2. **回退方法：列梯度法**（仅当Canny置信度 <0.25 时使用）
   - 计算背景图垂直梯度，找缺口位置

**参考来源**:
- 脚本之家：`opencv+playwright滑动验证码的实现`
- 博客园：`playwright--自动化（二）：过滑块验证码验证码缺口识别`
- GitHub：`Tencent-Slider-Passer-Playwright`

---

## 问题16：requirements.txt 缺 opencv-python 依赖

**位置**: `python/requirements.txt`

**问题描述**:
问题15引入了 OpenCV（cv2），但 requirements.txt 未声明 `opencv-python` 依赖。换机器部署时会 ImportError。

**修复方案**:
添加 `opencv-python>=4.8.0` 到 requirements.txt。

---

## 问题17：scale 计算系统性偏短（第2轮第3次"差5mm"根因）

**位置**: `handle_cnipa_verify_captcha()` scale 计算

**问题描述**:
第2轮第3次 gap=145→drag=131（×0.905），正好"差约5mm"。原因：scale 公式 `travel / panel_w` 的分母偏大。

拼图块有宽度（piece_w），缺口位置的有效范围是 0 到 `(panel_w - piece_w)`，而不是 0 到 `panel_w`。用 `panel_w` 做分母导致 scale 偏小约 10%。

**修复方案**:
```python
_piece_img = _Img.open(io.BytesIO(piece_bytes))  # 必须用拼图截图，不是面板图
piece_w = _piece_img.width / dpr
effective_range = panel_w - piece_w
scale = travel / effective_range
```

这样 scale 会略大（约 1/0.9 ≈ 1.11 倍），补偿之前的系统性偏短。

**注意**：早期误用 `_panel_img.width / dpr` 会使 `piece_w≈panel_w`、`eff_range≈0`，从而退回 `scale=1`，公式实际失效。

---

## 修复优先级总结（含八轮审核 + 三轮实测反馈）

| 优先级 | 问题 | 路径 | 影响 |
|--------|------|------|------|
| P0 | **问题42：v5 登录流程精简 + 三域 cookie 快照诊断** | login | **门户绕行不可靠；无诊断手段定位 SSO 断裂点** |
| P0 | **问题41：单方法检测不可靠 + 精修过度 + 松手过快 + SSO白屏** | verify+login | **n=1 时 gap 偏 10-60px；精修移过头；松手期<100ms 被识别为机器人** |
| P0 | **问题40：content_offset 不一致 + 模板匹配偏左** | verify | **"差一点"根因：闭环对齐到错误目标 + 亮度剖面精修** |
| P0 | **问题39：Otsu 轮廓 fill 阈值 0.45 过严（应为 0.55）** | verify | **内容框落入空白区→gap_x 偏 57px→"差几厘米"根因** |
| P0 | **问题20：matchTemplate 内部纹理噪声导致 gap_x 偏大** | verify | **"超过缺口2-3cm"根因，改用边框环形遮罩** |
| P0 | 问题15：缺口检测算法不稳定（已改用Canny+matchTemplate） | verify | 3次全失败→改用OpenCV成熟方案 |
| P0 | 问题17：scale 计算系统性偏短 | verify | "差5mm"根因，改用 travel/(panel_w-piece_w) |
| P0 | 问题12：devicePixelRatio 未计算 | verify | 高 DPI 距离翻倍（本机 dpr=1.0 无影响） |
| P0 | 问题4：刷新顺序错误 | 易盾 | 直接导致每次重试必然失败 |
| P0 | 问题6：scale 计算偏大 | verify | 拖动距离偏大，滑块拖过头（已被问题17修正） |
| P1 | 问题11：human_drag 拖动轨迹不符合真人行为 | 通用 | 改为过冲回拉策略，阶段2公式已修复连续性 |
| P1 | 问题1：线性拖动轨迹 | 通用 | 易被反爬系统识别为机器人 |
| P1 | 问题8：验证通过判断过严 | verify | 成功时误判为失败，触发不必要重试 |
| P2 | 问题16：requirements.txt 缺 opencv-python | 部署 | 换机器会 ImportError |
| P2 | 问题14：缺口检测受左侧拼图块干扰（已升级为问题15） | verify | 最终改用Canny+matchTemplate |
| P2 | 问题13：drag 随机偏移过大 | verify | DPI 修正后 ±1px |
| P2 | 问题7/9/10：刷新等待时间 | verify | 三个分支统一 1.5s |
| P3 | 问题2：失败判断选择器 | 易盾 | 可能导致成功/失败状态误判 |
| P4 | 问题5：缺口检测精度 | 通用 | 影响拖动距离计算准确性 |
| P5 | 问题3：DOM 稳定等待 | 易盾 | 影响重试时坐标获取准确性 |

---

## 实测记录

### 第1轮（2026-08-28）

**测试现象**:
- 第1次：没拖到缺口位置就松手 → 失败
- 第2次：超过了缺口位置才停 → 失败
- 第3次：没拖到缺口位置就松手 → 失败

**根因分析**:
1. **距离不稳定（第1/3次不够，第2次过头）**：`human_drag` 最后一步有 ±1.2px 抖动 + 微调过冲 1~3px
2. **整体偏大（第2次明显过头）**：`screenshot()` 设备像素 vs `bounding_box()` CSS 像素，DPR 未换算
3. **随机偏移叠加**：`random.uniform(-2, 3)` 在 DPI 偏差上又加了不确定性

### 第2轮（2026-08-28，修复问题11-14后）

**测试现象**:
- 第1次：拖动非常快，明显拖过头太多（缺口在中间，几乎拖到最后边）→ gap=263→drag=237
- 第2次：拖动非常小，只拖动了窗口的十分之一 → gap=61→drag=56
- 第3次：几乎快到缺口位置，速度也慢下来了，还差5毫米左右 → gap=145→drag=131

**日志**: dpr=1.00（本机非高DPI）；边缘法置信度常<0.3 回退到像素diff；失败后日志仍写"边缘法"（标签bug）

**根因分析**:
1. **边缘检测法的根本缺陷**：`.verify-sub-block` 截图包含背景内容（不是纯拼图形状），导致边缘匹配到的是背景纹理而非缺口
2. **第2次 gap≈61 接近拼图宽度**：匹配到了拼图块截图位置附近的背景特征，是典型的左侧干扰
3. **第1次 gap=263 过大**：匹配到了背景右侧某个纹理特征
4. **第3次 gap=145 最接近正确**：说明算法偶尔能正确检测，但不稳定

**修复方向**: 放弃自研算法，改用网上成熟方案 OpenCV Canny + matchTemplate（问题15 V3）

### 第3轮（2026-08-28，修复问题15-17后）

**测试现象**:
- 第1次 gap=0, conf=inf → 几乎没拖（匹配异常）
- 第2次 gap=140, drag=148.9 → 距离看起来合理，仍失败（多约2cm）
- 第3次 gap=311（列梯度法）, drag=330.2 → 明显过头

**日志**: scale≈1.067, piece_w≈60.8（问题17已生效）

**根因分析**:
1. **conf=inf 异常**：第1次 max_val=inf 且 max_loc[0]=0 被当成成功返回
2. **overshoot 机制导致"多2cm"**：阶段2过冲 3-8px，如果 gap 已经偏大，再加 overshoot 就更多

**修复方案**:
1. 过滤 inf/nan 和 gap_x < search_start 的异常值
2. 简化 human_drag，去掉 overshoot 机制，直接 ease-out 拖到目标
3. 加调试截图 debug_gap_detection.png，验证 gap 检测准确性

---

## 问题18：overshoot 机制导致"每次都多2cm"

**位置**: `human_drag()` 函数

**问题描述**:
第3轮实测第2次 gap=140, drag=148.9，肉眼观察"差不多但多了2cm"。原因：阶段2过冲 3-8px 叠加 gap 检测偏大，导致总共多了约 20px。

**修复方案**:
简化 human_drag，去掉阶段2过冲和阶段3回拉，直接 ease-out 拖到目标位置。先验证 gap 检测和 scale 计算是否正确，后续再加拟人化。

---

## 问题19：conf=inf 异常值未过滤

**位置**: `_detect_yidun_gap_offset()` 函数

**问题描述**:
第3轮第1次 conf=inf 且 gap_x=0 被当成成功返回，导致几乎没拖动。

**修复方案**:
```python
# TM_CCOEFF_NORMED 正常应在 [-1, 1]；拒 inf/nan 与 >1
if max_loc[0] >= search_start and 0.25 <= max_val <= 1.0 and max_val == max_val:
    gap_x = max_loc[0]
    return gap_x
```

调试截图：RGBA→BGR 后写入 `python/output/debug_gap_detection.png`（绿框=检测到的缺口）。

---

## 问题20：matchTemplate 内部纹理噪声导致 gap_x 系统性偏大（第3轮实测根因）

**位置**: `_detect_yidun_gap_offset()` 函数，matchTemplate 遮罩

**问题描述**:
第3轮实测（修复问题15-19后）3次全部超过缺口 2-3cm。原因：

`.verify-sub-block` 截图包含的是拼图块**原始位置**的背景纹理（因为拼图块是从背景中"抠出来"的），内部有建筑/树木/纹理等边缘。而缺口处是阴影/暗色区域，内部边缘完全不同。

使用完整 alpha 遮罩（整个拼图形状）时，`matchTemplate` 同时匹配了轮廓边缘和内部纹理边缘。内部纹理边缘与缺口不匹配，产生噪声把匹配位置推偏（偏右），导致 `gap_x` 系统性偏大。

**修复方案（边框环形遮罩 border ring mask + Otsu 阈值分割）**:

由于 Playwright 截图无透明通道（alpha 全为 255），且背景复杂导致 Canny 轮廓提取失败，改用 Otsu 自动阈值分割提取拼图前景：

```python
# 步骤1：Otsu 阈值分割提取拼图前景（不依赖 alpha/轮廓）
_, otsu_mask = cv2.threshold(piece_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

# 极性校正：拼图块通常比背景暗，THRESH_BINARY 会把亮背景当白色前景。
# 通过检查四角像素判断：角点通常是背景，如果角点为白(>127)说明极性反了，需要翻转。
corner_brightness = (
    int(otsu_mask[0, 0]) + int(otsu_mask[0, -1])
    + int(otsu_mask[-1, 0]) + int(otsu_mask[-1, -1])
) // 4
if corner_brightness > 127:
    otsu_mask = cv2.bitwise_not(otsu_mask)

# 验证 Otsu 结果合理性：前景面积占比应在 20%-95% 之间
otsu_fg_ratio = cv2.countNonZero(otsu_mask) / (pw * ph) if (pw * ph) > 0 else 0
if 0.2 <= otsu_fg_ratio <= 0.95:
    alpha_mask = otsu_mask  # 用 Otsu 遮罩替代 alpha 遮罩

# 步骤2：基于实际形状的边框环形遮罩
kernel_size = max(3, int(min(pw, ph) * 0.08))
kernel = np.ones((kernel_size, kernel_size), np.uint8)
eroded = cv2.erode(alpha_mask, kernel)   # 腐蚀：直接缩小形状
border_ring = cv2.subtract(alpha_mask, eroded)  # 差集 = 轮廓环形

# 只用轮廓边缘做匹配，消除内部纹理干扰
result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED, mask=border_ring)
```

**原理**：
- Otsu 算法自动找到最佳阈值，将图像分为前景（拼图块）和背景
- 拼图块与白色/浅色背景有明显亮度差异，Otsu 可有效分离
- 用前景面积比（0.2-0.95）验证 Otsu 结果是否合理
- 腐蚀（erode）直接缩小形状，不要 dilate（膨胀）
- 原形状减去腐蚀结果 = 只保留轮廓边框（环形区域）
- 注意：erode→dilate 是开运算，几乎还原原形状，差集像素极少，会触发回退
- 匹配时只比较轮廓位置的边缘，不受内部纹理影响
- 缺口的轮廓边缘与拼图轮廓边缘形状一致，匹配更精准

**调试增强**：
- 日志输出 `mask_type`（otsu vs alpha）、`border_ring` 像素数和 `kernel_size`
- 保存 `debug_border_ring.png` 便于验证遮罩形状
- 新增 `debug_piece_mask.png` 显示实际使用的遮罩（Otsu 二值化 vs alpha）
- 调试截图增加红线标注搜索起点

**已知残留风险**：
- 如果拼图块截图背景与拼图亮度接近，Otsu 可能无法正确分离
- 前景面积比验证（0.2-0.95）可能过滤掉某些特殊拼图形状
- 备选方案：若 Otsu 失败，可尝试自适应阈值（adaptiveThreshold）或 GrabCut 算法

---

> **~~问题 36/37/38 旧内容已合并到文件顶部的问题 38（最终版）~~**

---

## 问题 43：cpquery 自有 CAS 登录表单直接提交（绕过门户 SSO）

**状态**：已实施修复，待测试

**根因分析**（基于 output2.txt + 诊断文件）：
1. cpquery 返回 HTTP 400 + 完整 CAS 登录表单（30,827 字符），form action = `/chinesepatent/index`
2. 表单字段：username, password, execution（Spring Web Flow 令牌）, ticket（空）
3. 提交按钮为 `<input type="submit">`（非 `<button>`），旧代码 `_click_sso_submit_button()` 无法匹配
4. 旧代码 fallback 到 Enter 提交 → JS 拦截 → 重定向到 AM 登录 → 门户 → cpquery 无会话
5. 门户 `isAuth` 返回 UUID token，但不为 cpquery 创建会话 cookie
6. cpquery 域始终无 cookie（只有 2 个跟踪 cookie `dX1xbeyMT58WO/WP`）

**诊断证据**：
- `cpquery_resp_400.html`：HTTP 400 + 30,827 字符 CAS 表单
- `cpquery_response_chain.txt`：仅 1 个响应（400），无重定向
- `portal_isAuth.json`：`isAuth: true` + UUID token（与 cpquery 无关）
- `diagnostic_09_final_fail.json`：cpquery 域 cookie 为空

**修复方案**：
- 新增 `_is_cpquery_own_cas_form()`：检测 form action 包含 `/chinesepatent/`
- 新增 `_submit_cpquery_cas_form()`：使用 `form.submit()` 绕过 JS 点击拦截器
- 修改 `_click_sso_submit_button()`：增加 `input[type="submit"]` 选择器 + Enter fallback
- 修改 `cnipa_sso_login()`：检测 cpquery CAS 表单 → 直接 form.submit() → 检查会话
- 增强响应拦截器：捕获 cpquery 表单提交后的实际响应（写入 `cpquery_form_resp_*.html`）

**待验证**：
- form.submit() 是否能绕过 JS 拦截直接 POST 到 cpquery
- cpquery 服务器接收 POST 后的实际响应（设置 cookie? 重定向? 返回错误?）
- 如果 cpquery 仍重定向到 AM → 需要进一步分析 cpquery 的认证架构
