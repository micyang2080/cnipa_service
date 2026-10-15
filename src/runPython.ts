/**
 * Spawn Python UIA runners under CNIPA_CONNECTOR_ROOT.
 *
 * UIA 轨道 I/O 契约（与旧 PW stdin 协议不同，2026-09-02 接线定案）：
 *   入参：argv[1] = input.json 路径（Node 写临时文件）
 *   出参：stdout 末行 = CNIPA_EMIT_JSON=1 时 runner 打印的契约白名单 JSON
 *   退出码：业务失败也可能 exit 1 → **先解析末行 JSON**，解析到即成功取数；
 *           解析不到再按超时/运行时错误/无效输出上报。
 */

import { existsSync, mkdirSync, statSync, writeFileSync } from 'fs';
import { spawnSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export type RunCnipaResult =
  | { success: true; data: unknown }
  | {
      success: false;
      error: string;
      code: 'ENV_INVALID' | 'PYTHON_MISSING' | 'SCRIPT_MISSING' | 'RUNTIME_ERROR' | 'INVALID_OUTPUT';
    };

function resolveExistingDir(candidate: string): string | null {
  try {
    if (existsSync(candidate) && statSync(candidate).isDirectory()) {
      return candidate;
    }
  } catch {
    // ignore
  }
  return null;
}

function hasCnipaConnector(root: string): boolean {
  // UIA 轨道锚点脚本（旧 run_cnipa_login_pw.py 已废弃）
  return existsSync(path.join(root, 'run_cnipa_search_decisions_uia.py'));
}

function getCnipaRoot(): { ok: true; root: string } | { ok: false; error: string } {
  const candidates: string[] = [];
  const packageRoot = path.resolve(__dirname, '..');

  if (process.env.CNIPA_CONNECTOR_ROOT) {
    candidates.push(path.resolve(process.env.CNIPA_CONNECTOR_ROOT));
  }

  candidates.push(path.join(packageRoot, 'python'));
  candidates.push(path.join(packageRoot, 'scripts', 'cnipa'));

  for (const dir of candidates) {
    const resolved = resolveExistingDir(dir);
    if (!resolved) continue;
    if (hasCnipaConnector(resolved)) {
      return { ok: true, root: resolved };
    }
  }

  return {
    ok: false,
    error: `CNIPA UIA runner not found. Tried: ${candidates.join(' | ')}`,
  };
}

function getPythonCommand(): { ok: true; cmd: string } | { ok: false; error: string } {
  const root = process.env.GENIUSWORK_PYTHON_ROOT;
  if (root) {
    const exeNames = process.platform === 'win32' ? ['python.exe', 'python3.exe'] : ['python3', 'python'];
    for (const name of exeNames) {
      const candidate = path.join(root, name);
      if (existsSync(candidate)) {
        return { ok: true, cmd: candidate };
      }
    }
  }
  const fallback = process.platform === 'win32' ? 'python' : 'python3';
  return { ok: true, cmd: fallback };
}

function resolveSpawnMaxBuffer(): number {
  const raw = process.env.CNIPA_SPAWN_MAX_BUFFER;
  if (raw) {
    const n = parseInt(raw, 10);
    if (Number.isFinite(n) && n >= 8 * 1024 * 1024) {
      return n;
    }
  }
  return 256 * 1024 * 1024;
}

function resolveTimeoutMs(envKey: string, defaultMs: number): number {
  const raw = process.env[envKey];
  const parsed = raw ? parseInt(raw, 10) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : defaultMs;
}

/** 从 stdout 取最后一个可解析为 JSON 的行（自尾向首扫，跳过 runner 的 [uia] 日志行）。 */
function parseLastJsonLine(stdout: string): unknown | null {
  const lines = (stdout || '').split('\n').map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (!line.startsWith('{') && !line.startsWith('[')) continue;
    try {
      return JSON.parse(line);
    } catch {
      // 继续向前找（可能是日志里恰好以 { 开头的行）
    }
  }
  return null;
}

/** UIA 轨道仅 Windows；非 win32 直接契约错误，不 spawn。 */
function unsupportedOsResult(): RunCnipaResult {
  return {
    success: true,
    data: {
      success: false,
      error: 'UNSUPPORTED_OS',
      message: `当前操作系统为 ${process.platform}，cnipa-mcp 仅支持 Windows（依赖 pywinauto/UIA 桌面自动化）`,
      hint: '请在 Windows 上的 GeniusWork 中使用本 MCP；macOS/Linux 暂不支持',
    },
  };
}

