# 客户端接入 — CLIENT_ADAPTER

> **SysPersona-OS** · 本文件定义：客户端接入契约：一个客户端 = 一段配置 + 一份能力声明
> 规范全文以仓库 `main` 为准；改了要同步 README / CHANGELOG / 台账。

> 状态：**v6.1 实现**（2026-11-11）。参考实现 `tools/tray/persona_tray_clients.py`、
> `tools/tray/persona_hermes_kernel.py`（运行版在记忆库根，依赖同目录的 `persona_*` 模块）。
> 这份文档只讲一件事：**本项目怎么和一个具体的第三方客户端配合，以及怎么再添加一个**。
> 示例只给 Hermes（CN 桌面版）。

## 一、定位：客户端是外部的，我们不进它的内部

- 客户端（DSH / Hermes / 以后的别的）是**别人的程序**。我们不修改它、不重打包、不接管它的界面逻辑。
- 我们只做三件它做不到的事：**认得它、看着它、在受限范围内操作它**。
- 一条硬规矩：**能只读就不操作；能命令行就不点界面**。点界面只作为最后手段（DSH 没有命令入口才用它）。

```text
记忆库（人格 + 分层记忆 + 台账）
    │  只读：进程 / 水位 / 版本 / 会话文件
    └── 受限操作：发一句话、重启内核、重启窗口、拉起网关
```

## 二、契约：一个客户端 = 一段配置

真源 `ops/tray-clients.json`（运行版在记忆库）。**五个字段**，缺一个就少一项能力：

| 字段 | 回答什么 | 例（Hermes） |
|---|---|---|
| `img` | 靠哪个 exe 名认出"它在跑" | `hermes-agent-cn-desktop.exe` / `…-runtime-win32-x64.exe` |
| `wm` | 水位从哪读 | `hermes`（读守护写的 `ops/hctx.json`；没有就只报"在跑"） |
| `actions` | 能对它做什么、用哪种方法 | `compact: cdp`、`kernel: restart-kernel`、`gateway: keeper` |
| `open` | 它的会话/日志在哪 | `hermes-home\sessions` |
| **`detect`** | **它装在哪、配置在哪、工作目录在哪**（2026-11-11 新增） | exe / `config.yaml` / `hermes-home` |

`detect` 是为了「程序自动拉起、重载」而加的——**认不出路径，就谈不上拉起和重载**。
形状（`persona_client_probe.py` 读它）：

```json
"detect": {
  "exe":     ["%LOCALAPPDATA%\\Programs\\DeepSeek Harness\\DeepSeek Harness.exe"],
  "home":    ["%USERPROFILE%\\.dsh"],
  "config":  ["%USERPROFILE%\\.dsh\\.credentials.yaml"],
  "workdir": ["%USERPROFILE%\\.dsh"],
  "auto_launch": false
}
```

- 路径支持 `%ENV%` 与 `~`，多个候选**取第一个存在的**；`config` 支持通配（取最新一个）；
- `auto_launch` **默认 `false`**：先做到"认得出来、报得出来"；自动开窗口会打断人正在做的事，
  与「不抢焦点」是同一条纪律，要开得人显式改这一项；
- 版本从 exe 的资源段读（`GetFileVersionInfo`），不靠 exe 名猜；
- 验证：`python persona_client_probe.py check` 一屏给出 exe/配置/工作目录/是否在跑/版本；
  托盘里每个客户端二级菜单有「打开配置文件」和「看它认路的结果」。

> 踩过的坑（2026-11-11）：托盘读配置时**按白名单合并**，把 `detect`/`open` 这类新字段直接砍掉、
> 还把文件重写回去 —— 已改成**全量合并**。加新字段时别再写白名单。

```json
"Hermes": {
  "on": true, "label": "Hermes",
  "img": ["hermes-agent-cn-desktop.exe", "hermes-agent-cn-runtime-win32-x64.exe"],
  "wm": "hermes",
  "open": {"sessions": ""},
  "actions": {"status": true, "compact": "cdp", "reload": "cdp",
              "kernel": "restart-kernel",
              "restart": "watchdog:enact:restart-hermes",
              "gateway": "keeper"}
}
```

**托盘菜单不写死**：有哪个 `actions` 就出哪几项；客户端没在跑，它那一级整项消失（"运行才有选项"）。

## 三、自动操作客户端：六种方法，各有前置条件

