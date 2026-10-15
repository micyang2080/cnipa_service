# Slider Captcha (滑块验证码) 开源解决方案调研报告

## 概述

本报告基于当前工作空间的实现代码和文档,总结了滑块验证码的核心技术点和成熟方案。

---

## 一、核心挑战与解决方案

### 1. 缺口检测 (Gap Detection)

#### 主要算法

**1.1 Canny 边缘检测 + 模板匹配 (主流方案)**

```python
# 从当前项目 python/src/utils/core.py
bg_edges = cv2.Canny(bg_blur, 60, 140)
piece_edges = cv2.Canny(content_mask, 40, 120)

# 用拼图块边缘作为模板匹配
res = cv2.matchTemplate(bg_edges, tmpl, cv2.TM_CCOEFF_NORMED)
_, mv, _, ml = cv2.minMaxLoc(res)
gap_x = ml[0]
```

**优势:**
- 对颜色变化不敏感
- 边缘特征稳定
- 抗背景纹理干扰

**1.2 Otsu 阈值分割 + 轮廓检测**

```python
# 提取拼图块内容区域
_, otsu = cv2.threshold(piece_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

# 极性校正:拼图块通常比背景暗
if (int(otsu[0, 0]) + int(otsu[0, -1]) + int(otsu[-1, 0]) + int(otsu[-1, -1])) // 4 > 127:
    otsu = cv2.bitwise_not(otsu)

# 轮廓检测
cnts, _ = cv2.findContours(otsu, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
```

**1.3 相对暗洞检测 (Shape-Hole)**

```python
# 邻域模糊 - 原图:相对更暗处为正
blur = cv2.GaussianBlur(bg_gray, (41, 41), 0)
hole_map = cv2.subtract(blur, bg_gray).astype(np.float32)

# 用拼图 mask 做模板匹配
res_h = cv2.matchTemplate(hole_map, tmpl_f, cv2.TM_CCORR_NORMED)
```

**1.4 亚像素精修 (抛物线插值)**

```python
# 从 puzzle-slider-captcha (PyPI) 参考
if 1 <= xx < res_h.shape[1] - 1:
    left_v = float(res_h[yy, xx - 1])
    center_v = float(res_h[yy, xx])
    right_v = float(res_h[yy, xx + 1])
    denom = left_v - 2.0 * center_v + right_v
    if abs(denom) > 1e-6:
        sub_px = (left_v - right_v) / (2.0 * denom)
        if abs(sub_px) < 1.0:
            xx_f = xx + sub_px  # 亚像素精度
```

---

### 2. 拼图块内容提取 (Piece Content Extraction)

#### 关键问题

拼图块截图 (`.verify-sub-block`) 通常包含大量白色 padding,实际 jigsaw 内容只占元素的一部分。

#### 解决方案

**2.1 Alpha 通道法 (透明截图有效时)**

```python
# 检测 alpha 通道是否有效
alpha = arr[:, :, 3]
alpha_mean = float(alpha.mean())
is_opaque = alpha_mean > 250

if not is_opaque:
    # 找第一个不透明列
    for x in range(w):
        col = alpha[:, x]
        opaque_ratio = float(np.count_nonzero(col > 30)) / h
        if opaque_ratio > 0.08:
            return x  # 内容左边缘
```

**2.2 Canny 边缘密度法 (不透明截图主方法)**

```python
edges = cv2.Canny(gray_u8, 50, 150)
edge_density = edges.sum(axis=0) / (255.0 * h)  # 每列边缘比例

# 滑动窗口平滑
kernel_w = 5
kernel = np.ones(kernel_w) / kernel_w
edge_smooth = np.convolve(edge_density, kernel, mode='same')

# 找第一个边缘密度显著升高的位置
baseline_end = max(min_x + 1, int(w * 0.10))
baseline = float(edge_smooth[min_x:baseline_end].mean())
threshold = max(baseline * 3.0, 0.03)

for x in range(min_x, w):
    if edge_smooth[x] > threshold:
        return x  # 内容左边缘
```

**2.3 列灰度方差法 (纹理 vs 纯色)**

```python
for x in range(min_x, w):
    col = gray[:, x]
    cur_std = float(np.std(col))
    if cur_std > 8.0 and cur_std > prev_std * 2.5 and cur_std > 5.0:
        return x  # 纹理区域开始
    prev_std = max(prev_std * 0.9, cur_std)
```

**2.4 Otsu + 轮廓检测 (综合方案)**

