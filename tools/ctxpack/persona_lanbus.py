# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_lanbus.py — SysPersona 记忆层「局域网节点服务」（服务端 + 客户端）
r"""persona_lanbus.py — SysPersona 记忆层「局域网节点服务」（服务端 + 客户端）

为什么存在：persona_cloud_bus.py 只管"多个程序共用一个目录时怎么写不坏"，
传输本身交给网盘。本文件补的是**节点间通道**：指定一台机器当服务端，
其余设备经 ZeroTier / Tailscale 之类的虚拟组网连上来，做增量同步。

协议（版本 1，纯标准库，HTTP/1.1）：
  GET  /health             探活，**不需要令牌**，不含任何路径信息
  GET  /manifest           白名单目录清单 {files:[{path,size,sha256,mtime}]}，需令牌
  GET  /blob?path=...      取原始字节，响应头 X-SHA256，需令牌
  POST /blob?path=...      上传字节（默认上限 64MB），落盘前校验 X-SHA256，需令牌

鉴权：共享令牌 PERSONA_LAN_TOKEN（环境变量优先，其次
%LOCALAPPDATA%\SysPersona\lan-token.txt），hmac.compare_digest 比对；
**取不到令牌服务端拒绝启动**，没有"默认无鉴权"这一档。
所有响应带 X-Node: <主机名>，便于在日志里分辨是谁。

白名单：
  只读  index/  archive/  续接/
  可写  index/（同步产物）  ops/lanbus/（拉取暂存 / 拒收残片）
  其余一律 400；含 ..、绝对路径、盘符/冒号的请求直接 400。
  写盘一律「临时文件 → fsync → os.replace」，不删任何文件。

安全默认：--bind 默认 127.0.0.1（只本机）。要对外必须显式 --bind，
启动时会打印本机全部 IPv4 并提示「只在虚拟组网网段暴露」。

用法：
  python persona_lanbus.py token
  python persona_lanbus.py serve --port 8737 [--bind 127.0.0.1] [--root <库根>]
  python persona_lanbus.py sync  --server http://192.168.1.20:8737 [--dry-run] [--only index/,续接/]

本机的 Python 解释器不在 PATH 时，用环境变量指定（见 docs/USAGE.md「环境变量」一节）：
  set "PERSONA_PY=<你的 python.exe 绝对路径>"
  后面所有命令都写成 "%PERSONA_PY%" persona_lanbus.py ...
"""

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

VERSION = 1
DEFAULT_PORT = 8737
READ_DIRS = ("index", "archive", "续接")
WRITE_DIRS = ("index",)                 # 同步产物
STAGE_REL = "ops/lanbus"                # 拉取暂存 / 拒收残片
TOKEN_ENV = "PERSONA_LAN_TOKEN"
HOST = (socket.gethostname() or "unknown").strip()
NAME = "persona_lanbus"
LOG_LOCK = threading.Lock()


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# 64MB；测试可用 PERSONA_LANBUS_MAX_BYTES 临时收小（只影响上限，不改协议）
MAX_BYTES = _env_int("PERSONA_LANBUS_MAX_BYTES", 64 * 1024 * 1024)

ROOT = ""
REAL_ROOT = ""
OPS = ""
LOG = ""
STATE = ""
TOKEN = ""
SHA_CACHE = {}
SHA_LOCK = threading.Lock()


# ── 基础 ────────────────────────────────────────────────────────────
def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def token_path():
    """令牌文件：%LOCALAPPDATA%\\SysPersona\\lan-token.txt（Linux/mac 兜底到 ~/.SysPersona）。"""
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".SysPersona")
        return os.path.join(base, "lan-token.txt")
    return os.path.join(base, "SysPersona", "lan-token.txt")


def load_token():
    """返回 (令牌, 来源说明)。环境变量优先，其次令牌文件；都没有返回空串。"""
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env, "环境变量 %s" % TOKEN_ENV
    p = token_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                tok = f.read().strip()
            if tok:
                return tok, "文件 %s" % p
        except OSError:
            pass
    return "", "未配置"


