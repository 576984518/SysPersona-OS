# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：硅基流动「现在哪些模型免费」日更（任务 A）——2026-11-10 定：一天只抓一次，后台抓，注意频率，别封号。
r"""硅基流动「现在哪些模型免费」日更（任务 A）——2026-11-10 定：一天只抓一次，后台抓，注意频率，别封号。

为什么要有这个：免费名单是短暂的、每天会变，所以每天真去页面抓一次，
而不是把名单写死在代码里。抓不到就保留旧缓存，绝不因为一次失败把旧名单清空。

频率纪律（用户 2026-11-10 追加，优先于「多抓几次更准」）：
  - 主触发 = 开机/登录（启动文件夹里一条快捷方式），**不是固定时刻**：晚上不常开机，
    定 03:10 经常空跑；
  - 闸门：缓存 ops/sf-free.json 的 at 距今 < 20 小时 → 打印「今天已经抓过」，退出 0，
    不导航、不请求；≥ 20 小时才真抓一次。一天开关机多次也只抓一次；
  - 兜底：开机那次失败（没登录/网络没起来）允许下一次开机再试，但**滚动 24 小时最多 2 次真抓**，
    超过就等明天（每次真抓都写进 ops/sf-models.jsonl）；
  - 开机时网络/CDP 可能比脚本晚：--boot 模式先轮询 CDP 端口，最多等 120 秒再开始；
  - 一次执行 = 一次导航，等渲染，抽完就关标签页（自己开的标签才关）；
  - 失败不重试：CDP/登录/结构问题都只记一条 fail，退出码 1，等下次开机；
  - 手动兜底开关：--force 无视 20h 闸门，--force-twice 连 2 次/24h 上限也无视（慎用）。

页面实况（2026-11-10 实测，和最初预期不一样，记在这里免得后人再猜）：
  - /models 会 302 到 /me/models，卡片是纯 div + onclick，**没有 <a> 链接**；
  - 卡片容器 class: relative flex cursor-pointer flex-col justify-between overflow-hidden rounded-lg border …
  - 卡片第 1 个子 div 是角标（"⚡️ 1017" / "New" / "Deprecated"），第 2 个是 id+vendor，
    第 3 个是描述，第 4 个 class 含 "flex gap-2" 的是标签行；
  - **页面不显示价格数字**，免费模型只在标签行里挂一个 "Free" 标签；
    所以 price_text 记 "Free"，付费模型记 ""（不是漏抓，是页面就没有）；
  - 下架模型（实测 80 张）的 id 不放在 div/span 里，而是 <del>vendor/Model</del>，
    旁边还有个 "Deprecated" 角标 —— 只查 div/span 会漏掉它们（踩过）；
  - 全页约 164 张卡（84 张在售 + 80 张下架）一次性渲染在 DOM 里，内层 overflow-y-auto 滚动
    **不加载新卡**，所以不用滚、更不能循环刷新；滚动 window 无效（滚动条在内层 div 上）。
  - 模型 id 只出现在叶子节点，形如 vendor/Model-Name，用它当锚点比 class 稳。

输出：ops/sf-free.json  {at, source, models:[{id,name,price_text,free,deprecated}], free_ids}
      （deprecated 是额外字段：下架模型的 id 在 <del> 里、旁边有 Deprecated 角标；
        评审器用它避开下架模型，不算改变约定的四个字段）
日志：ops/sf-models.jsonl  每次「真抓」一条（成功失败都记，没真抓不记）
退出码：0 有数据（新抓或缓存）；1 没抓到 / 被频控拒绝

CLI：--refresh 抓一次（20h 内有缓存则直接退出）/ --list 看缓存 / --free 只打印 free_ids
      --boot 开机模式（先等 CDP 起来，最多 120 秒）；--force 无视 20h 闸门；
      --force-twice 连 2 次/24h 上限也无视（显式二次确认）

开机自启（按项目习惯放启动文件夹；装一次就自动了，不装就手动跑）：
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-SFModels.lnk")
  $lnk.TargetPath = "$env:PERSONA_PY"                       # 或 <runtime>\python\pythonw.exe
  $lnk.Arguments = '"<记忆库根>\persona_sf_models.py" --refresh --boot'
  $lnk.WorkingDirectory = "<记忆库根>"
  $lnk.Save()
  （或登录型计划任务：schtasks /Create /SC ONLOGON /TN "SysPersona-SFModels" /TR "…" /F）

环境变量：PERSONA_NODE = node（不设就找 <runtime>\node\bin\node.exe、再找 PATH）；
PERSONA_PY = 上面快捷方式用的解释器；记忆库根取本脚本所在目录。
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from persona_root import node as _node

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
CACHE = os.path.join(OPS, "sf-free.json")
LOG = os.path.join(OPS, "sf-models.jsonl")
CDP_PORT = int(os.environ.get("CDP_PORT") or 9222)
MODELS_URL = "https://cloud.siliconflow.cn/models"
STALE_HOURS = 24        # --list 超过这么久提示过期
CACHE_GATE_HOURS = 20   # 闸门：缓存 20 小时内不再抓（一天一次）
MAX_REAL_24H = 2        # 滚动 24 小时最多真抓 2 次（开机失败允许下一次开机再试）
BOOT_WAIT_SECS = 120    # 开机模式等 CDP/网络起来的上限

NODE = _node()
if not NODE:
    NODE = shutil.which("node") or "node"   # 最后才交给 PATH；真没有时下方会报"找不到 node"

# Node 侧脚本：CDP 抓卡片。结果写到 argv[2] 指定的文件，stdout 不承担数据。
COLLECT_JS = r"""
const http = require('http');
const fs = require('fs');
const OUT = process.argv[2];
const PORT = parseInt(process.argv[3] || '9222', 10);
const TARGET = 'https://cloud.siliconflow.cn/models';
const R = { ok: false, reason: '', url: '', title: '', cookie_count: 0, has_auth: false,
            models: [], note: '', secs: 0, navigated: false };
