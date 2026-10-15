#!/usr/bin/env node
/**
 * npm 发布：将包内 python/ 拷贝到 scripts/cnipa，供 npx 安装后无需 CNIPA_CONNECTOR_ROOT。
 */
const fs = require('fs');
const path = require('path');

const pkgRoot = __dirname;
const source = path.join(pkgRoot, 'python');
const dest = path.join(pkgRoot, 'scripts', 'cnipa');

if (!fs.existsSync(source)) {
  console.warn('[cnipa-mcp] copy-connector: python/ not found:', source);
  process.exit(0);
}

fs.mkdirSync(path.dirname(dest), { recursive: true });
if (fs.existsSync(dest)) {
  fs.rmSync(dest, { recursive: true });
}
fs.cpSync(source, dest, { recursive: true });
console.log('[cnipa-mcp] copied python/ to scripts/cnipa');