function runCnipaUiaScript(
  scriptFile: string,
  args: Record<string, unknown>,
  timeoutMs?: number,
): RunCnipaResult {
  if (process.platform !== 'win32') {
    return unsupportedOsResult();
  }

  const rootResult = getCnipaRoot();
  if (!rootResult.ok) {
    return { success: false, error: rootResult.error, code: 'ENV_INVALID' };
  }
  const root = rootResult.root;
  const scriptPath = path.join(root, scriptFile);

  if (!existsSync(scriptPath)) {
    return {
      success: false,
      error: `${scriptFile} not found at: ${scriptPath}`,
      code: 'SCRIPT_MISSING',
    };
  }

  const pyResult = getPythonCommand();
  if (!pyResult.ok) {
    return { success: false, error: pyResult.error, code: 'PYTHON_MISSING' };
  }
  const pythonCmd = pyResult.cmd;

  // 入参走 input.json（UIA runner 约定 argv[1]），落 output/uia_inputs/ 留证
  const inputDir = path.join(root, 'output', 'uia_inputs');
  mkdirSync(inputDir, { recursive: true });
  const inputBase = scriptFile.replace(/\.py$/, '').replace(/^run_/, '');
  const inputFile = path.join(
    inputDir,
    `${inputBase}_${Date.now()}.json`,
  );
  writeFileSync(inputFile, JSON.stringify(args), 'utf-8');

  const env = {
    ...process.env,
    CNIPA_CONNECTOR_ROOT: root,
    CNIPA_EMIT_JSON: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  };

  const result = spawnSync(pythonCmd, ['-X', 'utf8', scriptPath, inputFile], {
    cwd: root,
    encoding: 'utf-8',
    env,
    timeout: timeoutMs ?? resolveTimeoutMs('CNIPA_DEFAULT_TIMEOUT_MS', 600_000),
    maxBuffer: resolveSpawnMaxBuffer(),
  });

  if (result.error) {
    const err = result.error as NodeJS.ErrnoException;
    if (err.code === 'ETIMEDOUT') {
      const sec = Math.round((timeoutMs ?? 600_000) / 1000);
      return {
        success: false,
        error: `spawnSync ETIMEDOUT：Python 超过 ${sec} 秒。可设 CNIPA_DEFAULT_TIMEOUT_MS（毫秒）。`,
        code: 'RUNTIME_ERROR',
      };
    }
    return { success: false, error: result.error.message, code: 'RUNTIME_ERROR' };
  }

  // 先解析末行 JSON——UIA runner 业务失败时 exit 1 但仍发射契约 JSON（§接口契约）
  const emitted = parseLastJsonLine(result.stdout || '');
  if (emitted !== null && typeof emitted === 'object') {
    return { success: true, data: emitted };
  }

  if (result.status !== 0) {
    const stderr = (result.stderr || '').trim();
    const tail = (result.stdout || '').trim().split('\n').slice(-3).join(' | ');
    const detail = stderr || tail;
    return {
      success: false,
      error: detail || `Python exited with code ${result.status}（且无契约 JSON 输出）`,
      code: 'RUNTIME_ERROR',
    };
  }

  return {
    success: false,
    error: `No contract JSON output from ${scriptFile}（stdout 末行不是 JSON）`,
    code: 'INVALID_OUTPUT',
  };
}

/** cnipa_login 新语义：环境状态检查（零点击零站点交互，UIA 轨道无自动登录）。 */
export function runCnipaLogin(_args: Record<string, unknown>): RunCnipaResult {
  return runCnipaUiaScript(
    'run_cnipa_check_status_uia.py',
    {},
    resolveTimeoutMs('CNIPA_STATUS_TIMEOUT_MS', 120_000),
  );
}

/**
 * UIA 自动登录（run_cnipa_login_uia.py，2026-09-02）。
 * 入参：{username?, password?, login_type?, probe_only?, max_slider_attempts?}
 *   - 带 username/password → runner 先存 account.ini 再登录（首次使用流）；
 *   - 不带 → 读 account.ini；没有凭证 → 契约错误 NEED_CREDENTIALS（agent
 *     应据此向用户索要账密后带参重调）。
 */
export function runCnipaLoginUia(args: Record<string, unknown>): RunCnipaResult {
  const { username, password, login_type, probe_only, max_slider_attempts } =
    args as Record<string, unknown>;
  const payload: Record<string, unknown> = {};
  if (username) payload.username = username;
  if (password) payload.password = password;
  if (login_type) payload.login_type = login_type;
  if (probe_only !== undefined) payload.probe_only = probe_only;
  if (max_slider_attempts !== undefined) payload.max_slider_attempts = max_slider_attempts;
  return runCnipaUiaScript(
    'run_cnipa_login_uia.py',
    payload,
    resolveTimeoutMs('CNIPA_LOGIN_TIMEOUT_MS', 300_000),
  );
}

