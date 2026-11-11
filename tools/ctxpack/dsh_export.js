#!/usr/bin/env node
// SysPersona-OS · 本文件：dsh_export.js — 把 DSH 会话文件导出成可读 Markdown
/*
 * dsh_export.js — 把 DSH 会话文件导出成可读 Markdown
 *
 * 输入：session.v4.jsonl.zstd（多帧 zstd）
 * 输出：<out>.md（用户/助手正文 + 工具调用摘要，reasoning 不导出）
 *
 * 用法：
 *   node dsh_export.js <session.v4.jsonl.zstd> <out.md>
 *   node dsh_export.js <会话目录> <out.md>
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);

function decompressFrames(buf) {
  const offs = [];
  let i = 0;
  while ((i = buf.indexOf(MAGIC, i)) !== -1) { offs.push(i); i += 4; }
  const parts = [];
  for (let k = 0; k < offs.length; k++) {
    const end = k + 1 < offs.length ? offs[k + 1] : buf.length;
    try { parts.push(zlib.zstdDecompressSync(buf.subarray(offs[k], end))); } catch (_) { /* 跳过坏帧 */ }
  }
  return Buffer.concat(parts);
}

function loadSession(p) {
  let file = p;
  if (fs.statSync(p).isDirectory()) {
    file = path.join(p, 'session.v4.jsonl.zstd');
  }
  if (!fs.existsSync(file)) throw new Error('not found: ' + file);
  return { file, text: decompressFrames(fs.readFileSync(file)).toString('utf8') };
}

function clip(s, n) {
  s = String(s || '').replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function textOf(parts) {
  if (!Array.isArray(parts)) return '';
  return parts.filter((x) => x && x.type === 'text' && x.text).map((x) => x.text).join('\n').trim();
}

function main() {
  const [src, out] = process.argv.slice(2);
  if (!src || !out) {
    console.error('usage: node dsh_export.js <session.v4.jsonl.zstd|dir> <out.md>');
    process.exit(2);
  }
  const { file, text } = loadSession(src);
  const lines = text.split('\n').filter(Boolean);
  const outLines = [];
  let users = 0, assistants = 0, tools = 0;

  const meta = JSON.parse(lines[0]);
  outLines.push('# 会话导出', '');
  outLines.push('- 来源：`' + file.replace(/\\/g, '/') + '`');
  // 分身字段：壳是 dsh；模型取会话里第一条带 model 的事件（取不到写 unknown，不猜）。
  const modelHit = text.match(/"model"\s*:\s*"([^"]+)"/);
  outLines.push('- 设备：dsh');
  outLines.push('- 模型：' + (modelHit ? modelHit[1] : 'unknown'));
  outLines.push('- 会话：`' + (meta.id || '?') + '`');
  outLines.push('- 创建：' + new Date(meta.createdAt || 0).toISOString());
  outLines.push('- 事件：' + lines.length);
  outLines.push('');

  for (const line of lines) {
    let o;
    try { o = JSON.parse(line); } catch (_) { continue; }
    const d = o.data || {};
    if (o.type === 'user/message' || o.type === 'assistant/message') {
      const t = o.type === 'user/message' ? textOf(d.content) : textOf((d.message || {}).content);
      if (!t) continue;
      // 消息之间必须有分隔线：索引与检索都靠 "\n\n---\n\n" 切块。
      // 缺了它，整份会话会被当成一篇"文档"按段落切，用户/助手角色全丢。
      if (users + assistants > 0) outLines.push('---', '');
      outLines.push(o.type === 'user/message' ? '## 用户' : '## 助手', '', t, '');
      if (o.type === 'user/message') users++; else assistants++;
    } else if (o.type === 'tool/call') {
      outLines.push('- 🔧 `' + (d.name || '?') + '` ' + clip(d.arguments, 160));
      tools++;
    }
  }

  fs.writeFileSync(out, outLines.join('\n'), { encoding: 'utf8' });
  console.log('wrote ' + out);
  console.log('user=' + users + ' assistant=' + assistants + ' tool=' + tools + ' chars=' + outLines.join('\n').length);
}

main();
