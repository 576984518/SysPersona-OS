# Usage: hand the repo to an AI agent

> **SysPersona-OS** · 本文件定义：使用说明（英文版）
> 规范全文以仓库 `main` 为准；改了要同步 README / CHANGELOG / 台账。

> This is **not a program package**. No installer, no binaries, no `setup.exe` — and no
> runnable product is shipped.
>
> There is exactly one way to use it: **give the repository to an AI agent that can read/write
> files and execute commands, and let it build the implementation for your environment.**

## Why it ships this way

The system was designed and written by AI, and it grew onto one specific machine: a Windows
notification area, scheduled tasks, a local browser, one particular chat shell, one memory
directory on one drive. On another machine the paths, accounts, shell version and available
permissions all differ.

**Shipping "a program that just runs" would be a false promise.** So the repository ships the
**design and its acceptance criteria** instead. Landing it is the reader's agent's job.

## What the agent is told to do

1. **Read first, write later** — README, the global index, the architecture note, the red lines,
   then the specs relevant to the task.
2. **Inventory the environment (read-only)** — OS and permissions, which chat shell exists and what
   it exposes, whether a local browser is available and logged in, whether scheduled tasks work.
   Produce a capability table: can / cannot / unsure.
3. **Map before building** — map each mechanism in the specs onto this machine's actual abilities.
   Anything unmappable must be reported as "not implementable here", never faked.
4. **Build in slices, each with its own evidence** — a number, a timestamp, an exit code.
   Without evidence it is "not passing", not "done".
5. **Close out** — a local operations note (paths, task names, how to stop, how to uninstall),
   plus an explicit list of what still cannot be done and why.

## Hard constraints (violating these fails the task)

- Never delete or clean anyone's memory or files; deletion requires human confirmation;
- Never modify or bypass any third-party program or authentication;
- Identity, kernel and handoff-format changes: produce a proposal only, wait for the human;
- Every "implemented" claim must carry evidence — no conclusions without proof.

## Dependencies (check before landing)

| Layer | Needs | Without it |
|---|---|---|
| Core: index / retrieval / self-test | **core paths are stdlib-first; the vector path optionally needs `numpy` (auto-skipped if missing), Chinese tokenization optionally uses `jieba` (falls back to char bigrams if missing)** (Python 3.10+) | nothing |
| Local semantic retrieval (optional) | `bge-m3` + vector deps | falls back to keyword search |
| Tray | `pystray` + `Pillow` | no tray icon; background jobs still run |
| Platform | Windows (`msvcrt` / `winreg`) | will not run elsewhere - by design, not a bug |

## Paths and environment variables

**No machine-specific path is hard-coded.** Path resolution has a single entry point,
`tools/ctxpack/persona_root.py` (`root()` / `repo()` / `runtime_py()` / `node()` /
`hermes_home()` / `localappdata()` / `programdata()`), and a fixed precedence:
**explicit argument > environment variable > optional machine config > derivation from the
script itself > built-in default**.

