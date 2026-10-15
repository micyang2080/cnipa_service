# -*- coding: utf-8 -*-
"""clean_output.py — python/output 目录治理（可重复执行）

背景（2026-09-02 用户要求）：output 下混着【交付物】与【过程临时文件】，
页面截图类探针会随实跑无限增长。本脚本固化去留策略：

交付物（永远保留）
  output/figures/                      摘要附图等最终产物

关键证据（整目录保留，含截图）
  output/uia_runs/detail_20260901_202133    04 全胜 run + _replay_hline_probe.py 数据源
  output/uia_runs/figure_20260902_141208    PF4 自测夹具来源（03_no_figure.png）
  output/uia_runs/figure_20260902_150348    15:05 对话框识别失灵的根因现场
  output/uia_runs/figure_20260902_151459    05 端到端首胜（用户确认）

其余 uia_runs/<run>/
  保留 payload.json / debug.json / *.txt / *.json（排障史，KB 级）
  删除 *.png / *.jpg（体积大头；RUNBOOK 已把结论沉淀为文字+自测夹具）

PW 时代遗留（CDP 轨道已废，整体删除）
  cnipa_session/ debug_steps/ sso_probe/ cnipa_cache/
  根下 offline_*.png debug_*.png captcha_*.png login_blank.png
  diag_after_login.png cpquery_blank.png cpquery_resp_200.html
  portal_isAuth.json leak_scan_test.py
  uia_runs 根下散件 ocr_synth_test.png ocr_page[12]_boxed.txt
  （ocr 文本夹具已冻结进 ocr_fixtures_detail.py，源文件无需保留）

用法：
  python clean_output.py            # dry-run，只打印将执行的动作
  python clean_output.py --apply    # 实际删除/清理
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'output')
RUNS = os.path.join(OUT, 'uia_runs')

# 整目录保留的关键证据 run（相对 uia_runs/）
KEEP_FULL_RUNS = {
    'detail_20260901_202133',     # 04 全胜 + 演示重组素材
    'figure_20260902_141208',     # PF4 自测夹具来源
    'figure_20260902_150348',     # 15:05 根因现场
    'figure_20260902_151459',     # 端到端首胜
}
# 自测/脚本对单个文件的硬引用（防误删，双保险）
KEEP_FILES = {
    os.path.join(RUNS, 'run_20260901_171031', 'tree_flat.txt'),  # 04 selftest 树回放
}
# PW 时代遗留目录/散件（相对 output/）
PW_DIRS = ['cnipa_session', 'debug_steps', 'sso_probe', 'cnipa_cache']
PW_FILES = [
    'offline_cat.png', 'offline_gap.png', 'offline_gap2.png', 'offline_dark.png',
    'debug_gap_detection.png', 'debug_piece.png',
    'captcha_bg.png', 'captcha_piece.png',
    'login_blank.png', 'diag_after_login.png', 'cpquery_blank.png',
    'cpquery_resp_200.html', 'portal_isAuth.json', 'leak_scan_test.py',
]
RUNS_ROOT_JUNK = ['ocr_synth_test.png', 'ocr_page1_boxed.txt', 'ocr_page2_boxed.txt']
# PW 时代散件通配（output 根下）
PW_GLOBS = [
    'diagnostic_*.json', 'cpquery_*.html', 'cpquery_*.txt',
    'portal_nav_links.json', 'is_trusted_test.py', 'debug_*.png',
]

STRIP_EXTS = ('.png', '.jpg', '.jpeg', '.bmp')


def sizeof(path):
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def main(argv):
    apply = '--apply' in argv
    if not os.path.isdir(OUT):
        print('no output dir, nothing to do')
        return 0
    actions = []  # (kind, path, bytes)
    freed = 0

    def rm(path, kind):
        nonlocal freed
        if not os.path.exists(path):
            return
        sz = sizeof(path)
        actions.append((kind, path, sz))
        freed += sz
        if apply:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)

    # 1) PW 遗留
    for d in PW_DIRS:
        rm(os.path.join(OUT, d), 'PW目录')
    for f in PW_FILES:
        rm(os.path.join(OUT, f), 'PW散件')
    import glob as _glob
    for pat in PW_GLOBS:
        for f in _glob.glob(os.path.join(OUT, pat)):
            rm(f, 'PW散件')
    for f in RUNS_ROOT_JUNK:
        rm(os.path.join(RUNS, f), 'uia_runs散件')

    # 2) 旧 run 截图瘦身（保留 json/txt；KEEP_FULL_RUNS 整目录跳过）
    if os.path.isdir(RUNS):
        for name in sorted(os.listdir(RUNS)):
            run_dir = os.path.join(RUNS, name)
            if not os.path.isdir(run_dir) or name in KEEP_FULL_RUNS:
                continue
            for fn in sorted(os.listdir(run_dir)):
                fp = os.path.join(run_dir, fn)
                if fp in KEEP_FILES:
                    continue
                if os.path.isfile(fp) and fn.lower().endswith(STRIP_EXTS):
                    rm(fp, '旧run截图')

    print(f'== clean_output {"APPLY" if apply else "DRY-RUN"} ==')
    for kind, path, sz in actions:
        print(f'  [{kind}] {sz/1024:8.0f} KB  {os.path.relpath(path, HERE)}')
    if not actions:
        print('  (nothing to clean)')
    print(f'共 {len(actions)} 项，可释放 {freed/1024/1024:.1f} MB')
    if not apply:
        print('dry-run 未删除；确认无误后加 --apply 执行')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