export function runCnipaSearchDecisions(args: Record<string, unknown>): RunCnipaResult {
  const { library_type, filters, max_items, page, force_refresh } = args as {
    library_type?: string;
    filters?: Record<string, unknown>;
    max_items?: number;
    page?: number;
    force_refresh?: boolean;
  };
  const payload: Record<string, unknown> = {
    library_type: library_type ?? 'invalid',
    filters: filters ?? {},
  };
  // 技能侧契约 §1.1：max_items 默认 50、上限 500（双保险钳制，runner 同规则）
  if (max_items !== undefined) {
    const n = Math.floor(Number(max_items));
    if (Number.isFinite(n)) payload.max_items = Math.min(Math.max(n, 1), 500);
  }
  if (page !== undefined && Number.isFinite(Number(page))) payload.page = Math.max(1, Math.floor(Number(page)));
  if (force_refresh !== undefined) payload.force_refresh = Boolean(force_refresh);
  return runCnipaUiaScript(
    'run_cnipa_search_decisions_uia.py',
    payload,
    resolveTimeoutMs('CNIPA_SEARCH_TIMEOUT_MS', 900_000),
  );
}

export function runCnipaFetchDecisionDetail(args: Record<string, unknown>): RunCnipaResult {
  const { anjianbh, zhuanlisqh, card_fields, keep_tab, library_type, detail_url, list_item, application_no, case_no, force_refresh } =
    args as Record<string, unknown>;
  const payload: Record<string, unknown> = {};
  // 技能侧契约 §3.1 新入参（2026-09-03）：application_no/case_no/library_type/
  // detail_url/list_item（search item 原样）；旧键 anjianbh/zhuanlisqh/card_fields 兼容
  if (application_no) payload.application_no = application_no;
  if (case_no) payload.case_no = case_no;
  if (library_type) payload.library_type = library_type;
  if (detail_url) payload.detail_url = detail_url;
  if (list_item && typeof list_item === 'object') payload.list_item = list_item;
  if (anjianbh) payload.anjianbh = anjianbh;
  if (zhuanlisqh) payload.zhuanlisqh = zhuanlisqh;
  if (card_fields && typeof card_fields === 'object') payload.card_fields = card_fields;
  if (keep_tab !== undefined) payload.keep_tab = keep_tab;
  if (force_refresh !== undefined) payload.force_refresh = Boolean(force_refresh);
  return runCnipaUiaScript(
    'run_cnipa_fetch_decision_detail_uia.py',
    payload,
    resolveTimeoutMs('CNIPA_DETAIL_TIMEOUT_MS', 600_000),
  );
}

export function runCnipaFetchAbstractFigure(args: Record<string, unknown>): RunCnipaResult {
  const { application_no, save_path, keep_tab, force_refresh } = args as Record<string, unknown>;
  const payload: Record<string, unknown> = { application_no };
  if (save_path) payload.save_path = save_path;
  if (keep_tab !== undefined) payload.keep_tab = keep_tab;
  if (force_refresh !== undefined) payload.force_refresh = Boolean(force_refresh);
  return runCnipaUiaScript(
    'run_cnipa_fetch_abstract_figure_uia.py',
    payload,
    resolveTimeoutMs('CNIPA_FIGURE_TIMEOUT_MS', 600_000),
  );
}

/** 旧 PW 轨道遗留：仅历史兼容，UIA 轨道不读凭证（deprecated）。 */
export function runCnipaSaveCredentials(args: Record<string, unknown>): RunCnipaResult {
  if (process.platform !== 'win32') {
    return unsupportedOsResult();
  }
  const rootResult = getCnipaRoot();
  const root = rootResult.ok ? rootResult.root : '';
  const scriptPath = path.join(root || 'python', 'run_cnipa_save_credentials.py');
  if (!existsSync(scriptPath)) {
    return {
      success: false,
      error: 'run_cnipa_save_credentials.py not found（UIA 轨道无需凭证，本工具已废弃）',
      code: 'SCRIPT_MISSING',
    };
  }
  const pyResult = getPythonCommand();
  if (!pyResult.ok) {
    return { success: false, error: pyResult.error, code: 'PYTHON_MISSING' };
  }
  const result = spawnSync(pyResult.cmd, ['-X', 'utf8', scriptPath], {
    cwd: root,
    input: JSON.stringify(args),
    encoding: 'utf-8',
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    timeout: 30_000,
    maxBuffer: resolveSpawnMaxBuffer(),
  });
  if (result.error || result.status !== 0) {
    return {
      success: false,
      error: (result.stderr || result.error?.message || `exit ${result.status}`).trim(),
      code: 'RUNTIME_ERROR',
    };
  }
  const last = (result.stdout || '').trim().split('\n').filter(Boolean).pop();
  try {
    return { success: true, data: JSON.parse(last ?? '') };
  } catch {
    return { success: false, error: `Invalid JSON: ${(last ?? '').slice(0, 200)}`, code: 'INVALID_OUTPUT' };
  }
}
