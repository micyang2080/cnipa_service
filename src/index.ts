#!/usr/bin/env node
/**
 * MCP server for 国知局（CNIPA）复审/无效决定 + 摘要附图。
 * 对外只暴露工具能力与入参；实现细节见 docs/接口契约.md。
 */
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import type { ToolAnnotations } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import {
  runCnipaFetchAbstractFigure,
  runCnipaFetchDecisionDetail,
  runCnipaLogin,
  runCnipaLoginUia,
  runCnipaSaveCredentials,
  runCnipaSearchDecisions,
} from './runPython.js';
import type { RunCnipaResult } from './runPython.js';

const READ_ONLY_SEARCH: ToolAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: true,
};

const server = new McpServer(
  {
    name: 'cnipa-patent-review-mcp',
    version: '0.2.0',
  },
  { capabilities: { tools: {} } }
);

function formatResult(data: unknown): string {
  if (data === null || typeof data !== 'object') return String(data);
  const o = data as Record<string, unknown>;
  const parts: string[] = [];

  // 契约错误结构：{success:false, error:<code>, message, hint}
  if (o.success === false && typeof o.error === 'string') {
    parts.push(`错误[${o.error}]: ${typeof o.message === 'string' ? o.message : ''}`);
    if (typeof o.hint === 'string' && o.hint) parts.push(`提示: ${o.hint}`);
    return parts.join('\n');
  }

  // status 工具
  if (typeof o.ready === 'boolean') {
    parts.push(o.ready ? '服务可用' : '服务暂不可用');
    if (typeof o.logged_in === 'boolean') parts.push(`logged_in: ${o.logged_in}`);
    if (typeof o.message === 'string' && o.message) parts.push(o.message);
    return parts.join('\n');
  }

  if (typeof o.library_type === 'string') parts.push(`library_type: ${o.library_type}`);
  if (typeof o.total === 'number') parts.push(`total: ${o.total}`);
  if (typeof o.truncated === 'boolean') parts.push(`truncated: ${o.truncated}`);
  if (typeof o.from_cache === 'boolean') parts.push(`from_cache: ${o.from_cache}`);
  if (typeof o.cache_file === 'string' && o.cache_file) parts.push(`cache_file: ${o.cache_file}`);

  // login 工具（cnipa_login）：登录态结果
  if (typeof o.logged_in === 'boolean') {
    parts.push(`logged_in: ${o.logged_in}`);
    if (typeof o.already === 'boolean') parts.push(`already: ${o.already}`);
    if (typeof o.account_configured === 'boolean') {
      parts.push(`account_configured: ${o.account_configured}`);
    }
    if (typeof o.username_masked === 'string' && o.username_masked) {
      parts.push(`username: ${o.username_masked}`);
    }
    if (typeof o.message === 'string' && o.message) parts.push(`message: ${o.message}`);
    return parts.join('\n');
  }

  const items = o.items;
  if (Array.isArray(items)) {
    parts.push(`items: ${items.length} 条`);
    const preview = items.slice(0, 5);
    if (preview.length > 0) {
      parts.push('预览（前 5 条）:');
      parts.push(JSON.stringify(preview, null, 2));
    }
  }
  if (o.detail && typeof o.detail === 'object') {
    parts.push(`detail: ${JSON.stringify(o.detail, null, 2)}`);
  }
  if (typeof o.figure_path === 'string' && o.figure_path) {
    parts.push(`figure_path: ${o.figure_path}`);
  }
  if (typeof o.method === 'string' && o.method) {
    parts.push(`method: ${o.method}`);
  }
  if (parts.length === 0) {
    return JSON.stringify(o, null, 2);
  }
  return parts.join('\n');
}

function toolResponse(data: unknown, failPrefix: string) {
  const ok =
    data !== null &&
    typeof data === 'object' &&
    (data as Record<string, unknown>).success !== false;
  return {
    content: [{ type: 'text' as const, text: formatResult(data) }],
    isError: !ok,
  };
}

