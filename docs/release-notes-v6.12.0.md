# v6.12.0 · 工具箱：把"有工具没人点"变成点得到

> **这一版只回答一个问题：十几个只读的手动工具，为什么放在命令行里就等于不存在？**

---

## 一、设计思想

今天做了引导包、备份轮转、清道夫、节点探活——全是命令行。盘点时发现 107 个脚本里有 **18 个
谁都不调用、但自己都有 CLI**，也就是说它们一点都不死，只是**没人会专门开终端去跑它们**。

所以这一版不加能力，只加**入口**，并且只加**只读**的入口：

- **点了就跑**：每项一条只读命令，输出摊在一个框里，不用记参数；
- **只放只读**：托盘里的点击是"一次误触就执行"，凡是有副作用的（加密/解封、改权限档、
  清道夫真删、备份真打）**一律不放在托盘**——那些必须先在命令行里看清参数；
- **不抢焦点**：子进程 `CREATE_NO_WINDOW`，有超时（托盘线程不能被卡住的脚本拖死），
  输出超 6000 字只留尾部并说明省略了多少。

## 二、步骤

```
托盘 → 本机 → 工具箱（只读）
   ├─ 工具链体检        persona_tools.py check
   ├─ 备份轮转计划      persona_backup_retention.py plan
   ├─ 清道夫状态        persona_janitor.py status
   ├─ 引导包清单        persona_bootstrap.py list
   ├─ 人在不在          persona_presence.py
   ├─ 权限档位（读当前） persona_permission.py --current
   ├─ 漂移记录（最近 10） persona_drift.py show --n 10
   └─ 守护日志（最后 40） persona_logs.py --tail 40
```

## 三、要实现什么

| 目标 | 机制 | 算完成 |
|---|---|---|
| 工具点得到 | 「本机 → 工具箱（只读）」二级菜单 | 菜单快照里八项齐全 |
| 每项真能跑 | 逐条实测 exit=0 | 八条命令全部 exit 0 且有输出 |
| 绝无副作用 | 只选只读子命令 | `drift` 用 `show`（`scan` 会写基线），`permission` 用 `--current` |
| 卡住不拖死托盘 | `subprocess.run(timeout=90)`，超时弹"超时"而不是卡死 | 超时分支有明确文案 |

## 四、细节

**为什么写一个通用 `_tool()`**：八个入口如果各写一遍，参数、超时、截断、错误处理会各长各样；
收成一个方法后，"只读工具"这条边界只需在一处守住。

**测试里抓到的两个真问题**：

1. `persona_drift.py` 默认是 `scan`，**会写 `ops/drift.jsonl`**——放进"只读工具箱"就自相矛盾，
   改成 `show --n 10`；
2. `persona_presence.py` 没有子命令，直接跑就出结果——我最初的测试脚本把它当"带子命令"来调，
   于是报 `exit=2`；**是测试脚本错了，不是工具错了**，改正后 exit 0。

## 五、证据

```
$ 菜单快照（ops/tray-menu.txt）
  - 工具箱（只读，点了就跑）
    - 工具链体检
    - 备份轮转计划（只读）
    - 清道夫状态（只读）
    - 引导包清单（只读）
    - - - - -
    - 人在不在
    - 权限档位（读当前）
    - 漂移记录（看最近 10 条）
    - - - - -
    - 守护日志（最后 40 行）

$ 逐条实测
persona_tools.py check            exit=0  工具      来源       路径 / 版本
persona_backup_retention.py plan  exit=0  现有快照 8 份 / 1398.4 MB
persona_janitor.py status         exit=0  候选：0 个 / 0.0 MB
persona_bootstrap.py list         exit=0  库根：…
persona_presence.py               exit=0  [presence] 空转 260 秒 → 人不在
persona_permission.py --current   exit=0  当前权限：完全权限
persona_drift.py show --n 10      exit=0  （无记录）
persona_logs.py --tail 40         exit=0  后台日志

$ 托盘重启后：pid 6896，菜单快照已刷新
```

## 六、边界

- **有副作用的工具不进托盘**：引导包打包、备份真打、清道夫 `sweep/purge`、加密/解封、改权限档，
  都只能命令行跑；这是故意的；
- 只做"展示"，不做交互（不提供输入框让填参数）；
- 不跨平台：菜单与子进程标志都是 Windows 语义；
- 没有把 18 个工具全放进去，只放了**八个纯只读**的；其余要么要参数、要么会写盘。
