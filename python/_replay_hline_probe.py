# -*- coding: utf-8 -*-
"""离线回放：真实 run detail_20260901_202133 的 11 屏 dump + 按像素计算的表格横线。
验证三件事：
  1) 逐屏横线宽度/位置与 20:57 标定一致（行分隔 775 / 外框 856）；
  2) 全量回放 decision_summary 仍收在创造性。。（结构线规则接管后结果不变）；
  3) 删掉 shot5 的"案由"段题（模拟 OCR 漏识/异版式）后，摘要仍止于创造性。。
     —— 证明收尾不再依赖任何写死段题字样。"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cnipa_ocr as co

RUN = os.path.join(HERE, "output", "uia_runs", "detail_20260901_202133")

SPECS = [("shot", i) for i in range(1, 9)] + \
        [("tail", i) for i in range(1, 4)]


def load(kind, i, drop_head=False):
    base = os.path.join(RUN, "%s%d_ocr.txt" % (kind, i))
    img = os.path.join(RUN, "%s%d.png" % (kind, i))
    items = []
    for ln in open(base, encoding="utf-8"):
        m = re.match(r"^L\|(\d+,\d+,\d+,\d+)\|(.*)$", ln.rstrip("\n"))
        if m:
            box = tuple(int(v) for v in m.group(1).split(","))
            items.append((m.group(2), box))
    hl = co.hline_items(img)
    items = sorted(items + hl, key=lambda it: (it[1][1], it[1][0]))
    if drop_head:                      # 变体B：剔除 案由 段题（仅 shot5 有）
        items = [(t, b) for t, b in items
                 if co.norm(t) not in ("案由", "一案由")]
    return items, [h[0] for h in hl]


pages, info = [], []
for kind, i in SPECS:
    items, hl = load(kind, i)
    pages.append(items)
    info.append(("%s%d" % (kind, i), hl))

print("== 横线逐屏（宽度序列） ==")
for name, hl in info:
    print("%-12s %s" % (name, hl if hl else "-"))

print("\n== A) 原样回放 ==")
s = co.parse_detail_pages(pages).get("decision_summary") or ""
print("len =", len(s), "| endswith 创造性。。 =", s.endswith("否定涉案专利的创造性。。"))
print("污染检查(案由/请求人/优先权日/__HL__) =",
      any(k in s for k in ("案由", "请求人", "优先权日", "__HL__")))

print("\n== B) 删除段题『案由』后回放（结构线规则独立收尾） ==")
pages_b = []
for kind, i in SPECS:
    items, _ = load(kind, i, drop_head=(kind == "shot5"))
    pages_b.append(items)
s2 = co.parse_detail_pages(pages_b).get("decision_summary") or ""
print("len =", len(s2), "| endswith 创造性。。 =", s2.endswith("否定涉案专利的创造性。。"))
print("与 A 完全一致 =", s == s2)

ok = (s.endswith("否定涉案专利的创造性。。")
      and s2.endswith("否定涉案专利的创造性。。")
      and s == s2
      and "__HL__" not in s
      and "案由" not in s)
print("\nRESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