let CREATED = false, TABID = '';
const T0 = Date.now();
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function finish(code) {
  R.secs = +((Date.now() - T0) / 1000).toFixed(1);
  try { fs.writeFileSync(OUT, JSON.stringify(R), 'utf8'); } catch (e) {}
  let exited = false;
  const done = () => { if (!exited) { exited = true; process.exit(code); } };
  // 自己开的标签才关；复用的标签不动
  if (CREATED && TABID) {
    try {
      const req = http.request({ host: '127.0.0.1', port: PORT, path: '/json/close/' + TABID, method: 'GET' }, () => done());
      req.on('error', done);
      req.setTimeout(1500, () => { try { req.destroy(); } catch (e) {} done(); });
      req.end();
      setTimeout(done, 2000);
      return;
    } catch (e) {}
  }
  done();
}
function httpJson(method, p) {
  return new Promise((res, rej) => {
    const req = http.request({ host: '127.0.0.1', port: PORT, path: p, method }, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(new Error('CDP 返回不是 JSON')); } });
    });
    req.on('error', (e) => rej(new Error('CDP 不通：' + (e.code || e.message))));
    req.setTimeout(5000, () => req.destroy(new Error('CDP 超时')));
    req.end();
  });
}

// 抽卡表达式：一次求值，不滚动、不点击、不 reload。
const EXPR = `(() => {
  const ID_RE = /^[A-Za-z0-9._-]+\\/[A-Za-z0-9._-]+$/;
  const FREE_RE = /^(free|免费|免费模型|0\\s*元|[¥￥]\\s*0(\\.0+)?)$/i;
  const PRICE_RE = /[¥￥]\\s*[\\d.]+|\\d[\\d.,]*\\s*元|free|免费/i;
  const leaves = Array.from(document.querySelectorAll('*')).filter((e) => !e.children.length);
  const seen = new Set();
  const models = [];
  for (const el of leaves) {
    const id = (el.textContent || '').trim();
    if (!id || id.length > 80 || !ID_RE.test(id) || seen.has(id)) continue;
    seen.add(id);
    let card = el;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      card = card.parentElement;
      if (/(^|\\s)cursor-pointer(\\s|$)/.test(String(card.className || ''))) break;
    }
    let row = null;
    for (const ch of Array.from(card.children)) {
      const cls = String(ch.className || '');
      if (/flex/.test(cls) && /gap-2/.test(cls)) { row = ch; break; }
    }
    const tags = [];
    for (const t of Array.from((row || card).querySelectorAll('span,div,del'))) {
      if (t.children.length) continue;
      const txt = (t.textContent || '').trim();
      if (txt && txt.length <= 40) tags.push(txt);
    }
    const freeTag = tags.find((t) => FREE_RE.test(t)) || '';
    const priceTag = tags.find((t) => PRICE_RE.test(t)) || '';
    const deprecated = !!el.closest('del') || tags.indexOf('Deprecated') >= 0;
    const free = !!freeTag || /free|免费/i.test(priceTag) || /[¥￥]\\s*0(\\.0+)?(?!\\d)/.test(priceTag);
    models.push({ id: id, name: id, price_text: freeTag || priceTag || '', free: free, deprecated: deprecated });
  }
  return JSON.stringify({
    models: models,
    tagRows: models.filter((m) => m.price_text).length,
    bodyLen: (document.body.innerText || '').length
  });
})()`;