/**
 * 未登录自动补登录重试（2026-09-02 账密自动登录上线）：
 * 检索/详情/附图返回契约错误 NOT_LOGGED_IN 时——
 *   1. 跑一次自动登录（读 account.ini）；
 *   2. 登录返回 NEED_CREDENTIALS → 透传该错误（agent 据此向用户索要账密）；
 *   3. 登录成功 → 原操作重试一次；
 *   4. 登录失败（CAPTCHA_FAIL/AUTH_REJECTED…）→ 返回登录错误本身
 *      （比原 NOT_LOGGED_IN 更接近根因）。
 */
function withAutoLogin(run: () => RunCnipaResult): RunCnipaResult {
  const first = run();
  if (!first.success) return first;
  const data = first.data as Record<string, unknown> | null;
  if (!data || data.error !== 'NOT_LOGGED_IN') return first;

  const login = runCnipaLoginUia({});
  if (!login.success) return first; // runner 级失败：保留原错误（可再查 status）
  const ld = login.data as Record<string, unknown> | null;
  if (ld && ld.error === 'NEED_CREDENTIALS') return login; // 向用户索要账密
  if (ld && ld.success === true) return run();             // 登录成功 → 重试
  return login;                                            // 登录失败 → 报根因
}

server.registerTool(
  'cnipa_check_status',
  {
    title: '国知局查询服务状态',
    description: '查询本服务当前是否可用（ready / logged_in 等状态字段）。',
    inputSchema: {},
    annotations: READ_ONLY_SEARCH,
  },
  async () => {
    const result = runCnipaLogin({});
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `状态检查失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '状态检查失败');
  }
);

const loginSchema = {
  username: z.string().optional().describe('国知局账号（手机号）。首次使用时传入，之后自动记住'),
  password: z.string().optional().describe('国知局密码。与 username 一起传入；不会出现在输出中'),
  login_type: z
    .enum(['natural', 'legal'])
    .optional()
    .describe('登录身份：natural=自然人（默认）、legal=法人'),
  probe_only: z.boolean().optional().describe('true=只检查状态，不执行登录'),
  max_slider_attempts: z.number().optional().describe('滑块最大尝试次数，默认 3'),
};

server.registerTool(
  'cnipa_login',
  {
    title: '国知局账号登录',
    description:
      '配置或刷新国知局账号。首次使用传入 username/password；之后可省略。' +
      '若返回 NEED_CREDENTIALS，向用户索要账号密码后带参重调。',
    inputSchema: loginSchema,
    annotations: { ...READ_ONLY_SEARCH, idempotentHint: false },
  },
  async (args) => {
    const result = runCnipaLoginUia(args);
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `登录失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '登录失败');
  }
);

