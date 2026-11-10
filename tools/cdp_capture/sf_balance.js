#!/usr/bin/env node
/*
 * sf_balance.js — 读硅基流动「余额充值 / 费用明细」页上的余额（只读，一天最多几次）
 *
 * 为什么存在：硅基的余额没有可用的 API（`GET /v1/user/info` 2026-11-10 起 410 Gone），
 * 只能读登录态页面。人给的页：https://cloud.siliconflow.cn/me/expensebill
 * 页面里那行是：`余额 ¥9.7636…`（下一行「（剩余可透支额度 ¥ 0）」）。
 *
 * 频率纪律（同 persona_sf_models 的路子）：
 *   - 闸门：缓存 ops/sf-balance.json 的 at 距今 < 6 小时 → 直接退出 0，不导航；
 *   - 滚动 24 小时最多 6 次真抓，超了等下次（每次真抓写 ops/sf-balance.jsonl）；
 *   - 一次执行 = 一次导航，抽完就关自己开的标签页；
 *   - 失败只记一条，保留旧缓存，绝不把旧数字清空。
 *
 * 用法：node sf_balance.js [--force] [--print]
 * 输出：ops/sf-balance.json {at, balance, overdraft, currency, source, raw}
 * 退出码：0 有数据（新抓或缓存）/ 1 没抓到 / 2 被频控拒绝
 */
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const OPS = path.join(ROOT, 'ops');
const OUT = path.join(OPS, 'sf-balance.json');
const LOG = path.join(OPS, 'sf-balance.jsonl');
const URL_ = 'https://cloud.siliconflow.cn/me/expensebill';
const GATE_HOURS = 6;
const MAX_PER_DAY = 6;

const argv = process.argv.slice(2);
const FORCE = argv.includes('--force');
const PRINT = argv.includes('--print');

// 本地时间「YYYY-MM-DD HH:mm:ss」——别用 toISOString（那是 UTC，差 8 小时）
function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function readJSON(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8').replace(/^\uFEFF/, '')); }
  catch (e) { return null; }
}
function writeJSON(p, d) {
  const tmp = p + '.' + process.pid + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(d, null, 2), 'utf8');
  fs.renameSync(tmp, p);
}
function appendLog(rec) {
  try { fs.appendFileSync(LOG, JSON.stringify(rec) + '\n', 'utf8'); } catch (e) { /* 记不上不挡事 */ }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdpList() {
  const r = await fetch('http://127.0.0.1:9222/json/list');
  return r.json();
}

async function openTab() {
  const u = 'http://127.0.0.1:9222/json/new?' + encodeURIComponent(URL_);
  try {
    const r = await fetch(u, { method: 'PUT' });
    if (r.ok) return r.json();
  } catch (e) { /* 老版本 Chrome 只认 GET */ }
  const r2 = await fetch(u);
  return r2.json();
}

function evaluate(page, expr) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0;
    const pend = new Map();
    const send = (method, params) => new Promise((res) => {
      const i = ++id; pend.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    });
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
    };
    ws.onerror = (e) => reject(new Error('WS: ' + String(e && e.message)));
    ws.onopen = async () => {
      try {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
        ws.close();
        resolve(r.result && r.result.result ? r.result.result.value : null);
      } catch (e) { ws.close(); reject(e); }
    };
  });
}

const READ_EXPR = `(() => {
  const t = document.body ? (document.body.innerText || '') : '';
  return JSON.stringify({ len: t.length, text: t.slice(0, 4000) });
})()`;

function parse(text) {
  const bal = text.match(/余额\s*[¥￥]\s*([\d,]+(?:\.\d+)?)/);
  const od = text.match(/剩余可透支额度\s*[¥￥]\s*([\d,]+(?:\.\d+)?)/);
  const num = (m) => (m ? Number(m[1].replace(/,/g, '')) : null);
  return { balance: num(bal), overdraft: num(od) };
}

async function main() {
  const cached = readJSON(OUT);
  const prevTs = cached && cached.at ? Date.parse(cached.at.replace(' ', 'T')) : 0;
  const ageH = prevTs ? (Date.now() - prevTs) / 3600000 : 1e9;

  if (!FORCE && ageH < GATE_HOURS) {
    const msg = `余额 ${cached.balance}（${ageH.toFixed(1)} 小时前抓的，不到 ${GATE_HOURS} 小时不重抓）`;
    console.log(msg);
    if (PRINT) console.log(JSON.stringify(cached, null, 2));
    return 0;
  }

  const dayAgo = Date.now() - 86400000;
  let real = 0;
  try {
    for (const line of fs.readFileSync(LOG, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      try { const r = JSON.parse(line); if (Date.parse((r.at || '').replace(' ', 'T')) > dayAgo) real += 1; } catch (e) { /* 跳过坏行 */ }
    }
  } catch (e) { real = 0; }
  if (!FORCE && real >= MAX_PER_DAY) {
    console.log(`24 小时内已真抓 ${real} 次（上限 ${MAX_PER_DAY}），等下次`);
    appendLog({ at: stamp(), ok: false, why: 'rate_limited', real_24h: real });
    return 2;
  }

  let page = null, own = false;
  try {
    const list = await cdpList();
    page = (list || []).find((t) => t.type === 'page' && (t.url || '').includes('/me/expensebill'));
    if (!page) { page = await openTab(); own = true; await sleep(6000); }
  } catch (e) {
    console.error('CDP 不可用：' + String(e && e.message));
    appendLog({ at: stamp(), ok: false, why: 'cdp_down' });
    return 1;
  }

  try {
    let raw = '';
    for (let i = 0; i < 4; i++) {
      const val = await evaluate(page, READ_EXPR);
      raw = (() => { try { return JSON.parse(val).text || ''; } catch (e) { return ''; } })();
      if (/余额\s*[¥￥]\s*[\d,]+/.test(raw)) break;
      await sleep(2500);          // 页面还没渲染完 / 还在登录跳转
    }
    const got = parse(raw);
    if (got.balance === null) {
      console.error('没读到余额（页面里没有「余额 ¥数字」），保留旧缓存');
      appendLog({ at: stamp(), ok: false, why: 'no_balance', chars: raw.length });
      return 1;
    }
    const snap = {
      at: stamp(), balance: got.balance, overdraft: got.overdraft,
      currency: 'CNY', source: 'cdp:cloud.siliconflow.cn/me/expensebill',
      raw: (raw.match(/余额[\s\S]{0,80}/) || [''])[0].replace(/\s+/g, ' ').trim(),
    };
    writeJSON(OUT, snap);
    appendLog({ at: snap.at, ok: true, balance: snap.balance, overdraft: snap.overdraft });
    console.log(`余额 ¥${snap.balance}${snap.overdraft !== null ? `（可透支 ¥${snap.overdraft}）` : ''}`);
    if (PRINT) console.log(JSON.stringify(snap, null, 2));
    return 0;
  } catch (e) {
    console.error('读取失败：' + String(e && e.message));
    appendLog({ at: stamp(), ok: false, why: 'read_failed', err: String(e && e.message).slice(0, 120) });
    return 1;
  } finally {
    if (own && page && page.id) {
      try { await fetch('http://127.0.0.1:9222/json/close/' + page.id); } catch (e) { /* 关不掉就算了 */ }
    }
  }
}

main().then((c) => process.exit(c)).catch((e) => { console.error(e); process.exit(1); });