```python
# 多阈值尝试
p90 = float(np.percentile(piece_gray, 90))
for thr in (max(160.0, p90 - 20), p90, min(250.0, p90 + 15), 200.0, 230.0, 240.0):
    bright_pc = cv2.threshold(piece_gray, int(thr), 255, cv2.THRESH_BINARY)[1]
    cnts, _ = cv2.findContours(bright_pc, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        # 形状约束
        if not (35 <= w <= 95 and 35 <= h <= 95):
            continue
        ar = w / max(h, 1)
        if not (0.65 <= ar <= 1.5):
            continue

        area = float(cv2.contourArea(c))
        fill = area / float(w * h)
        # 优先白描边环 (fill 低),拒绝大实心块
        if fill > 0.55 or fill < 0.05:
            continue

        sc = area / (1.0 + abs(1.0 - ar)) * (2.5 if fill < 0.28 else 0.6)
        # 拼图块通常在竖条中上部
        if y > ph * 0.55:
            sc *= 0.25

        if sc > best_sc:
            best_sc, best_pc = sc, (x, y, w, h)
```

---

### 3. 拖拽精度控制 (Drag Precision)

#### 3.1 开环粗拖 + 闭环微调

```python
def human_drag_align_piece(page, move, piece, panel, gap_css, tol=2.0, content_offset_css=0.0):
    # 1. 开环:拖到 (目标 − 12px),宁可短勿过
    margin = 12.0
    coarse_drag = max(80.0, need - margin)
    _drag_to(page, start_x, start_y, start_x + coarse_drag, start_y, steps=25)

    # 2. 闭环(不松鼠标):读拼图实时位置,欠拖小步补、过冲回拉
    for loop in range(2):
        cur_offset = _read_piece_offset(page, panel, piece, content_offset_css=content_offset_css)
        err = gap_css - cur_offset

        if abs(err) <= tol:
            break

        if err > 0:
            # 欠拖:保守前补,避免过冲
            correction = min(err, 10.0) * 0.85
        else:
            # 过冲:允许回拉
            correction = max(err, -15.0)

        _micro_drag(page, correction, start_y)
```

#### 3.2 实时位置读取

```python
def _read_piece_offset(page, panel, piece, content_offset_css=0.0):
    offset = page.evaluate(
        """([panelSel, pieceSel]) => {
            const p = document.querySelector(panelSel);
            const s = document.querySelector(pieceSel);
            if (!p || !s) return null;
            return s.getBoundingClientRect().left - p.getBoundingClientRect().left;
        }""",
        ['.verify-img-panel', '.verify-sub-block'],
    )
    return float(offset) + content_offset_css
```

#### 3.3 拟人化拖动曲线

```python
def _drag_to(page, start_x, start_y, end_x, end_y, steps=25):
    for i in range(steps):
        t = (i + 1) / steps
        # ease-out 曲线:先快后慢
        ease = 1 - (1 - t) ** 2
        cx = start_x + (end_x - start_x) * ease
        cy = start_y + (end_y - start_y) * ease
        page.mouse.move(cx, cy)
        # 延迟递增:前快后慢
        delay = 0.004 + 0.016 * t
        time.sleep(delay)
```

---

## 二、成熟开源项目参考

### 2.1 GitHub 项目

**1. mehul-08/slider-captcha-solver**
- 特点:先 resize 背景到渲染尺寸再匹配,避免坐标转换
- 参考:当前项目已采用此方案

**2. Tencent-Slider-Passer-Playwright**
- 特点:腾讯滑块验证码自动化
- 算法:Canny + matchTemplate
- 参考来源:脚本之家、博客园相关文章

**3. puzzle-slider-captcha (PyPI)**
- 特点:抛物线亚像素精修 matchTemplate 峰值
- 当前项目已集成此方案

### 2.2 技术文章参考

1. **脚本之家**: `opencv+playwright滑动验证码的实现`
2. **博客园**: `playwright--自动化(二):过滑块验证码验证码缺口识别`
3. **GitHub**: `Tencent-Slider-Passer-Playwright`

---

## 三、多方法交叉验证策略

### 3.1 四种检测方法

1. **shape-hole**: 形状约束的相对暗洞 (主方法)
2. **hollow**: 自适应高亮 → 镂空描边
3. **edge**: 形状边缘匹配
4. **rel-dark**: 相对暗洞检测

### 3.2 聚类评分

```python
# 按位置聚类
cluster_radius = 18
if bg_brightness < 100:
    cluster_radius = 22  # 暗背景下放宽

# 聚类评分
def _cluster_score(cl):
    wsum = sum(w for _, w, _, _ in cl)
    xbar = sum(x * w for x, w, _, _ in cl) / wsum
    methods = {m for _, _, m, _ in cl}
    bonus = 0.5 * max(0, len(methods) - 1)  # 多方法交叉验证加分

    if methods & {'hollow', 'edge', 'shape-hole'}:
        bonus += 1.2
    if 'shape-hole' in methods:
        bonus += 1.0
    if methods == {'rel-dark'}:
        bonus -= 0.5  # 仅 rel-dark 独赢降权

    return wsum + bonus, xbar, len(cl)
```

### 3.3 门禁机制

```python
# 单方法检测不可靠,拒绝拖动
if len(detect_methods) < 2:
    logger.warning('缺口检测仅 %s 独赢(无交叉验证),拒绝拖动', detect_methods)
    _click_verify_refresh(page)
    continue

# 暗背景下更严格
if bg_brightness < 100 and len(best_methods) == 1 and score < 8.0:
    logger.warning('暗背景下仅 %s 独赢 (score=%.1f<8.0),拒绝拖动', best_methods, score)
    return None
```