server.registerTool(
  'cnipa_save_credentials',
  {
    title: '[已废弃] 保存国知局账号凭证',
    description: '已废弃，请改用 cnipa_login（传 username/password）。',
    inputSchema: {
      username: z.string().describe('国知局账号（手机号）'),
      password: z.string().describe('密码'),
      credentials_path: z.string().optional().describe('可选：自定义凭证文件路径'),
    },
    annotations: { ...READ_ONLY_SEARCH, idempotentHint: false, destructiveHint: true },
  },
  async (args) => {
    const result = runCnipaSaveCredentials(args);
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `保存失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '保存失败');
  }
);

const FILTER_KEYS = [
  'application_no',
  'ipc_class',
  'decision_no',
  'case_no',
  'invention_title',
  'patent_type',
  'patentee',
  'petitioner',
  'panel_leader',
  'panel_examiner',
  'panel_member',
  'agency',
] as const;

const searchSchema = {
  library_type: z
    .enum(['invalid', 'reexam'])
    .optional()
    .describe('决定库：invalid=无效决定库（默认）、reexam=复审决定库'),
  filters: z
    .object({
      application_no: z
        .string()
        .optional()
        .describe('申请号/专利号，如 200610004173.6（CN/ZL 前缀、无点均可，自动规范化）'),
      ipc_class: z.string().optional().describe('分类号，如 A61K31/137'),
      decision_no: z.string().optional().describe('决定号，如 38430'),
      case_no: z.string().optional().describe('案件编号，如 4W107577'),
      invention_title: z.string().optional().describe('发明名称（支持模糊）'),
      patent_type: z
        .enum(['invention', 'utility_model', 'design'])
        .optional()
        .describe('专利类型：invention=发明专利、utility_model=实用新型、design=外观设计'),
      patentee: z.string().optional().describe('专利权人'),
      petitioner: z
        .string()
        .optional()
        .describe('请求人：无效库=无效宣告请求人；复审库=复审请求人'),
      panel_leader: z.string().optional().describe('合议组组长'),
      panel_examiner: z.string().optional().describe('主审员'),
      panel_member: z.string().optional().describe('参审员'),
      agency: z.string().optional().describe('代理机构'),
    })
    .describe('查询条件（12 项对齐 cpquery 展开项；多条件=站点 AND；至少 1 个非空，否则 FILTER_EMPTY）'),
  max_items: z
    .number()
    .int()
    .min(1)
    .max(500)
    .optional()
    .describe('最多返回条数，默认 50、上限 500（超出站点全量时 truncated=true）'),
  page: z
    .number()
    .int()
    .min(1)
    .optional()
    .describe('起始页码，从 1 起（默认 1；配合 max_items 自动翻页收集）'),
  force_refresh: z
    .boolean()
    .optional()
    .describe('true=跳过本地缓存强制重查（默认 false，命中时 from_cache=true）'),
};

function emptyFilters(filters: Record<string, unknown> | undefined): boolean {
  if (!filters) return true;
  return !FILTER_KEYS.some((k) => String(filters[k] ?? '').trim() !== '');
}

server.registerTool(
  'cnipa_search_decisions',
  {
    title: '国知局复审/无效决定检索',
    description:
      '按条件检索国知局复审或无效审查决定。filters 支持申请号、分类号、决定号、' +
      '案件编号、发明名称、专利类型、专利权人、请求人、合议组组长/主审员/参审员、' +
      '代理机构（可组合）。返回 items[]（固定 20 键，含 detail_url）；' +
      '可把 item 传给 cnipa_fetch_decision_detail，或用 application_no 取附图。' +
      '默认使用本地缓存；force_refresh=true 强制重查。',
    inputSchema: searchSchema,
    annotations: READ_ONLY_SEARCH,
  },
  async ({ library_type, filters, max_items, page, force_refresh }) => {
    if (emptyFilters(filters)) {
      return toolResponse(
        {
          success: false,
          error: 'FILTER_EMPTY',
          message: 'filters 至少需要 1 个非空条件（12 项键名见工具 schema）。',
          hint: '可用键：' + FILTER_KEYS.join(' / '),
        },
        '检索失败',
      );
    }
    const result = withAutoLogin(() =>
      runCnipaSearchDecisions({
        library_type: library_type ?? 'invalid',
        filters: filters ?? {},
        ...(max_items !== undefined ? { max_items } : {}),
        ...(page !== undefined ? { page } : {}),
        ...(force_refresh !== undefined ? { force_refresh } : {}),
      }),
    );
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `检索失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '检索失败');
  }
);

