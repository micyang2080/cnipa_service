# -*- coding: utf-8 -*-
"""只读回读探针：对一个表单输入框（按标签定位）实证「外层容器 Edit / 内层 input Edit」
的值通道，不点击、不键入、不发请求。用法：
    python -X utf8 python/uia_probe_readback.py [标签前缀，默认 案件编号]

背景：run_20260901_171031 实跑，edit_value 只下钻直接子级（children 全是 Group）
→ 内层 input Edit（Edit>Group>Group>Edit 第 3 层以下）读不到，回读仍为空。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cnipa_uia as uia


def main() -> int:
    label = sys.argv[1] if len(sys.argv) >= 2 else '案件编号'
    win = uia.require_window()
    edit = uia.label_to_edit(win, label)
    if edit is None:
        print(f'[probe] 未定位到「{label}」输入框（表单里没有该字段？）')
        return 1

    r = uia._rect(edit)
    print(f'[probe] {label} → 外层容器 Edit rect={r}')
    try:
        print(f'[probe]   外层 legacy.value = {edit.legacy_properties().get("value")!r}')
    except Exception as e:
        print(f'[probe]   外层 legacy 读取失败: {e}')

    print('[probe] 直接子级:')
    for c in edit.children():
        print(f'  - {_safe_ct(c)} name={uia._name(c)!r} rect={uia._rect(c)}')

    print('[probe] 全部后代中的 Edit（含 legacy.value）:')
    found = False
    for d in edit.descendants():
        if uia._ctype(d) != 'Edit':
            continue
        found = True
        v = ''
        try:
            v = d.legacy_properties().get('value') or ''
        except Exception as e:
            v = f'<err {e}>'
        print(f'  - Edit name={uia._name(d)!r} rect={uia._rect(d)} legacy.value={v!r}')
        _dump_value_channels(d)
    if not found:
        print('  （无后代 Edit）')
    _dump_value_channels(edit, tag='外层')
    return 0


def _dump_value_channels(el, tag: str = '内层') -> None:
    """穷举 pywinauto 可能的读值通道（全只读）。"""
    print(f'[probe]   [{tag}] 穷举报单:')
    try:
        print(f'      legacy_properties 全量 = {el.legacy_properties()!r}')
    except Exception as e:
        print(f'      legacy_properties 失败: {e}')
    for attr in ('iface_value', 'iface_text'):
        try:
            obj = getattr(el, attr, None)
            if obj is None:
                print(f'      {attr} = <不支持>')
                continue
            if attr == 'iface_value':
                print(f'      iface_value.CurrentValue = {obj.CurrentValue!r}')
            else:
                print(f'      iface_text.DocumentRange(0,-1).GetText() = '
                      f'{obj.DocumentRange(0, -1).GetText(-1)[:60]!r}')
        except Exception as e:
            print(f'      {attr} 失败: {e}')
    if hasattr(el, 'get_value'):
        try:
            print(f'      get_value() = {el.get_value()!r}')
        except Exception as e:
            print(f'      get_value 失败: {e}')
    try:
        from pywinauto.uia_element_info import UIAElementInfo
        ei = el.element_info
        print(f'      element_info.name = {ei.name!r}  value = '
              f'{getattr(ei, "value", "<无属性>")!r}')
    except Exception as e:
        print(f'      element_info 失败: {e}')


def _safe_ct(el) -> str:
    try:
        return str(el.element_info.control_type)
    except Exception:
        return '?'


if __name__ == '__main__':
    sys.exit(main())