| 方法 | 怎么干 | 前置条件 | 失败长什么样 |
|---|---|---|---|
| `inject` | 把文字放进输入框再回车（界面注入） | 找得到唯一主窗口；抢得到前台焦点；壳空闲 | `没抢到前台焦点` / 回车被吞（文字残留在输入框） |
| `cdp` | 连 Electron 调试口，在渲染进程里操作 DOM | 启动时带 `--remote-debugging-port` | `NO_TEXTAREA`（页面没长出来）/ `STILL_n`（没发出去） |
| `cli` | 直接调客户端自己的命令行（一次性提示） | 有可用 CLI、能用它的凭据 | 认证失败 / 需要额外 --resume |
| `restart-kernel` | 只重启它的**内核子进程**，不动窗口 | 认得内核进程命令行；能等到健康检查 | 健康检查 90 秒不通 |
| `keeper` | 拉起它的**独立网关进程**（微信通道） | 有它的启动脚本；能读到 home | 起了但 N 秒内没连上 |
| `watchdog:a:b` | 跑我们自己的进程动作（杀/拉） | 进程名对得上 | 关不掉时放弃（避免开两份） |

### 三条操作纪律（都是踩过的坑换来的）

1. **别在它生成中途重启**。内核被杀，那一轮永远停在"已发送、没回复"，界面上就是一条永远卡住的消息。
   现在重启前先查 `state.db` 的轮次租约，有活就先等（最多 120 秒），等不到就不重启。
2. **别同时开两份**。杀不干净就放弃重启；拉起前先看进程在不在。
3. **别点它的按钮**。能用命令/re 接口就别点；点了就没法验证到底生效没有。

## 四、示例：Hermes（CN 桌面版）串起来看

```text
桌面壳 hermes-agent-cn-desktop.exe
  └─ 内核    …\data\versions\0.21.0-cn.18\hermes-agent-cn-runtime-win32-x64.exe dashboard --host 127.0.0.1 --port 9120 --no-open
  └─ 网关    …\hermes-agent-cn-runtime-win32-x64.exe gateway run      （微信那条线，独立进程）
```

| 事 | 走哪条路 | 怎么验证 |
|---|---|---|
| 看在不在 | 进程名（`img`） | 托盘状态 |
| 水位 | `ops/hctx.json`（守护写） | 托盘 tooltip |
| 版本 | exe 的 FileVersion + `data\versions\*\manifest.json` 的 `runtimeVersion` | 托盘「客户端版本」 |
| 压缩上下文 | CDP 调试口发 `/compact` | 页面返回 `SENT` |
| 重读设定 | 同上，发一句"读 MEMORY" | 同上 |
| 改配置后生效 | **重启内核**（`restart-kernel`），不关窗口 | `GET :9120/api/health` → `{"ok":true}` |
| 微信通道 | `gateway status` 判活；`gateway-service\Hermes_Gateway.vbs` 隐藏拉起 | keeper 每分钟一轮 |
| 会话在哪 | `…\data\hermes-home\sessions` | 托盘「打开这个客户的会话存档」 |

Hermes 比 DSH 好办的地方：**它有 CLI、有调试口、有本地 HTTP**，所以压缩/重读/重启都能走命令行或接口，
不用碰界面。DSH 没有命令入口，只能注入——这是"客户端方法不通用"的具体含义。

## 五、加一个新客户端：五步

1. **把它开起来**，任务管理器看它的进程名（这就是 `img` 的候选）。
2. **定水位**：它有本地状态文件/接口能读吗？能就写个适配读进 `wm`；不能就留空——**留空只报"在跑"，不算异常**。
3. **定动作**：它能用什么方法？（有调试口 → `cdp`；有 CLI → `cli`；都没有 → `inject`）
   拿不准就先只写 `status: true`，别一上来就给它配重启。
4. **写进 `ops/tray-clients.json`** 的 `clients` 里一段，`on: true`，重启托盘。
5. **看日志**：`ops/tray-multi.log` 会写"建托盘（在跑：…）"和"菜单重建"。
   菜单里它自己那一级出现了，就接通了。

不需要改代码、不需要动它的安装目录。

## 六、不做的事

- 不写它的配置去"改它的行为"（除了我们自己的可替换配置文件）；
- 不注入它的界面按钮、不替它点确认；
- 不在它生成时抢焦点（人正在打字时要排队，不能冲掉他的输入）；
- 不假设"谁是主客户端"：谁是主只影响人先看哪一行，不影响能力。

## 七、失败模式对照

| 现象 | 通常是什么 | 先查 |
|---|---|---|
| 菜单里没有这个客户端 | 进程名不匹配 / `on: false` | `ops/tray-multi.log`、任务管理器里的 exe 名 |
| 有菜单项但点了没反应 | 方法不适用于这个版本 | 该客户端的日志；方法前置条件（调试口/CLI） |
| 界面里一条消息永远卡住 | 生成中途被重启 | 重启窗口（内核重启清不掉它） |
| 版本变了但功能坏了 | 客户端升级改了格式/接口 | 托盘「客户端版本」看差异；按第五节重定动作 |