(async () => {
  let list;
  try { list = await httpJson('GET', '/json'); } catch (e) { R.reason = e.message; return finish(1); }
  let page = (list || []).find((t) => t.type === 'page' && (t.url || '').indexOf('cloud.siliconflow.cn') >= 0);
  if (!page) {
    try { page = await httpJson('PUT', '/json/new?' + encodeURIComponent(TARGET)); CREATED = true; TABID = page.id; }
    catch (e) { R.reason = '开不了新标签：' + e.message; return finish(1); }
  }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let seq = 0;
  const waiters = new Map();
  function send(method, params) {
    return new Promise((res, rej) => {
      const id = ++seq;
      waiters.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => { if (waiters.has(id)) { waiters.delete(id); rej(new Error(method + ' 超时')); } }, 30000);
    });
  }
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    const w = m.id && waiters.get(m.id);
    if (!w) return;
    waiters.delete(m.id);
    if (m.error) w.rej(new Error((m.method || 'CDP') + '：' + (m.error.message || JSON.stringify(m.error))));
    else w.res(m.result);
  };
  try {
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error('WebSocket 连不上'));
      setTimeout(() => rej(new Error('WebSocket 连接超时')), 8000);
    });
  } catch (e) { R.reason = e.message; return finish(1); }

  try {
    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');

    let cookies = { cookies: [] };
    try { cookies = await send('Network.getAllCookies'); } catch (e) {}
    const names = (cookies.cookies || []).map((c) => c.name);
    R.cookie_count = names.length;
    R.has_auth = names.some((n) => n.indexOf('__SF_auth') === 0 || n === 'session-token');

    // 一次执行只导航一次；不复用可能放了一天的旧页面
    await send('Page.navigate', { url: TARGET });
    R.navigated = true;
    let ready = '';
    const tn = Date.now();
    while (Date.now() - tn < 25000) {
      await sleep(400);
      try {
        const r = await send('Runtime.evaluate',
          { expression: 'JSON.stringify([document.readyState, location.href])', returnByValue: true });
        const pair = JSON.parse(r.result.value);
        ready = pair[0];
        if (ready === 'complete' && String(pair[1] || '').indexOf('siliconflow') >= 0) break;
      } catch (e) {}
    }
    if (ready !== 'complete') { R.reason = '页面 25 秒没加载完'; return finish(1); }
    await sleep(2500);

    // 等卡片出现（最多 8 秒），不刷新
    let got = null;
    const tc = Date.now();
    while (Date.now() - tc < 8000) {
      const r = await send('Runtime.evaluate', { expression: EXPR, returnByValue: true });
      if (r.exceptionDetails) { R.reason = '页面脚本报错：' + (r.exceptionDetails.text || ''); return finish(1); }
      got = JSON.parse(r.result.value);
      if ((got.models || []).length >= 5) break;
      await sleep(800);
    }
    if (!got) { R.reason = '没跑出结果'; return finish(1); }
    R.models = got.models || [];
    R.note = '';

    if (!R.has_auth) { R.reason = '没登录（缺 __SF_auth.session-token）'; return finish(1); }
    if (R.models.length < 5) { R.reason = '只抽到 ' + R.models.length + ' 个模型，页面结构大概变了'; return finish(1); }
    const nFree = R.models.filter((m) => m.free).length;
    if (!nFree) R.note = '免费名单为空（也许今天真没有，也许标签改名了）';
    R.ok = true;
    return finish(0);
  } catch (e) {
    R.reason = 'CDP 命令失败：' + e.message;
    return finish(1);
  }
})();
"""


def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _log(rec):
    os.makedirs(OPS, exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_cache():
    try:
        with io.open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _hours_since(stamp):
    try:
        t = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        return max(0.0, (time.time() - t) / 3600.0)
    except (KeyError, TypeError, ValueError):
        return 1e9


def _last_real_fetch():
    """返回 (小时数, 记录)；没有日志返回 (None, None)。"""
    try:
        with io.open(LOG, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return None, None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("real"):
            return _hours_since(rec.get("at", "")), rec
    return None, None


def _real_fetches_24h():
    """滚动 24 小时内真抓了几次（失败也算一次，防反复试）。"""
    n = 0
    try:
        with io.open(LOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if rec.get("real") and _hours_since(rec.get("at", "")) < 24:
                    n += 1
    except OSError:
        pass
    return n


def wait_cdp(max_secs=BOOT_WAIT_SECS):
    """开机模式：等 CDP 端口起来（网络/浏览器可能比脚本晚），严格不超过 max_secs。"""
    url = "http://127.0.0.1:%d/json/version" % CDP_PORT
    deadline = time.time() + max_secs
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            return False
        try:
            with urllib.request.urlopen(url, timeout=min(2.0, remain)) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(min(3.0, max(0.0, deadline - time.time())))


def grab():
    """真去 CDP 抓一次。返回 (ok, payload_or_reason)。失败不重试。"""
    tmpdir = tempfile.mkdtemp(prefix="sf_models_")
    js_path = os.path.join(tmpdir, "collect.js")
    out_path = os.path.join(tmpdir, "out.json")
    try:
        with io.open(js_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(COLLECT_JS)
        try:
            p = subprocess.run([NODE, js_path, out_path, str(CDP_PORT)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except FileNotFoundError:
            return False, "找不到 node（%s）" % NODE
        except subprocess.TimeoutExpired:
            return False, "node 120 秒没跑完"
        try:
            with io.open(out_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            return False, "node 没交结果" + ("：" + err[-1] if err else "")
        if not data.get("ok"):
            return False, data.get("reason") or "未知原因"
        return True, data
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def do_refresh(force, force_twice, boot=False):
    old = _read_cache()
    age = _hours_since(old.get("at", "")) if old else None

    # 1) 闸门：20 小时内抓过就什么都不做（不开页面、不导航、不发请求）
    if old and age is not None and age < CACHE_GATE_HOURS and not (force or force_twice):
        print("今天已经抓过（%.1f 小时前，缓存 %s），用缓存：%d 个模型 / %d 个免费。"
              % (age, old.get("at", "?"), len(old.get("models") or []), len(old.get("free_ids") or [])))
        return 0

    # 2) 兜底上限：滚动 24 小时最多真抓 2 次（开机失败允许下次开机再试，但不能反复试）
    n24 = _real_fetches_24h()
    if n24 >= MAX_REAL_24H and not force_twice:
        print("24 小时内已真抓 %d 次（上限 %d），今天不再抓；等明天开机。"
              % (n24, MAX_REAL_24H))
        return 1

    # 3) 开机模式：先等 CDP/网络起来，最多 120 秒
    if boot:
        print("开机模式：等 CDP 起来（端口 %d，最多 %d 秒）…" % (CDP_PORT, BOOT_WAIT_SECS))
        if not wait_cdp():
            print("CDP %d 秒内没起来，这次跳过（等下次开机）；旧缓存保留。" % BOOT_WAIT_SECS)
            _log({"at": _iso(), "real": False, "ok": False,
                  "reason": "开机等 CDP 超时（%d 秒）" % BOOT_WAIT_SECS, "secs": BOOT_WAIT_SECS})
            return 1

    # 4) 一次导航，抓一次，不重试
    print("去抓一次（%s，端口 %d）…" % (MODELS_URL, CDP_PORT))
    started = time.time()
    ok, res = grab()
    secs = round(time.time() - started, 1)

    if not ok:
        reason = res
        _log({"at": _iso(), "real": True, "ok": False, "reason": reason, "secs": secs})
        if old:
            print("没抓到（%s），继续用 %.1f 小时前的旧名单（%d 个免费）；今天不再重试。"
                  % (reason, age if age is not None else 0, len(old.get("free_ids") or [])))
        else:
            print("没抓到（%s），也没有旧名单可退；今天不再重试。" % reason)
        return 1

    models = res.get("models") or []
    free_ids = [m["id"] for m in models if m.get("free")]
    payload = {
        "at": _iso(),
        "source": "cdp:cloud.siliconflow.cn/models",
        "models": models,
        "free_ids": free_ids,
    }
    os.makedirs(OPS, exist_ok=True)
    _write_atomic(CACHE, json.dumps(payload, ensure_ascii=False, indent=1))
    _log({"at": payload["at"], "real": True, "ok": True, "reason": "",
          "n_models": len(models), "n_free": len(free_ids), "secs": secs,
          "navigated": bool(res.get("navigated"))})

    print("已抓取：%d 个模型，免费 %d 个（耗时 %.1f 秒）" % (len(models), len(free_ids), secs))
    print("cookie 数量：%s（不打印值）" % res.get("cookie_count", "?"))
    if res.get("note"):
        print("注意：" + res["note"])
    print("缓存：ops/sf-free.json")
    return 0


def do_list():
    data = _read_cache()
    if not data:
        print("还没有缓存，先跑 --refresh")
        return 1
    age = _hours_since(data.get("at", ""))
    print("缓存时间：%s（%.1f 小时前）" % (data.get("at", "?"), age))
    print("来源：%s" % data.get("source", "?"))
    print("模型 %d 个，免费 %d 个" % (len(data.get("models") or []), len(data.get("free_ids") or [])))
    if age > STALE_HOURS:
        print("缓存过期：超过 %d 小时，建议跑 --refresh" % STALE_HOURS)
    for m in data.get("models") or []:
        mark = "[免费]" if m.get("free") else ""
        if m.get("deprecated"):
            mark = (mark + "[下架]") if mark else "[下架]"
        print("%-9s %-44s %s" % (mark, m.get("id", ""), m.get("price_text", "")))
    return 0


def do_free():
    data = _read_cache()
    if not data:
        sys.stderr.write("还没有缓存，先跑 --refresh\n")
        return 1
    age = _hours_since(data.get("at", ""))
    if age > STALE_HOURS:
        sys.stderr.write("缓存过期：%.1f 小时前，建议 --refresh\n" % age)
    for mid in data.get("free_ids") or []:
        print(mid)
    return 0


def main():
    ap = argparse.ArgumentParser(description="硅基流动免费模型日更（开机抓一次，20h 闸门，防封号）")
    ap.add_argument("--refresh", action="store_true", help="抓一次（20h 内有缓存则直接用缓存）")
    ap.add_argument("--list", action="store_true", help="打印缓存")
    ap.add_argument("--free", action="store_true", help="只打印 free_ids")
    ap.add_argument("--boot", action="store_true", help="开机模式：先等 CDP 起来（最多 120 秒）")
    ap.add_argument("--force", action="store_true", help="无视 20h 闸门（仍受 2 次/24h 上限）")
    ap.add_argument("--force-twice", action="store_true", dest="force_twice",
                    help="连 2 次/24h 上限也无视（显式二次确认，慎用）")
    a = ap.parse_args()
    if a.refresh or a.force or a.force_twice:
        return do_refresh(a.force, a.force_twice, a.boot)
    if a.free:
        return do_free()
    if a.list:
        return do_list()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