def _token_matches(got):
    """定长比较；bytes 比较避开 hmac.compare_digest 对非 ASCII str 的限制。"""
    try:
        return hmac.compare_digest(got.encode("utf-8"), TOKEN.encode("utf-8"))
    except (UnicodeError, AttributeError):
        return False


def default_root():
    env = (os.environ.get("SYSPERSONA_ROOT") or "").strip()
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import persona_root  # 仓库里的唯一实现
        return os.path.abspath(persona_root.find_root())
    except Exception:
        pass
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, "archive")):
            return cand
    return here


def configure_root(path):
    global ROOT, REAL_ROOT, OPS, LOG, STATE
    ROOT = os.path.abspath(path)
    REAL_ROOT = os.path.realpath(ROOT)
    OPS = os.path.join(ROOT, "ops")
    LOG = os.path.join(OPS, "lanbus.jsonl")
    STATE = os.path.join(OPS, "lanbus.json")


def atomic_write_bytes(path, data):
    """临时文件 → fsync → os.replace。失败也不删残片，改名 .failed 留证。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-lanbus-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.replace(tmp, tmp + ".failed")
        except OSError:
            pass
        raise


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))


def append_log(rec):
    """一行一条 JSON。不写令牌；写日志失败也绝不影响请求本身。"""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with LOG_LOCK:
            with open(LOG, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
    except OSError:
        pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def file_sha(path, rel):
    """按 (大小, mtime_ns) 缓存：manifest 反复扫描时不必重复哈希几百 MB。"""
    st = os.stat(path)
    with SHA_LOCK:
        c = SHA_CACHE.get(rel)
    if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
        return c[2], st
    digest = sha256_file(path)
    with SHA_LOCK:
        SHA_CACHE[rel] = (st.st_size, st.st_mtime_ns, digest)
    return digest, st


def local_ipv4():
    ips = set()
    try:
        for info in socket.getaddrinfo(HOST, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))      # 不发包，只为让系统选出口地址
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
    ips.add("127.0.0.1")
    return sorted(ips)


# ── 路径白名单 ──────────────────────────────────────────────────────
class BadPath(Exception):
    pass


def normalize_rel(raw):
    """规范化成 / 分隔的相对路径；任何可疑形态抛 BadPath。"""
    if raw is None:
        raise BadPath("缺少 path 参数")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    p = str(raw).strip()
    if not p:
        raise BadPath("path 为空")
    if "\x00" in p:
        raise BadPath("path 含 NUL")
    if p.startswith(("/", "\\")):
        raise BadPath("不允许绝对路径")
    if ":" in p:
        raise BadPath("不允许盘符/冒号路径")
    p = p.replace("\\", "/")
    segs = p.split("/")
    for s in segs:
        if s in ("", ".", ".."):
            raise BadPath("不允许空段 / . / ..")
    if len(segs) < 2:
        raise BadPath("path 必须落在白名单目录内的文件上")
    return "/".join(segs), segs


def check_readable(segs):
    if segs[0] not in READ_DIRS:
        raise BadPath("不在只读白名单（%s）内" % "/ ".join(d + "/" for d in READ_DIRS))


def check_writable(segs):
    if segs[0] in WRITE_DIRS:
        return
    if len(segs) >= 3 and segs[0] == "ops" and segs[1] == "lanbus":
        return
    raise BadPath("不在可写白名单（index/、ops/lanbus/）内")


def abs_under_root(segs):
    """拼绝对路径，并确认 realpath 没跑出库根（防符号链接/联接点越界）。"""
    full = os.path.join(ROOT, *segs)
    real = os.path.realpath(full)
    if real != REAL_ROOT and not real.startswith(REAL_ROOT + os.sep):
        raise BadPath("路径越界（realpath 不在库根内）")
    return full


def _include_name(name):
    if not name or name.startswith(".") or name.startswith("~$"):
        return False
    low = name.lower()
    return not (low.endswith(".tmp") or low.endswith(".part") or low.endswith(".failed"))


def iter_read_files():
    """稳定顺序遍历白名单只读目录。跳过 symlink、临时残片、Office 锁文件。"""
    for d in READ_DIRS:
        top = os.path.join(ROOT, d)
        if not os.path.isdir(top):
            continue
        for dp, dns, fns in os.walk(top, followlinks=False):
            dns.sort()
            dns[:] = [x for x in dns if not x.startswith(".")]
            for fn in sorted(fns):
                if not _include_name(fn):
                    continue
                full = os.path.join(dp, fn)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                yield rel, full


def build_manifest():
    out = []
    for rel, full in iter_read_files():
        try:
            digest, st = file_sha(full, rel)
        except OSError:
            continue
        out.append({"path": rel, "size": st.st_size,
                    "sha256": digest, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["path"])
    return out


def count_read_files():
    n = 0
    for _ in iter_read_files():
        n += 1
    return n


def quarantine(tmp, tag):
    """拒收的残片移到 ops/lanbus/，不删（约束：不删任何文件）。"""
    try:
        qd = os.path.join(ROOT, "ops", "lanbus")
        os.makedirs(qd, exist_ok=True)
        name = "rejected-%s-%s-%s.part" % (tag, time.strftime("%Y%m%d-%H%M%S"),
                                           os.urandom(3).hex())
        os.replace(tmp, os.path.join(qd, name))
        return name
    except OSError:
        return ""


# ── HTTP 服务端 ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SysPersonaLanBus/1"

    def log_message(self, fmt, *args):        # 默认 stderr 日志不要，统一走 jsonl
        pass

    # ---- 分发 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        self.close_connection = True
        u = urlsplit(self.path)
        self._method = method
        self._route = u.path
        self._query = parse_qs(u.query, keep_blank_values=True)
        self._target = ""
        self._ip = self.client_address[0]
        self._replied = False
        try:
            if method == "GET" and self._route == "/health":
                return self._health()
            if method == "GET" and self._route == "/manifest":
                if not self._authorized():
                    return self._deny(401, "缺少或错误的 Bearer 令牌")
                return self._manifest()
            if method == "GET" and self._route == "/blob":
                if not self._authorized():
                    return self._deny(401, "缺少或错误的 Bearer 令牌")
                return self._blob_get()
            if method == "POST" and self._route == "/blob":
                if not self._authorized():
                    return self._deny(401, "缺少或错误的 Bearer 令牌")
                return self._blob_put()
            return self._deny(404, "未知路由：%s %s" % (method, self._route))
        except BadPath as e:
            return self._deny(400, "非法请求：%s" % e)
        except Exception as e:                       # 不把异常细节回给对端
            if self._replied:
                self._log(500, "aborted: %s" % e.__class__.__name__, 0)
                return
            return self._deny(500, "服务端异常：%s" % e.__class__.__name__)

    # ---- 鉴权 ----
    def _authorized(self):
        raw = self.headers.get("Authorization") or ""
        parts = raw.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        return _token_matches(parts[1].strip())

    # ---- 响应 ----
    def _reply(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self._replied = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Node", HOST)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except OSError:
                self.close_connection = True

    def _err(self, code, msg):
        body = json.dumps({"ok": False, "error": msg}, ensure_ascii=False).encode("utf-8")
        extra = {"WWW-Authenticate": 'Bearer realm="lanbus"'} if code == 401 else None
        self._reply(code, body, extra=extra)

    def _deny(self, code, msg):
        self._err(code, msg)
        self._log(code, msg, 0)

    def _log(self, code, result, nbytes):
        rec = {"at": now(), "method": self._method, "path": self._route,
               "bytes": int(nbytes), "status": code, "result": result,
               "ip": self._ip, "node": HOST}
        if self._target:
            rec["target"] = self._target
        append_log(rec)
        try:
            print("[lanbus] %s %s %s %dB %s %s%s" % (
                self._ip, self._method, self._route, int(nbytes), code, result,
                (" " + self._target) if self._target else ""), flush=True)
        except OSError:
            pass

    def _q(self, name):
        vals = self._query.get(name) or []
        return vals[0] if vals else ""

    # ---- 路由实现 ----
    def _health(self):
        files = count_read_files()
        body = json.dumps({"ok": True, "node": HOST, "version": VERSION,
                           "root": os.path.basename(ROOT.rstrip("\\/")) or ROOT,
                           "files": files}, ensure_ascii=False).encode("utf-8")
        self._reply(200, body)
        self._log(200, "ok", 0)

    def _manifest(self):
        files = build_manifest()
        body = json.dumps({"files": files}, ensure_ascii=False).encode("utf-8")
        self._reply(200, body)
        self._log(200, "ok", len(body))

    def _blob_get(self):
        rel, segs = normalize_rel(self._q("path"))
        self._target = rel
        check_readable(segs)
        full = abs_under_root(segs)
        if not os.path.isfile(full):
            return self._deny(404, "文件不存在")
        digest, st = file_sha(full, rel)
        self._replied = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("X-SHA256", digest)
        self.send_header("X-Node", HOST)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            with open(full, "rb") as f:
                while True:
                    b = f.read(1024 * 1024)
                    if not b:
                        break
                    self.wfile.write(b)
                    sent += len(b)
            self.wfile.flush()
        except OSError:
            self._log(499, "client-abort", sent)
            return
        got_sha = digest
        if sent != st.st_size:
            self._log(500, "short-read", sent)
            return
        self._log(200, "sha=%s" % got_sha[:12], sent)

    def _blob_put(self):
        rel, segs = normalize_rel(self._q("path"))
        self._target = rel
        check_writable(segs)
        full = abs_under_root(segs)
        if os.path.isdir(full):
            raise BadPath("目标是目录，不是文件")

        clen_h = self.headers.get("Content-Length")
        if clen_h is None:
            return self._deny(411, "需要 Content-Length")
        try:
            clen = int(clen_h)
        except ValueError:
            return self._deny(400, "Content-Length 非法")
        if clen < 0:
            return self._deny(400, "Content-Length 非法")
        if clen > MAX_BYTES:
            self._drain(min(clen, 1 << 20))
            return self._deny(413, "超过上限 %d 字节（本次声明 %d）" % (MAX_BYTES, clen))

        claimed = (self.headers.get("X-SHA256") or "").strip().lower()
        if not claimed:
            return self._deny(400, "缺少 X-SHA256 头")
        if len(claimed) != 64 or any(c not in "0123456789abcdef" for c in claimed):
            return self._deny(400, "X-SHA256 必须是 64 位十六进制")

        d = os.path.dirname(full)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-lanbus-", dir=d)
        h = hashlib.sha256()
        got = 0
        try:
            with os.fdopen(fd, "wb") as f:
                while got < clen:
                    chunk = self.rfile.read(min(1024 * 1024, clen - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            self._quarantine_quiet(tmp, "io")
            return self._deny(400, "读请求体失败或连接中断")

        if got != clen:
            self._quarantine_quiet(tmp, "short")
            return self._deny(400, "请求体不完整（收到 %d / 声明 %d）" % (got, clen))

        digest = h.hexdigest()
        if not hmac.compare_digest(digest, claimed):
            self._quarantine_quiet(tmp, "sha")
            return self._deny(400, "sha256 不匹配（声明 %s…，实得 %s…）"
                              % (claimed[:12], digest[:12]))

        try:
            os.replace(tmp, full)
        except OSError as e:
            self._quarantine_quiet(tmp, "replace")
            return self._deny(500, "落盘失败：%s" % e.__class__.__name__)
        with SHA_LOCK:
            SHA_CACHE.pop(rel, None)
        body = json.dumps({"ok": True, "path": rel, "size": got,
                           "sha256": digest}, ensure_ascii=False).encode("utf-8")
        self._reply(200, body)
        self._log(200, "stored", got)

    def _drain(self, limit):
        """413 前尽量把已发出的请求体读掉，避免对端只看到连接重置。"""
        try:
            left = limit
            while left > 0:
                b = self.rfile.read(min(65536, left))
                if not b:
                    break
                left -= len(b)
        except OSError:
            pass

    def _quarantine_quiet(self, tmp, tag):
        name = quarantine(tmp, tag)
        if name:
            print("[lanbus] 拒收残片 -> %s" % name, flush=True)


def cmd_serve(a):
    global TOKEN
    root = os.path.abspath(a.root or default_root())
    if not os.path.isdir(root):
        print("错误：库根不存在：%s" % root, file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(root, "index")):
        print("错误：%s 不像记忆库根（缺 index/），拒绝启动" % root, file=sys.stderr)
        return 2
    configure_root(root)

    tok, src = load_token()
    if not tok:
        print("错误：没有共享令牌，服务端拒绝启动。", file=sys.stderr)
        print("  设置环境变量 %s，或先运行：python persona_lanbus.py token" % TOKEN_ENV,
              file=sys.stderr)
        print("  令牌文件位置：%s" % token_path(), file=sys.stderr)
        return 2
    TOKEN = tok

    print("SysPersona 节点服务 v%d" % VERSION)
    print("库根      : %s" % root)
    print("令牌来源  : %s（内容不打印）" % src)
    bind = (a.bind or "127.0.0.1").strip()
    print("绑定地址  : %s:%d%s" % (bind, a.port,
                                   "（默认安全档，仅本机）" if bind == "127.0.0.1" else ""))
    print("本机 IPv4 :")
    for ip in local_ipv4():
        tag = "loopback，仅本机" if ip.startswith("127.") else "网卡（物理/虚拟未区分，请自行确认网段）"
        print("  %-16s %s" % (ip, tag))
    print("提示：请只在虚拟组网（ZeroTier / Tailscale）网段暴露本服务；")
    if bind in ("0.0.0.0", "::"):
        print("警告：当前监听所有网卡（%s）——任何能路由到本机的人都可发起连接，" % bind)
        print("      务必确认令牌强度，并在防火墙里把来源限制到虚拟网段。")
    print("日志      : %s" % LOG)
    print()

    try:
        httpd = ThreadingHTTPServer((bind, a.port), Handler)
    except OSError as e:
        print("错误：监听 %s:%d 失败：%s" % (bind, a.port, e), file=sys.stderr)
        return 3
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            httpd.server_close()
        except OSError:
            pass
    return 0


def cmd_token(a):
    p = token_path()
    if os.path.exists(p):
        print("令牌文件已存在：%s" % p)
        print("（内容不打印，避免进日志/截图；需要轮换请先手动移走旧文件。）")
        return 0
    tok = secrets.token_urlsafe(32)
    atomic_write_text(p, tok + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    print("已生成令牌文件：%s" % p)
    print("令牌（只在本机显示这一次；对端请设环境变量 %s）：" % TOKEN_ENV)
    print(tok)
    return 0


# ── 客户端 ──────────────────────────────────────────────────────────
class BusError(Exception):
    pass


def _parts(server):
    u = urlsplit(server)
    if u.scheme != "http":
        raise BusError("只支持 http://（虚拟网段内明文 HTTP；本协议不做 TLS）")
    if not u.hostname:
        raise BusError("--server 缺少主机名：%s" % server)
    return u.hostname, (u.port or DEFAULT_PORT), (u.path or "").rstrip("/")


def warn_public(server):
    host = urlsplit(server).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_loopback or ip.is_private:
        return
    print("警告：%s 看起来是公网地址；令牌会以明文 HTTP 发送，"
          "本协议只该跑在虚拟网段。" % host, file=sys.stderr)


def _request(server, method, route, token=None, body=None, timeout=30):
    host, port, base = _parts(server)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {"Accept": "application/json", "User-Agent": "persona-lanbus/1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Length"] = str(len(body))
    try:
        conn.request(method, base + route, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        try:
            conn.close()
        except OSError:
            pass


def download_file(server, token, rel, expect_sha, expect_size, dest, staging):
    """下载到暂存 → 校验 sha/大小 → os.replace 到正式名。失败不删残片。"""
    host, port, base = _parts(server)
    conn = http.client.HTTPConnection(host, port, timeout=180)
    route = "/blob?path=" + quote(rel, safe="/")
    tmp = os.path.join(staging, "pull-%d-%s.part" % (os.getpid(), os.urandom(4).hex()))
    try:
        try:
            conn.request("GET", base + route,
                         headers={"Authorization": "Bearer " + token,
                                  "Accept": "application/octet-stream",
                                  "User-Agent": "persona-lanbus/1"})
            resp = conn.getresponse()
            if resp.status != 200:
                body = resp.read()[:300].decode("utf-8", "replace")
                raise BusError("HTTP %d %s" % (resp.status, body))
            declared = (resp.getheader("X-SHA256") or "").lower()
            h = hashlib.sha256()
            n = 0
            with open(tmp, "wb") as f:
                while True:
                    b = resp.read(1024 * 1024)
                    if not b:
                        break
                    n += len(b)
                    h.update(b)
                    f.write(b)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:                  # 传输中断：残片改名留证，不删
            quarantine(tmp, "io")
            raise BusError("传输中断：%s" % e)
    finally:
        try:
            conn.close()
        except OSError:
            pass

    digest = h.hexdigest()
    if n != expect_size:
        quarantine(tmp, "size")
        raise BusError("大小不符（%d / 清单 %d）" % (n, expect_size))
    if digest != expect_sha:
        quarantine(tmp, "sha")
        raise BusError("sha256 不符（%s… / 清单 %s…）" % (digest[:12], expect_sha[:12]))
    if declared and declared != expect_sha:
        quarantine(tmp, "hdr")
        raise BusError("服务端 X-SHA256 与清单不符")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(tmp, dest)
    return n, digest


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def parse_only(raw):
    if not raw:
        return []
    out = []
    for s in raw.replace("\\", "/").split(","):
        s = s.strip()
        if not s:
            continue
        if not s.endswith("/"):
            s += "/"
        out.append(s)
    return out


def cmd_sync(a):
    root = os.path.abspath(a.root or default_root())
    if not os.path.isdir(root):
        print("错误：本地库根不存在：%s" % root, file=sys.stderr)
        return 2
    configure_root(root)

    token, src = load_token()
    if not token:
        print("错误：没有令牌，无法同步。", file=sys.stderr)
        print("  设置环境变量 %s，或在本机运行：python persona_lanbus.py token" % TOKEN_ENV,
              file=sys.stderr)
        return 2

    server = (a.server or "").rstrip("/")
    if not server:
        print("错误：需要 --server http://<节点>:<端口>", file=sys.stderr)
        return 2
    warn_public(server)
    only = parse_only(a.only)

    try:
        status, headers, data = _request(server, "GET", "/manifest", token)
    except (OSError, BusError) as e:
        print("错误：连不上 %s：%s" % (server, e), file=sys.stderr)
        return 4
    node = headers.get("X-Node", "?")
    if status == 401:
        print("错误：401 未授权——令牌与服务端不一致。", file=sys.stderr)
        return 3
    if status != 200:
        print("错误：/manifest 返回 %d：%s" % (status, data[:200].decode("utf-8", "replace")),
              file=sys.stderr)
        return 4
    try:
        man = json.loads(data.decode("utf-8"))
        files = man.get("files") or []
    except (ValueError, AttributeError) as e:
        print("错误：manifest 不是合法 JSON：%s" % e, file=sys.stderr)
        return 4

    print("服务端    : %s（节点 %s）" % (server, node))
    print("本地库    : %s" % root)
    print("清单      : %d 个文件（白名单 index/ archive/ 续接/）" % len(files))
    if only:
        print("范围      : %s" % ", ".join(only))

    planned = []
    skipped = 0
    for f in files:
        rel = f.get("path") or ""
        if only and not any(rel.startswith(p) for p in only):
            continue
        dest = os.path.join(ROOT, *rel.split("/"))
        try:
            st = os.stat(dest)
            local = sha256_file(dest) if st.st_size == f.get("size") else ""
        except OSError:
            local = ""
        if local and local == f.get("sha256"):
            skipped += 1
            continue
        reason = "本地缺失" if not local else "sha 不同"
        planned.append((rel, f, reason))

    print("比对      : 需要拉取 %d 个，已一致 %d 个" % (len(planned), skipped))
    for rel, f, reason in planned:
        print("  ↓ %s  %s  （%s）" % (rel, _human(f.get("size") or 0), reason))

    if a.dry_run:
        print("[dry-run] 只打印计划：不下载、不写 ops\\lanbus.json")
        return 0

    staging = os.path.join(ROOT, "ops", "lanbus")
    os.makedirs(staging, exist_ok=True)

    pulled, failed = [], []
    for rel, f, _reason in planned:
        dest = os.path.join(ROOT, *rel.split("/"))
        try:
            n, digest = download_file(server, token, rel, f.get("sha256") or "",
                                      int(f.get("size") or 0), dest, staging)
        except (OSError, BusError) as e:
            failed.append(rel)
            print("  ✗ %s  %s" % (rel, e))
            append_log({"at": now(), "ev": "client-pull", "path": rel,
                        "bytes": 0, "result": "fail: %s" % e, "server": server})
            continue
        pulled.append(rel)
        print("  ✓ %s  %s" % (rel, _human(n)))
        append_log({"at": now(), "ev": "client-pull", "path": rel,
                    "bytes": n, "result": "ok", "server": server,
                    "sha256": digest})

    state = {
        "last_sync": now(),
        "server": server,
        "node": node,
        "root": root,
        "pulled": len(pulled),
        "pulled_paths": pulled,
        "failed": failed,
        "skipped": skipped,
        "only": only or ["(全部)"],
    }
    atomic_write_text(STATE, json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    append_log({"at": now(), "ev": "client-sync", "server": server,
                "pulled": len(pulled), "failed": len(failed),
                "skipped": skipped, "bytes": 0, "result": "ok" if not failed else "partial"})

    print("结果      : 拉取 %d 个，一致 %d 个，失败 %d 个" % (len(pulled), skipped, len(failed)))
    print("状态      : %s" % STATE)
    if failed:
        print("失败路径  : %s" % ", ".join(failed), file=sys.stderr)
        return 5
    return 0


# ── CLI ─────────────────────────────────────────────────────────────
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="SysPersona 局域网节点服务（服务端 serve / 客户端 sync）")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="当服务端：把记忆库白名单目录暴露给虚拟网段内其他节点")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--bind", default="127.0.0.1",
                    help="监听地址；默认 127.0.0.1（仅本机），对外必须显式指定虚拟网卡地址")
    sp.add_argument("--root", default="", help="库根，默认自动探测")
    sp.set_defaults(func=cmd_serve)

    yp = sub.add_parser("sync", help="当客户端：按 sha256 做增量拉取")
    yp.add_argument("--server", required=True, help="服务端地址，如 http://10.147.20.5:8737")
    yp.add_argument("--dry-run", action="store_true", help="只打印计划，不下载也不写状态")
    yp.add_argument("--only", default="", help="只同步这些前缀（逗号分隔），如 index/,续接/")
    yp.add_argument("--root", default="", help="本地库根，默认自动探测")
    yp.set_defaults(func=cmd_sync)

    tp = sub.add_parser("token", help="生成共享令牌文件（只在本机打印一次）")
    tp.set_defaults(func=cmd_token)

    a = ap.parse_args(argv)
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 0
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
