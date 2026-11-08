#!/usr/bin/env node
/**
 * watch_nav.js — 页面跳转监听器（替代每分钟轮询）
 *
 * 原理：一次性脚本只能"采样"，看不到事件。这里常驻挂在 CDP 上，
 * 订阅 Page.frameNavigated / Page.loadEventFired —— 你按 F5 或切回会话时，
 * 1–2 秒内就会触发一次深抓（复用 deepseek_sweep.js --active）。
 *
 * 用法：
 *   node watch_nav.js <会话输出目录> [heartbeat文件]
 *
 * 它只做两件事：跳转就抓、活着就写心跳。不做轮询兜底——
 * 不按 F5，DOM 不会变，轮询本来就是空转。
 */
import { spawn } from 'node:child_process';
import { writeFileSync, appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = 'http://127.0.0.1:9222';
const OUT = process.argv[2] || join(HERE, 'sessions');
const HB = process.argv[3] || join(process.env.LOCALAPPDATA || HERE, 'SysPersona', 'navwatch.heartbeat');
const LOG = join(dirname(HB), 'navwatch.log');

const DEBOUNCE_MS = 6000;   // 重载后 CDP target 会换，等页面稳定再抓
let ws = null, capturing = false, pending = null, lastLog = 0, retries = 0;
const STATE = join(process.env.LOCALAPPDATA || HERE, 'SysPersona', 'last.json');

function log(msg) {
  const line = `${new Date().toLocaleString('sv-SE').slice(5, 19)}  ${msg}\n`;
  try { mkdirSync(dirname(LOG), { recursive: true }); appendFileSync(LOG, line); } catch {}
}

function beat(status) {
  try { mkdirSync(dirname(HB), { recursive: true }); writeFileSync(HB, JSON.stringify({ at: Date.now(), status })); } catch {}
}

async function findTarget() {
  try {
    const r = await fetch(BASE + '/json/list');
    const list = await r.json();
    return list.find(t => t.type === 'page' && (t.url || '').includes('chat.deepseek.com')) || null;
  } catch { return null; }
}

function capture(reason) {
  if (capturing) { pending = reason; return; }
  capturing = true;
  log(`触发抓取（${reason}）`);
  // 必须走 PowerShell 包装脚本：归档 + 重出 HANDOFF + 写 last.json 都在它里面。
  // 直接调 deepseek_sweep.js 只会落盘 .md，后面三步全被绕过（2026-11-08 踩过）。
  const ps = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';
  const wrapper = join(process.env.LOCALAPPDATA || HERE, 'SysPersona', 'sweep_live.ps1');
  const p = spawn(ps, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', wrapper], { stdio: 'ignore' });
  p.on('exit', (code) => {
    capturing = false;
    log(`抓取结束 code=${code}`);
    beat('idle');
    // 重载瞬间 CDP target 会被替换，抓取可能拿到旧句柄而失败——自动重试最多两次
    let st = '';
    try { st = JSON.parse(readFileSync(STATE, 'utf8')).status || ''; } catch {}
    if (st === 'FAIL' && retries < 2) {
      retries += 1;
      log(`抓取失败（${st}），第 ${retries} 次重试`);
      setTimeout(() => capture('retry'), 6000);
      return;
    }
    retries = 0;
    if (pending) { const r = pending; pending = null; setTimeout(() => capture(r + ':queued'), DEBOUNCE_MS); }
  });
}

function schedule(reason) {
  if (schedule.timer) clearTimeout(schedule.timer);
  schedule.timer = setTimeout(() => { schedule.timer = null; capture(reason); }, DEBOUNCE_MS);
}

async function attach() {
  const t = await findTarget();
  if (!t) {
    if (Date.now() - lastLog > 300000) { lastLog = Date.now(); log('未找到 DeepSeek 标签页，等待…'); }
    beat('no-target');
    setTimeout(attach, 5000);
    return;
  }
  log(`已附着：${t.title || t.url}`);
  beat('attached');
  ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 0;
  const send = (method, params) => ws.send(JSON.stringify({ id: ++id, method, params: params || {} }));

  ws.onopen = () => { send('Page.enable'); send('Runtime.enable'); };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.method === 'Page.frameNavigated') {
      const url = m.params?.frame?.url || '';
      if (url.includes('chat.deepseek.com') && !m.params.frame.parentId) schedule('frameNavigated');
    } else if (m.method === 'Page.loadEventFired') {
      schedule('loadEvent');
    } else if (m.method === 'Page.navigatedWithinDocument') {
      // 单页应用里点侧边栏切会话不会整页刷新，只发这个事件
      const url = m.params?.url || '';
      if (url.includes('chat.deepseek.com')) schedule('withinDocument');
    }
  };
  ws.onclose = () => { beat('detached'); log('CDP 断开，2 秒后重连'); setTimeout(attach, 2000); };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

setInterval(() => beat(ws ? 'attached' : 'retrying'), 30000);
log(`监听器启动，输出目录 ${OUT}`);
attach();
