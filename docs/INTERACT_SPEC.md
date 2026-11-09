# 交互代理规范（INTERACT SPEC）

> 一句话：**AI 要碰本机，走这张动词表；表外的东西，默认不存在。**
> 状态：v5.3.0 起为默认链路；实现在 `tools/persona/persona_proxy.py`，自检在 `selftest.py` T5。

## 一、为什么要有代理

智能体直接拿 shell，等于每次都在「什么都可能被执行」的前提下工作：

| 失败模式 | 后果 |
|---|---|
| 没有白名单 | 一次提示词注入就能删目录 |
| 没有台账 | 出了事查不到「哪个分身、什么时候、干了什么」 |
| 和壳绑死 | 换一个壳，权限模型全部重做 |

代理把这三件事一次收敛：**动词表 = 白名单，审计行 = 台账，HTTP 协议 = 与壳无关**。

## 二、传输与鉴权

| 项 | 规定 |
|---|---|
| 监听 | `127.0.0.1:<port>`，默认 `8787`；**不绑 `0.0.0.0`** |
| 请求 | `POST /v1/call`，body `{"verb": "...", "args": {...}}` |
| 探活 | `GET /health` → `{"ok":true,"name":"persona-proxy","version":"5.3.0"}` |
| 鉴权 | 请求头 `X-Persona-Token`，值取 `ops/proxy-token.txt`（首次启动自动生成，权限 600） |
| 来源 | 只接受来自 `127.0.0.1` / `::1` 的连接 |
| 体积 | body ≤ 256 KB；`mem.read` 单次 ≤ 8000 字 |
| 并发 | 每请求一线程（`ThreadingHTTPServer`），慢请求不堵心跳 |

token 不进 URL（会落进日志），不进审计行。

## 三、动词表

| 动词 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `health` | — | `ok/name/version/at/root` | 探活，也要 token |
| `index.top` | `k ≤ 50` | `total/recent/pending` | 总索引最近 k 条来源 + 派发中任务 |
| `mem.grep` | `q`，`k ≤ 50` | `total/hits[path,hits,snippet]` | 只在 `sessions/archive/memory` 里搜 `.md` |
| `mem.read` | `path`（ROOT 内），`max_chars ≤ 8000` | `path/chars/truncated/text` | 越界 403 |
| `wake.list` | — | `alarms[]` | 闹钟表（含 due、enabled） |
| `wake.add` | `purpose` 必填，`in_seconds`/`at`/`every`/`agent`/`run` | `id/due` | 无 purpose → 400 |
| `report.report` | `agent` 必填，`status`/`note`/`read_index` | `agent/status/read_index` | 报到即心跳 |
| `report.who` | `within` | `agents[]` | 最近报到的分身 |
| `dispatch.add` | `agent`、`task` | `id/agent/task` | **两道门**：未报到 → 409；未读索引 → 409 |
| `dispatch.reap` | `ttl`、`max_retry ≤ 3` | `expired[]/redispatched[]/failed[]` | 超时回收 + 改派；换人上限用完 → `failed` 并写告警 |
| `sys.proc` | `name`（只允许进程名） | `running/query_ok/note` | `running` 三态：`true`/`false`/`null` |
| `sys.run` | `cmd`、`timeout ≤ 120` | `rc/out/err` | **默认拒绝**；只放行 `ops/proxy-allow.json` 里逐条写的前缀 |

`sys.run` 的白名单文件长这样（空表 = 全部拒绝，这是默认值）：

```json
{ "allow": ["cmd /c echo hello", "git status", "python tools/persona/selftest.py"] }
```

匹配规则：命令与白名单项完全相同，或以「白名单项 + 一个空格」开头。
不做正则、不做通配——**能匹配上什么，一眼能看出来**。

## 四、错误码

| HTTP | 含义 | 典型触发 |
|---|---|---|
| 200 | 成功 | — |
| 400 | 参数不对 | 缺 `q` / 缺 `path` / 缺 `purpose` |
| 401 | token 不对 | 头缺失或值不符 |
| 403 | 有 token 但这项不许做 | `mem.read` 越出 ROOT；`sys.run` 未列白名单 |
| 404 | 没有这个动词 / 路径 | 拼错动词名 |
| 409 | 前置条件不满足 | 派发被「报到门」或「索引门」拦下 |
| 413 | body 过大 | > 256 KB |
| 500 | 服务端异常 | 读文件失败等，错误信息截断 200 字 |
| 504 | 超时 | `sys.run` 超过 `timeout` |

失败一律返回 `{"ok":false,"error":"..."}`，**不回栈**、不回绝对路径。

## 五、审计

每次调用追加一行到 `ops/proxy-audit.jsonl`：

```json
{"at":"2026-11-09 17:49:02","verb":"mem.read","args":"{\"path\": \"sessions/...\"}","code":200,"ms":3,"peer":"127.0.0.1"}
```

- 只追加，不改历史；
- `args` 截断到 120 字，够认人、不够泄密；
- `code` 是 HTTP 码，失败也记；
- **不记 token**（自检里有一条专门断言这个）。

## 六、进程查询为什么是三态

`running` 有三个值，不是两个：

| 值 | 含义 | 保活动作 |
|---|---|---|
| `true` | 看见进程 | 不动作 |
| `false` | 快照完整，确实没有 | 可以拉起（受冷却约束） |
| `null` | 查不了（受限令牌、快照被过滤） | **不动手** |

理由：受限令牌下「看不见」和「没在跑」长得一模一样。把 `null` 当 `false`，
同一个壳会被开第二份——**比不保活更糟**。判据：快照里连本进程自己都看不见，就判 `null`。

## 七、边界

- **不做远程 shell**：`sys.run` 默认全拒，白名单只写「能力」，不写「整台机器」；
- **不碰壳**：代理不知道也不关心对面是哪个客户端；
- **不跨设备**：只听 `127.0.0.1`；要跨设备请自己加认证层，本规范不提供；
- **不是提权**：代理的权限不超过启动它的用户；它只是把「能做什么」变得可看、可审计。

## 八、验收（selftest T5）

| 判据 | 期望 |
|---|---|
| 动词表可用 | `health` / `index.top` / `mem.grep` / `mem.read` 均 200 |
| 越界拦住 | `mem.read {"path":"../../etc/passwd"}` → 403 |
| 未授权拦住 | `sys.run` 未加白名单 → 403；加了 → 200 且输出含预期字符串 |
| 动词不存在 | 拼错 → 404（不是断连接） |
| 鉴权有效 | 错 token → 401 |
| 有台账 | `ops/proxy-audit.jsonl` 行数 = 调用次数（含失败） |
| 不泄密 | 审计行里没有 token |

## 九、修订记录

| 日期 | 版本 | 改了什么 | 为什么 |
|---|---|---|---|
| 2026-11-09 | v1.0 | 首次成文：传输、12 个动词、错误码、审计、三态进程查询、边界 | v5.3 交付；此前「AI 怎么调本机」只有口头约定 |
