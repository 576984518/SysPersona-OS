# Usage: hand the repo to an AI agent

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
| Core: index / retrieval / self-test | **standard library only** (Python 3.10+) | nothing |
| Local semantic retrieval (optional) | `bge-m3` + vector deps | falls back to keyword search |
| Tray | `pystray` + `Pillow` | no tray icon; background jobs still run |
| Platform | Windows (`msvcrt` / `winreg`) | will not run elsewhere - by design, not a bug |

**Environment variable**: `SYSPERSONA_ROOT` must point at your memory root and **must be set by you**;
without it scripts fall back to example paths, which are not real paths on your machine.

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
| | Qwen (qianwen.com) | | | pending | |
| | DeepSeek (chat.deepseek.com) | | | pending | |
| | Doubao (doubao.com) | | | pending | |

**Three checks (all required before "deployment succeeded")**: the repo can be obtained; `SYSPERSONA_ROOT`
is set correctly and index + retrieval return `file:line`; `selftest.py` runs and every failure is
classified as "platform mismatch" or "real defect".

**Two known traps** (seen 2026-11-10, already fixed): an unset `SYSPERSONA_ROOT` falls back to example
paths and yields "0 sources" - an environment mistake, not a repo defect; and a sandbox with no outbound
network cannot clone or download - an environment limit, not a repo defect.