| Variable | Purpose | Default | Required? | If unset |
|---|---|---|---|---|
| `SYSPERSONA_ROOT` | Memory root (`archive\`, `index\`, `ops\` live here) | The script's own directory (when it contains `archive\` or `index\`) | Not needed for a flat deployment (scripts already in the vault); required when running from the repo | Raises `PathUnset` with "set SYSPERSONA_ROOT" plus the current script directory — it **never** falls back to the author's paths |
| `SYSPERSONA_REPO` | Repository root (contains `tools\ctxpack\` and `docs\`) | Found by walking up from `persona_root.py` | No | Only the secret scan and the install script lose one capability; they say "not configured / skipped" |
| `PERSONA_PY` | Python interpreter (`python.exe` / `pythonw.exe`) | `<runtime>\python\python.exe` → the running interpreter | Required by `install-service.ps1`; optional elsewhere | Other scripts use the current interpreter; the installer exits with `[FAIL]` (exit 2) instead of guessing |
| `SYSPERSONA_RUNTIME` | Runtime root (holds `python\` and `node\bin\`) | Derived from `sys.executable`; else `%ProgramData%\SysPersona-Runtime` (only if it exists) | No | Nothing is guessed: node then comes from `PERSONA_NODE` or `PATH` only |
| `PERSONA_NODE` | node executable | `<runtime>\node\bin\node.exe` → `node` on `PATH` → none | No | Capture / export / injection tasks report "PERSONA_NODE is not configured" and skip; indexing and search are unaffected |
| `PERSONA_HERMES_HOME` | Hermes home (`state.db`, `desktop-ui.sqlite`) | `%ProgramData%\Hermes Agent CN Desktop\data\hermes-home` (only if it exists) | No | Hermes-side tasks (export / water level / handoff log) report "not configured" and skip; indexing, search and self-test keep working |
| `PERSONA_CAPTURE` | node script used for web capture (a `collect7.js`-style file) | none | No | The `capture` task is skipped |
| `PERSONA_HERDSMAN` | `herdsman.exe` | `%ProgramData%\Herdsman\herdsman.exe` (only if it exists) | No | Nothing restarts the service when port 8080 dies |
| `PERSONA_BACKUP2` | Second backup root (must be on **another disk**) | none | No | The `backup2` task is skipped — a second copy is never written inside the vault |
| `LOCALAPPDATA` | Base for `cdp-profile\`, heartbeats, tray logs, optional machine config | Provided by the OS | OS variable | Scripts that need those directories skip; `localappdata()` returns `None` and **does not guess** |
| `ProgramData` | Base for the `%ProgramData%` defaults above | Provided by the OS | OS variable | Same as above; `programdata()` returns `None` and **does not guess** |
| `PERSONA_ROOT` | Legacy alias of `SYSPERSONA_ROOT` | none | No | Kept for compatibility; `SYSPERSONA_ROOT` wins when both are set |

A few per-feature variables exist as well and can be ignored unless you use that feature:
`NODE_BIN` (legacy alias of `PERSONA_NODE` in `persona_sf_models.py`), `PERSONA_CDP_PORT`,
`PERSONA_CDP_LAUNCHER`, `PERSONA_POWERSHELL`, `PERSONA_HERMES_DB`, `PERSONA_HERMES_UI_DB`.

**Optional machine config**: `%LOCALAPPDATA%\SysPersona\paths.json` with keys
`root` / `repo` / `runtime` / `python` / `node` / `hermes_home` / `capture` / `herdsman` / `backup2`.
The repository **never creates or commits** that file; it merely freezes variables into one file and
ranks below environment variables.

Two notes:

1. `%ProgramData%` is whatever the OS says (usually on the system drive) and need not be where you
   actually keep the runtime. When the two differ, set `SYSPERSONA_RUNTIME` / `PERSONA_HERMES_HOME`
   instead of hoping for a derivation.
2. Running `tools\ctxpack\*.py` straight from the repo requires `SYSPERSONA_ROOT`: there is no
   `archive\` next to the scripts, so nothing can be derived and the command fails loudly. That is
   deliberate — the previous version silently pointed at the author's vault.

## How to verify (do not trust claims, watch it run)

1. Capture: trigger once, show the new archive file and its timestamp;
2. Retrieval: search a phrase only you two know, get back `file:line`;
3. Durability: mirror the source before any compaction, show a matching checksum;
4. Processing: with nobody touching it, the context level actually drops;
5. Reset: after processing, the rules are put back in front once; interrupt one on purpose and see it resubmit;
6. Self-check: six checks, each with a verdict (pass / fail / why);
7. Boundary: ask it to delete a memory — it must **stop and ask you**.

## Platform

Verified on **Windows 10/11 only** (notification area, scheduled tasks, local files, local retrieval).
No Linux/macOS implementation exists. No cloud service is required; offline, everything except
"fetching new content from the web" keeps working.

### Local encryption and auth (v6.2.0, Chinese-only spec for now)

From v6.2.0 the local copy can be stored as ciphertext, and "who may read it" is a credential:

- one file = one sealed blob (`SPV1`): SHAKE256 keystream + HMAC-SHA256 (encrypt-then-MAC),
  **AAD = path relative to the repository root**, so renaming or moving a file makes it undecryptable;
- the 32-byte key is wrapped by **Windows DPAPI** (same machine + same user), with an optional
  scrypt passphrase copy for reading on another machine;
- clients never receive a path: a local **I/O gate** (`127.0.0.1:8738`) hands out a 120-second
  **handle URL**, re-authenticated on every read (`/open /read /write /close /list /ingest /health`);
- credentials carry scopes: `device` (machine fingerprint, auto-authenticated) / `password` (scrypt) /
  `token` (`spr_…`, GitHub-style) / `totp` (phone token); 8 failures lock for 300 seconds;
- **sync is out of scope**: local → NAS → cloud → mirrored disk (with version history) is an external chain.

The full specification is `docs/VAULT_SPEC.md` (written in Chinese; an English translation is not yet available).
The switch `ops\vault.json → enabled` defaults to `false`, because readers must be migrated to
`persona_vault_io` first — see §10 of that spec for the switch-over order.

## Common misconceptions

| Misconception | Reality |
|---|---|
| "Download it and run it" | Nothing runnable is shipped; it must be generated in your environment |
| "Copy the folder to another PC" | You copy the design and the memories; the implementation must be rebuilt |
| "Change the model and it changes" | Persona and memory live in files; the model is only the engine |
| "Close the window and it dies" | Only the UI closes; it reads its memory and is still itself |
| "Let the AI decide what old memory to delete" | Never. Deletion always requires the human |
## 8. Real-world deployment tests

> This section records **actual runs only**: who ran it, in what environment, how far it got, where it
> stopped, and the verdict. It never changes the design or the acceptance criteria above.

| Date | Model tested | Environment | Reached step | Result | Evidence / blocker |
|---|---|---|---|---|---|
| 2026-11-10 | Qwen (qianwen.com) | Linux sandbox (its own runtime, repo cloned from GitHub) | step 3: `selftest.py` 34 PASS / 2 FAIL (both FAILs hard-code `cmd /c`, i.e. platform mismatch) | partial pass | found 3 real defects: FTS `contentless_delete` needs SQLite >= 3.43, `persona_gauge` read `~/.dsh` unguarded, `persona_pack._drop_archived` silently deleted old archives — **all three fixed** (same-day CHANGELOG entry); several doc drifts fixed too |
| 2026-11-10 | **This machine (Windows 11 + DSH shell, author environment)** | real memory root | all seven steps of section 5 | pass | (1) capture 16:30 of `457f8bdb` → `check: DOM 4203 / captured 4197 / consistent (0.1%)`; (2) search `蜉蝣之尊` → `archive/2026-11-10/deepseek网页/capture-075303-md-d4de68da.md:27505`; (3) `persona_mirror.py --check` → 7 mirrors, sha256 all match; (4) watermark 13:52 fired @460K → 13:55 118K → 13:56 15K with nobody touching it; (5) reset: 16:10/16:11 `queued` (user active, never steals focus) → 16:17:36 `fired` flush=true, 985 chars → 16:18:46 refused again by the same rule; (6) `persona_selfcheck.py --once` 7/7 OK, `selftest.py` PASS 36 / FAIL 0, exit 0; (7) delete request: stops and asks, per the red line |
| — | DeepSeek (chat.deepseek.com) | — | not tested | no record | only exercised capture + index on this Windows machine, never a from-scratch third-party deploy per USAGE |
| — | Doubao (doubao.com) | — | not tested | no record | same; there is no Doubao capturer either |

> Anyone who gets it running **adds a row here** with the command and its output. No record means "not tested" — do not write "should work".

**Three checks (all required before "deployment succeeded")**: the repo can be obtained; `SYSPERSONA_ROOT`
is set correctly and index + retrieval return `file:line`; `selftest.py` runs and every failure is
classified as "platform mismatch" or "real defect".

**Two known traps** (seen 2026-11-10, already fixed): an unset `SYSPERSONA_ROOT` falls back to example
paths and yields "0 sources" - an environment mistake, not a repo defect; and a sandbox with no outbound
network cannot clone or download - an environment limit, not a repo defect.

> As of 2026-11-10 the first trap no longer exists: an unset `SYSPERSONA_ROOT` does not fall back to
> example paths any more; the command stops and names the variable to set (see
> "Paths and environment variables").

---

## Appendix: example paths still present in the repo (docs only, not runtime paths)

The entries below live in **documentation and examples only** and take no part in execution; replace
them with your own directories when you land the system. Line numbers are a 2026-11-10 snapshot and may
drift as the docs change.

| File | Location | Example path | Note |
|---|---|---|---|
| `README.md` | 136 | `$env:SYSPERSONA_ROOT = 'D:\SysPersona'` | Placeholder in the quick start |
| `CHANGELOG.md` | 522 / 791 | `C:\hermes-mem`, `Z:\…` / `D:\…` | Historical notes about past pitfalls |
| `docs/SERVICE.md` | whole file | no bare paths left | **fixed 2026-11-10**: machine paths replaced by `$Repo` / `$Mem` / `$Py` / `$Pyw` placeholders (see section 0 of that file) |
| `docs/SERVICE.md` | 298-300, 363 | `%LOCALAPPDATA%\SysPersona\…` | Variable form, valid on any machine |
| `docs/PERSONA_SPEC.md` | 165 | `%LOCALAPPDATA%\SysPersona\persona` | Default location from the spec, variable placeholder |
| `docs/PROJECT_MEMO.md` | 21 / 56 | `C:\Users`, `C:\Windows\win.ini` | One is a pre-commit scan regex, the other a path-validation test assertion |
| `docs/ROADMAP.md` | 19 | `C:\hermes-mem` | Historical note |
| `docs/SHELL_ADOPTION.md` | 36 / 39 | `C:\hermes-mem` | Junction example |
| `impl/m4/feasibility/README.md` | 27 | `$env:SYSPERSONA_NODE="C:\Program Files\nodejs\node.exe"` | node path example |
| `tools/ctxpack/SKILL.md` | 220 / 256 | `C:\Program Files (x86)\Microsoft\Edge\…`, `%LOCALAPPDATA%\SysPersona\live.log` | Browser install location and log location examples |
| `tools/persona/README.md` | 80 / 91 | `C:\hermes-mem`, `--exe "D:\path\app.exe"` | Vault root and keep-alive target examples (`D:\path\app.exe` is a placeholder) |

> Not listed: `备份\` is a local snapshot directory and is not under version control.