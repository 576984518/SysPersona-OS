# 人格运行时（tools/persona）— v5.3

> 这一版只回答一个问题：**唤醒之后到底发生了什么。**
> 答案不是「拉起来就算完」：醒来 → 读总索引 → 报到 → 认领 → 执行 → 自己定下一次。

## 一、文件

| 文件 | 干什么 | 一句话判据 |
|---|---|---|
| `persona_wake.py` | 闹钟登记与触发；到点先摘一份**总索引简讯** | 没有 purpose 的闹钟登记被拒（exit 2） |
| `persona_report.py` | 报到 / 谁在 / 派发 / `ack` / `reap` | 没报到不派（exit 2），报到但没读索引不派（exit 3） |
| `persona_dispatch.py` | 派发台账（append-only，按 id 取最后状态） | 过期不改旧行，只追加 expired + 改派；换人 3 次仍无人接 → failed + 告警 |
| `persona_master.py` | 总索引速览（人读 / `--json` / `--pending`） | 来源按时间倒序，末尾列「派发中的任务」 |
| `persona_proxy.py` | 交互代理：**AI 调本机的唯一通道** | 越界 403、未知动词 404、错 token 401、未授权命令 403 |
| `persona_keepalive.py` | 保活：盯进程、没在跑就拉起 | 查不了（`None`）时**不动手**，不重复拉起 |
| `selftest.py` | 不碰真库的自检（临时库 + 真跑） | `PASS 32 / FAIL 0` |

七个脚本放在**同一个目录**，互相 `import`；`PERSONA_ROOT` 可覆盖库根（默认脚本所在目录），
所以自检、换机器、跑测试都不用改代码。

## 二、接线（守护进程）

守护进程（`persona_daemon.py`）加两处：`wake`（本来就有）、`reap` 与 `proxy`（新加）。
**保活不另开任务**——它并进已有的 `agent` 任务，换掉原来写死壳 exe 的那份实现：

```python
DEFAULTS = {..., "wake": 30, "agent": 60, "reap": 60, "proxy": 60}

def t_agent():                      # 原来写死 hermes 的 exe，现在读配置
    return t_keepalive()

def t_keepalive():
    import persona_keepalive as K
    res = K.check()
    if not res:
        return True, "未登记保活目标"
    return bool(res[0].get("ok")), res[0]["msg"]

def t_reap():
    import persona_dispatch
    r = persona_dispatch.reap(ttl=600)
    return True, "过期 %d，改派 %d" % (len(r["expired"]), len(r["redispatched"]))

def t_proxy():
    import persona_proxy
    if persona_proxy.is_up():
        return True, "代理在跑"
    subprocess.Popen([sys.executable, os.path.join(ROOT, "persona_proxy.py"), "serve"],
                     cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True, "代理没在跑，已拉起"

TASKS = {..., "wake": t_wake, "agent": t_agent, "reap": t_reap, "proxy": t_proxy}
```

保活目标写在 `ops/keepalive.json`（`name` / `match` / `exe` / `cooldown`），换壳只改配置。
节奏都在 `ops/persona.conf.json` 里，可单独关掉一个任务而不影响其它。

## 三、一次完整的心跳

```
闹钟到点（wake）
  → 摘简讯：总索引最近 8 条 + 派发中的任务          → ops/wake-brief.json
  → 执行 purpose（可选 --run，环境变量带 PERSONA_PURPOSE / PERSONA_BRIEF）
  → 写触发记录                                       → ops/wake-fires.jsonl
  → 替 agent 报到（read_index=true）                 → ops/reports.json
派发（dispatch）
  → 门一：最近 600 秒报到过；门二：报到时读过总索引
  → 落 pending                                       → ops/dispatch.jsonl
超时（reap，默认 ttl 600 秒）
  → 追加一条 expired；有醒着的人就再追加一条改派的新 pending
  → 换人次数（retry_count）到 3 仍无人接 → 标 failed 并写 ops/alerts.jsonl 叫人
  → 原记录不覆盖：查出「谁在什么时候把什么交给了谁」
```

## 四、用法

```powershell
$PY = "D:\ProgramData\SysPersona-Runtime\python\python.exe"
$R  = "C:\hermes-mem"          # 库根，按实际部署改

& $PY $R\persona_wake.py add --in 90 "检查抓取结果" --agent hermes-v4
& $PY $R\persona_wake.py list
& $PY $R\persona_wake.py brief                    # 醒来第一眼看这段
& $PY $R\persona_master.py --k 10                 # 总索引
& $PY $R\persona_report.py report --agent hermes-v4 --read-index
& $PY $R\persona_report.py dispatch hermes-v4 "跑一次索引"
& $PY $R\persona_report.py reap --ttl 600
& $PY $R\persona_proxy.py serve --port 8787
& $PY $R\persona_proxy.py call index.top '{"k":5}'
& $PY $R\persona_keepalive.py add --name shell --match your-app --exe "D:\path\app.exe"
& $PY $R\persona_keepalive.py check
```

自检（不需要真库）：

```powershell
& $PY tools\persona\selftest.py          # 临时库，跑完自动删
& $PY tools\persona\selftest.py --keep   # 留着现场
```

## 五、为什么这样切

- **简讯而不是「注入一句话」**：注入哪个界面是壳的事，写什么才是这套东西的事。
  简讯是纯文件（`ops/wake-brief.json`），壳愿意读就读、愿意显示就显示——换壳不用改代码。
- **append-only 派发台账**：派发是一次责任转移，覆盖写就查不到中途变过什么。
- **两道门而不是一道**：只查「在不在线」会派给一个刚醒、还什么都没读的智能体；
  「读没读索引」是它能干活的前提。
- **代理默认只读**：写与执行（`sys.run`）要人在 `ops/proxy-allow.json` 里逐条加前缀，
  默认是空表——**默认全部拒绝**。

## 六、边界

- 不含界面注入、字体替换、本地 OCR 兜底——那是针对具体壳的私有适配，只提方法，不入库；
- 代理只监听 `127.0.0.1`，不做跨设备、不做公网；
- 进程查询返回三态：`true` / `false` / `null`（查不了）。**查不了不动手**，
  因为在受限令牌下「看不见」会伪装成「没在跑」，照着重启就会开出第二份；
- `sys.run` 不是「远程 shell」：白名单里没有的命令一律 403，审计逐条落盘。