---

## 四、自适应策略

### 4.1 背景亮度自适应

```python
bg_brightness = float(bg_gray.mean())

if bg_brightness < 100:
    darkness = max(0.3, bg_brightness / 100.0)
    # 提升 edge/hollow 权重
    for x, w, m, meta in cands:
        if m in ('edge', 'hollow'):
            bright_cands.append((x, w / darkness, m, meta))
        elif m in ('shape-hole', 'rel-dark'):
            bright_cands.append((x, w * darkness, m, meta))
```

### 4.2 缺口左边缘精修

```python
# 从模板匹配峰值向左扫描,找亮度梯度最大处
scan_y_start = max(0, yy_out - 5)
scan_y_end = min(bh, yy_out + ch + 5)
strip = bg_f[scan_y_start:scan_y_end, :]
col_mean = strip.mean(axis=0)

# 在峰值附近 ±15px 范围内扫描
scan_left = max(search_start, gap_x - 15)
scan_right = min(bw - 1, gap_x + 15)

gradient = np.abs(np.diff(col_mean[scan_left:scan_right + 1]))
peak_grad = int(np.argmax(gradient)) + scan_left

# 验证:该处左侧应比右侧亮(缺口是暗区)
left_brightness = float(col_mean[max(0, peak_grad - 8):peak_grad + 1].mean())
right_brightness = float(col_mean[peak_grad:min(bw, peak_grad + 9)].mean())
brightness_drop = left_brightness - right_brightness

if brightness_drop > 8:
    shift = gap_x - peak_grad
    if 0 < shift <= 5:  # 仅允许向左微修 ≤5px
        gap_x_refined = peak_grad
```

---

## 五、失败处理与回退策略

### 5.1 拼图内容提取失败回退

```python
# 策略优先级:
# 1. Alpha 通道(透明截图)
# 2. Canny 边缘密度(不透明截图主方法)
# 3. 列灰度方差(纹理 vs 纯色)
# 4. 纯色检测(找第一个非白/非纯色列)
# 5. 默认返回 0(所有方法失败)
```

### 5.2 缺口检测失败处理

```python
# 1. 截图前隐藏拼图块,避免左侧浮块干扰
page.evaluate("""() => {
    const el = document.querySelector('.verify-sub-block');
    if (el) { el.style.visibility = 'hidden'; }
}""")
bg_bytes = page.locator('.verify-img-panel').screenshot()

# 2. 刷新后等待新图加载
_click_verify_refresh(page)
time.sleep(1.5)  # 充分等待

# 3. 门禁:单方法检测拒绝拖动
if len(detect_methods) < 2:
    continue
```

---

## 六、关键经验总结

### 6.1 参考点一致性

**核心问题**: 缺口检测的 `content_offset_x0` 与拖拽对齐的 `content_offset_css` 必须使用同一参考点。

**解决方案**:
```python
# 缺口检测时返回 content_offset_x0
gap_x, detect_methods, content_offset_x0 = _detect_puzzle_gap_offset(bg_bytes, piece_bytes)

# 拖拽时直接使用,不再独立检测
content_offset_px = content_offset_x0
content_offset_css = content_offset_px / dpr
```

### 6.2 截图时机

**关键**: 截图背景前必须隐藏拼图块,避免左侧浮块干扰缺口检测。

### 6.3 多方法交叉验证

**经验**: 单方法检测 `n=1` 时 gap 位置常偏 10-60px;`n≥2` 时多方法交叉验证,gap 位置可靠。

### 6.4 暗背景处理

**策略**: 暗背景下 `edge/hollow` 方法权重提升,`shape-hole/rel-dark` 方法权重降低。

---

## 七、推荐技术栈

### 7.1 核心技术

- **图像处理**: OpenCV (cv2)
- **图像加载**: PIL/Pillow
- **数值计算**: NumPy
- **浏览器自动化**: Playwright

### 7.2 算法组合

1. **缺口检测**: Canny + matchTemplate + Otsu 轮廓
2. **内容提取**: 多策略回退 (Alpha → 边缘密度 → 方差 → 纯色)
3. **拖拽控制**: 开环粗拖 + 闭环微调 + 拟人化曲线
4. **精度保障**: 多方法交叉验证 + 门禁机制

---

## 八、参考资料

1. **当前项目实现**: `python/src/utils/core.py`
2. **问题修复记录**: `python/src/utils/memory.md`
3. **GitHub 项目**:
   - mehul-08/slider-captcha-solver
   - Tencent-Slider-Passer-Playwright
4. **PyPI 包**: puzzle-slider-captcha
5. **技术文章**:
   - 脚本之家: opencv+playwright滑动验证码的实现
   - 博客园: playwright--自动化(二):过滑块验证码验证码缺口识别
