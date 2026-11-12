# v6.10.0 · 引导包真跑一遍：开场那一步，加上带运行时

> **这一版只回答一个问题：把引导包解到一台空机器上，到底还差什么才能开工？**

---

## 一、设计思想

v6.5.0 定义了引导包，但**从没拿真包在一台"空机器"上跑过**。这一版就是去跑它，遇到什么补什么。

跑出来的两件事，都不是设计问题，是**边界没走到底**：

1. **带运行时那条路是死的**：`pack --with-runtime` 永远失败——密钥扫描按"名字里有 token 就拒"，
   把 git 的 `git-credential-manager.exe`、`ssh-agent.exe` 和 Python 的 `tokenize.py` 全判成泄密源。
2. **解出来还不能开工**：包里只有 `scripts/ config/ docs/`，`ops/ sessions/ archive/ index/ 备份/ 续接/` 都不存在。

所以这一版只做两件事：**把扫描改成分层判**、**给一条明确的开场命令 `init`**。

## 二、步骤

```
打包（两台机器之间唯一要复制的东西）
  │  pack [--with-runtime]
  ├─ 扫描：先判"是不是文本"，再判"名字/内容像不像密钥"；二进制不按文本扫
  ▼
解包到新机器
  ├─ verify：按 MANIFEST 校 sha256（不过就不许装）
  ├─ init --apply：建目录骨架（ops/sessions/archive/index/备份/续接/steps/memory/清理）
  └─ 照 init 打印的五步走：selftest → 认身份 → 探活 → 试拉 → 自检
```

## 三、要实现什么

| 目标 | 机制 | 算完成 |
|---|---|---|
| 带运行时的包能打出来 | 扫描分层：文本（扩展名白名单 + 无 NUL）才按名字/内容判密钥 | `pack --with-runtime` 返回 0 且 `verify` 全绿 |
| 二进制不被误杀 | 二进制既不按名字判、也不读内容 | git/python 自带工具全部进包 |
| 解出来就能开工 | `init --root <目录> --apply` 建九个骨架目录，并打印后续五步 | 解包后 `init` 一次，九个目录齐 |
| 开场顺序不靠记性 | `init` 直接打印 ①~⑤（含探活与 `--dry-run` 试拉） | 顺序与 `DEPLOY_MIN_SPEC` §四 一致 |

## 四、细节

**分层判的必要性**：早先的扫描把"名字含 token"当唯一判据，于是 `token.py`（Python 标准库的语法分词器）
和 `git-credential-manager.exe` 都被拒。**一个能跑的引导包永远打不出来**——这比漏扫更糟，
因为它让整条路看起来"还没做"，而不是"做错了"。

现在的判据是两条腿：**扩展名必须是装密钥的容器**（`.txt/.json/.env/.key/.pem/…`）+ **名字里要有密钥词**；
内容扫描也只对文本做（`sk-` 长串、私钥头），并且只读前 256 KB。

**`init` 只建目录，不搬数据**：它不复制会话、不建索引、不写配置——那三件事分别是连接、重建、认身份的职责。
dry 是默认，`--apply` 才动手；已有目录只报"已有"，不覆盖。

## 五、证据

```
# ① 带运行时打包（修好之后）
$ python persona_bootstrap.py pack --out <目录> --with-runtime
文件 5684 个 / 396.8 MB（含 MANIFEST.json）
跳过：docs/交接文件规范-v2.md   ← 不在这个包里
耗时 132 秒；zip 156 MB
$ python persona_bootstrap.py verify <zip>
校验：OK 5684 · 哈希不符 0 · 缺 0

# ② 拿真包在空目录里跑（模拟新机器）
$ python persona_bootstrap.py pack --out <目录>          # 112 个文件 / 1.7 MB
$ Expand-Archive … → 目录里只有 config/ docs/ scripts/ MANIFEST.json
$ python scripts/selftest.py
== 结果：PASS 36 / FAIL 0 ==                            ← 脚本层面真的能跑

$ python scripts/persona_machine.py detect              ← 认得出库根
$ python scripts/persona_janitor.py status              ← 缺目录也不报错
$ 检查骨架：ops sessions archive index 备份 续接 steps memory 清理 → 全部 False

$ python persona_bootstrap.py init --root <目录> --apply
已就绪：新建 9 个 · 已有 0 个
  ① python scripts\selftest.py
  ② python scripts\persona_machine.py new
  ③ python scripts\persona_lanbus.py health --server http://<对端虚拟IP>:8737
  ④ python scripts\persona_lanbus.py sync --server … --dry-run
  ⑤ python scripts\persona_selfcheck.py --once
→ 复查：九个目录全部 True
```

## 六、边界

- `init` **不**替你装运行时（没 python 就先解带运行时的包，或跑 `restore-runtime.ps1`）；
- `init` **不**替你连节点、不拉数据：那要令牌与对端地址，属于人的决定；
- `pack` 仍拒绝把 `sessions/archive/index/备份/ops/续接/steps` 打进包，**这条红线没有松动**；
- 扫描仍是启发式的（按扩展名+关键词）：它不是防泄密的唯一防线，真正的防线是"包里不放密钥"的习惯。
