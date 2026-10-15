#!/usr/bin/env node
/**
 * Smoke: MCP server (UIA 轨道版)。
 * 1) tools/list 五工具齐；
 * 2) 真实调用 cnipa_check_status（零点击零站点交互，安全）验证全链路。
 * 注意：MCP server 常驻，取到响应后主动 kill（不等待自然退出）。
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
    clientInfo: { name: 'smoke', version: '0.2.0' },
  },
};
const listTools = { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} };
const callStatus = {
  jsonrpc: '2.0',
  id: 3,
  method: 'tools/call',
  params: { name: 'cnipa_check_status', arguments: {} },
};
// probe_only：只探测窗口/登录态/凭证文件，不导航不填表（安全）
const callLoginProbe = {
  jsonrpc: '2.0',
  id: 4,
  method: 'tools/call',
  params: { name: 'cnipa_login', arguments: { probe_only: true } },
};

const proc = spawn('node', [entry], { stdio: ['pipe', 'pipe', 'pipe'] });
let stdout = '';
let stderr = '';

proc.stdout.on('data', (chunk) => {
  stdout += chunk.toString();
});
proc.stderr.on('data', (chunk) => {
  stderr += chunk.toString();
});
proc.on('error', (e) => {
  console.error('spawn error:', e.message);
  process.exit(1);
});

const send = (o) => proc.stdin.write(JSON.stringify(o) + '\n');
send(init);
send({ jsonrpc: '2.0', method: 'notifications/initialized' });
send(listTools);
send(callStatus);
send(callLoginProbe);

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

const t0 = Date.now();
const iv = setInterval(() => {
  const listed = parse(2);
  const called = parse(3);
  const probed = parse(4);
  if (!listed || !called || !probed) return;
  clearInterval(iv);
  clearTimeout(guard);
  proc.kill();

  const names = ((listed.result && listed.result.tools) || []).map((t) => t.name).sort();
  const expected = [
    'cnipa_check_status',
    'cnipa_fetch_abstract_figure',
    'cnipa_fetch_decision_detail',
    'cnipa_login',
    'cnipa_save_credentials',
    'cnipa_search_decisions',
  ];
  if (JSON.stringify(names) !== JSON.stringify(expected)) {
    console.error('Tool mismatch:', names);
    process.exit(1);
  }
  console.log('tools ok:', names.join(', '));

  const text = ((called.result && called.result.content) || [])
    .map((c) => c.text || '')
    .join('\n');
  if (!/环境就绪|环境未就绪|状态检查失败/.test(text)) {
    console.error('status call unexpected text:', text.slice(0, 500));
    process.exit(1);
  }
  console.log('status call ok (%ss): %s', ((Date.now() - t0) / 1000).toFixed(1), text.split('\n')[0]);

  const probeText = ((probed.result && probed.result.content) || [])
    .map((c) => c.text || '')
    .join('\n');
  if (!/logged_in|登录失败|NEED_CREDENTIALS/.test(probeText)) {
    console.error('login probe unexpected text:', probeText.slice(0, 500));
    process.exit(1);
  }
  console.log('login probe ok:', probeText.split('\n')[0]);
  process.exit(0);
}, 300);

const guard = setTimeout(() => {
  clearInterval(iv);
  proc.kill();
  console.error('smoke timeout (120s). stdout head:', stdout.slice(0, 500), 'stderr:', stderr.slice(0, 500));
  process.exit(1);
}, 120_000);