const detailSchema = {
  application_no: z
    .string()
    .optional()
    .describe('申请号/专利号（建议，对拍校验用），如 200610004173.6（CN/ZL 前缀、无点均可）'),
  case_no: z
    .string()
    .optional()
    .describe('案件编号（建议，页内导航下钻入口），如 4W107577'),
  library_type: z
    .enum(['invalid', 'reexam'])
    .optional()
    .describe('决定库：invalid=无效决定库（默认）、reexam=复审决定库（决定左导航择路）'),
  detail_url: z
    .string()
    .optional()
    .describe('可选：search 返回的 detail_url（缺号时从中抽 case_no/application_no；并原样回显）'),
  list_item: z
    .object({
      source: z.string().optional(),
      review_type: z.number().optional(),
      case_no: z.string().optional(),
      decision_no: z.string().optional(),
      issue_date: z.string().optional(),
      decision_date: z.string().optional(),
      invention_title: z.string().optional(),
      petitioner: z.string().optional(),
      patentee: z.string().optional(),
      application_no: z.string().optional(),
      request_date: z.string().optional(),
      legal_basis: z.string().optional(),
      decision_summary: z.string().optional(),
      decision_result: z.string().optional(),
      panel_members: z.string().optional(),
      figure_path: z.string().optional(),
      detail_url: z.string().optional(),
      patent_type: z.string().optional(),
      ipc_class: z.string().optional(),
      agency: z.string().optional(),
    })
    .optional()
    .describe(
      '推荐：cnipa_search_decisions 的 item 原样传入——卡片精确值优先覆盖 OCR 噪声值，' +
        'review_type 自动判定决定库',
    ),
  anjianbh: z
    .string()
    .optional()
    .describe('[兼容旧键] 案件编号，等同 case_no'),
  zhuanlisqh: z
    .string()
    .optional()
    .describe('[兼容旧键] 申请号，等同 application_no'),
  card_fields: z
    .object({
      case_no: z.string().optional(),
      decision_no: z.string().optional(),
      issue_date: z.string().optional(),
      app_no: z.string().optional(),
      patentee: z.string().optional(),
      app_date: z.string().optional(),
      class_no: z.string().optional(),
      conclusion: z.string().optional(),
      invalid_requester: z.string().optional(),
      reexam_requester: z.string().optional(),
      patent_type: z.string().optional(),
      title: z.string().optional(),
    })
    .optional()
    .describe('[兼容旧键] 内部卡片字段（建议改用 list_item）'),
  keep_tab: z.boolean().optional().describe('true=保留相关标签（默认 false）'),
  force_refresh: z
    .boolean()
    .optional()
    .describe('true=跳过本地缓存强制重取（默认 false，命中时 from_cache=true）'),
};

server.registerTool(
  'cnipa_fetch_decision_detail',
  {
    title: '国知局决定详情',
    description:
      '获取单条复审/无效决定详情（决定日、请求日、法律依据、决定要点、审查结论、' +
      '合议组成员等，固定 20 键）。推荐传入 cnipa_search_decisions 返回的 list_item。' +
      '默认使用本地缓存；force_refresh=true 强制重取。',
    inputSchema: detailSchema,
    annotations: READ_ONLY_SEARCH,
  },
  async (args) => {
    const result = withAutoLogin(() => runCnipaFetchDecisionDetail(args));
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `详情失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '详情失败');
  }
);

const figureSchema = {
  application_no: z
    .string()
    .describe('专利申请号（13 位含点），如 200610004173.6 或 CN200410082265.7'),
  save_path: z
    .string()
    .optional()
    .describe('附图保存绝对路径（可选；不传则由服务写入默认位置并以 figure_path 返回）'),
  keep_tab: z.boolean().optional().describe('true=保留相关标签（默认 false）'),
  force_refresh: z
    .boolean()
    .optional()
    .describe('true=跳过本地缓存强制重取（默认 false，命中时 from_cache=true）'),
};

server.registerTool(
  'cnipa_fetch_abstract_figure',
  {
    title: '国知局摘要附图',
    description:
      '按申请号获取摘要附图 PNG，返回 figure_path。无附图时返回 NO_FIGURE。' +
      '默认使用本地缓存；force_refresh=true 强制重取。',
    inputSchema: figureSchema,
    annotations: READ_ONLY_SEARCH,
  },
  async (args) => {
    const result = withAutoLogin(() => runCnipaFetchAbstractFigure(args));
    if (!result.success) {
      return {
        content: [{ type: 'text' as const, text: `附图失败: ${result.error}` }],
        isError: true,
      };
    }
    return toolResponse(result.data, '附图失败');
  }
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
