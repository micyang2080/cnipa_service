#!/usr/bin/env node
/**
 * Contract smoke（技能侧契约 v2，2026-09-03）：
 * 1) tools/list：cnipa_search_decisions.filters 含 12 项条件键 + max_items + page；
 *    cnipa_fetch_decision_detail 含 list_item / detail_url / application_no / case_no / library_type；
 * 2) tools/call 空条件检索 → 契约错误 FILTER_EMPTY（Node 侧拦截，零 Python/零站点交互）。
 * MCP server 常驻，取到响应后主动 kill。
 */
import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const entry = path.join(__dirname, '..', 'build', 'index.js');

const init = {
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'contract-smoke', version: '0.2.0' },
  },
};
const listTools = { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} };
const callEmpty = {
  jsonrpc: '2.0',
  id: 3,
  method: 'tools/call',
  params: { name: 'cnipa_search_decisions', arguments: { filters: {} } },
};
const callBlank = {
  jsonrpc: '2.0',
  id: 4,
  method: 'tools/call',
  params: {
    name: 'cnipa_search_decisions',
    arguments: { filters: { patentee: '   ', invention_title: '' } },
  },
};

const proc = spawn('node', [entry], { stdio: ['pipe', 'pipe', 'pipe'] });
let stdout = '';
let stderr = '';
proc.stdout.on('data', (c) => (stdout += c.toString()));
proc.stderr.on('data', (c) => (stderr += c.toString()));
proc.on('error', (e) => {
  console.error('spawn error:', e.message);
  process.exit(1);
});

const send = (o) => proc.stdin.write(JSON.stringify(o) + '\n');
send(init);
send({ jsonrpc: '2.0', method: 'notifications/initialized' });
send(listTools);
send(callEmpty);
send(callBlank);

const parse = (id) => {
  for (const l of stdout.split('\n')) {
    if (!l.startsWith('{')) continue;
    try {
      const o = JSON.parse(l);
      if (o.id === id) return o;
    } catch {
      // skip
    }
  }
  return null;
};

const fail = (msg) => {
  clearInterval(iv);
  clearTimeout(guard);
  proc.kill();
  console.error('FAIL:', msg);
  process.exit(1);
};

const iv = setInterval(() => {
  const listed = parse(2);
  const empty = parse(3);
  const blank = parse(4);
  if (!listed || !empty || !blank) return;
  clearInterval(iv);
  clearTimeout(guard);
  proc.kill();

  const tools = (listed.result && listed.result.tools) || [];
  const search = tools.find((t) => t.name === 'cnipa_search_decisions');
  const detail = tools.find((t) => t.name === 'cnipa_fetch_decision_detail');
  if (!search || !detail) fail('tools missing');

  const sch = (search.inputSchema && search.inputSchema.properties) || {};
  const fprops = (sch.filters && sch.filters.properties) || {};
  const want = [
    'application_no', 'ipc_class', 'decision_no', 'case_no', 'invention_title',
    'patent_type', 'patentee', 'petitioner', 'panel_leader', 'panel_examiner',
    'panel_member', 'agency',
  ];
  const missing = want.filter((k) => !(k in fprops));
  if (missing.length) fail(`search.filters 缺键: ${missing.join(',')}`);
  if (!('max_items' in sch) || !('page' in sch)) fail('search 缺 max_items/page');
  const ptEnum = (fprops.patent_type && fprops.patent_type.enum) || [];
  if (JSON.stringify(ptEnum) !== JSON.stringify(['invention', 'utility_model', 'design'])) {
    fail(`patent_type 枚举不符: ${JSON.stringify(ptEnum)}`);
  }
  console.log('search schema ok: filters 12 键 + patent_type 枚举 + max_items/page');

  const dprops = (detail.inputSchema && detail.inputSchema.properties) || {};
  const wantD = ['list_item', 'detail_url', 'application_no', 'case_no', 'library_type'];
  const missD = wantD.filter((k) => !(k in dprops));
  if (missD.length) fail(`detail 缺新入参: ${missD.join(',')}`);
  const liProps = (dprops.list_item && dprops.list_item.properties) || {};
  const wantLI = ['case_no', 'decision_no', 'issue_date', 'invention_title', 'petitioner',
    'patentee', 'application_no', 'decision_result', 'review_type', 'detail_url'];
  const missLI = wantLI.filter((k) => !(k in liProps));
  if (missLI.length) fail(`list_item 缺键: ${missLI.join(',')}`);
  console.log('detail schema ok: list_item/detail_url/application_no/case_no/library_type');

  const text = (r) =>
    (((r.result || {}).content) || []).map((c) => c.text || '').join('\n');
  const t3 = text(empty);
  const t4 = text(blank);
  if (!empty.result || empty.result.isError !== true || !t3.includes('FILTER_EMPTY')) {
    fail(`空 filters 未报 FILTER_EMPTY: ${t3.slice(0, 200)}`);
  }
  if (!blank.result || blank.result.isError !== true || !t4.includes('FILTER_EMPTY')) {
    fail(`全空白 filters 未报 FILTER_EMPTY: ${t4.slice(0, 200)}`);
  }
  console.log('FILTER_EMPTY ok: 空/全空白条件均被拦截（零 Python 调用）');
  process.exit(0);
}, 300);

const guard = setTimeout(() => {
  clearInterval(iv);
  proc.kill();
  console.error('timeout (60s). stdout head:', stdout.slice(0, 500), 'stderr:', stderr.slice(0, 500));
  process.exit(1);
}, 60_000);
