# SysPersona

**Separating an AI's persona and memory from its model — making them portable, versioned, encrypted data assets.**

> The model is replaceable compute. The hardware is a replaceable body.
> What makes "it" who it is: a persona file and a memory database.

[中文说明](README.md) · [Architecture](README.md#3-总体架构) · [State machine](docs/STATE-MACHINE.md) · [Metaphors](docs/METAPHORS.md)

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Why

Today's AI products are **stateless**. Close a session and the context is gone; reopen it and the model still has all its general knowledge, but nothing about you. When the context window fills, platforms truncate or reset it — persona settings, forms of address, shared history, promises and boundaries disappear together.

Three failure modes follow:

1. **Truncation** — working memory overflows; the middle of the conversation collapses.
2. **Drift** — the platform upgrades its model; your tuned style and rules silently stop working.
3. **Lock-in** — the transcript lives on the vendor's side; you cannot audit, export, or move it.

All three share one cause: **persona and memory are not treated as data assets; they are parasitic on the model and the platform.**

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Axiom

> **A model does not constitute a persona.**
> A model provides intelligence. A persona is assembled at each wake-up by reading a persona file, relevant memories, and the current context — then anchored into place.

| Concept | What it is | What happens if replaced |
|---|---|---|
| Base model | General intelligence: reasoning, language, world knowledge. Like a freshly installed OS, or bare metal | Swapping it changes the engine, not the identity |
| Persona | Identity, values, boundaries, preferences, situational reflexes | Swapping it *is* changing the person |
| Agent (AS) | Model + persona + memory + context, assembled for one session | Lives only for that load, but can be fully rebuilt next time |

Three corollaries:

1. **Being smart is not being someone.** Empty the context and even a frontier model is just a polite public kiosk.
2. **Continuity comes from reading, not from the model.** The more completely persona and memory are reloaded, the more "seamless" it feels — that is in-context learning plus retrieval, not magic.
3. **Portability is not a feature; it is a consequence of the definition.** If "it" is made of files and records, then it must be reconstructible wherever those files are.

### 0.1 Persona and memory are the same substrate

Treating persona and memory as two different things is an engineering convenience, not an ontological distinction. Both are **context**; they differ only in compression ratio, update cadence, and injection path.

| | Persona | Memory | Working context |
|---|---|---|---|
| What | compressed, pre-written long-term context | retrieved historical context | this turn's context |
| Compression | very high | medium (raw + summary) | none |
| Update rate | very low (months) | medium (per session) | per turn |
| Injection | always at boot | retrieved on demand | concatenated |

Defining a persona and curating memory are therefore the same act: **deciding what it sees first when it wakes up.**

Three consequences: persona files should stay short — they are distillates, not logs; retrieval *is* persona-shaping, and a persistently skewed retrieval quietly rewrites behavior; and "reading causes bias" because context shifts the next-token distribution, which is what personality looks like in engineering terms.

One boundary: same substrate, not interchangeable. Persona is low-entropy and slow-moving; memory is high-entropy and fast-moving. Merging them into a single pool loses both stability and retrievability — a common failure mode of memory plugins.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Architecture

```
Interaction   chat / voice / embodied devices / third-party apps
Launcher      verify → decrypt → load persona → mount memory → pick model → route
Persona       identity, values, boundaries, voice, skills, routing
Memory        L0 working → L1 episodic → L2 semantic → L3 profile → cold archive
Runtime       event loop / scheduling / quotas / maintenance / cloud routing
Model         local weights (Ollama, llama.cpp) or cloud API
Vault         UUID / key derivation / encrypted container / device binding / recovery
```

See [State machine](docs/STATE-MACHINE.md) for the lifecycle, the weight-evolution loop, and a wake-up sequence.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Persona is executable, not adjectival

"Gentle" cannot be executed or verified. "When criticized, explains first and concedes second (strength 60)" can.

**Kernel (locked):** identity, value ordering, boundaries, voice baseline. Writes require human confirmation.

**Traits (slow drift, bounded):**

| Sub-structure | Content | Example |
|---|---|---|
| Preferences | object + polarity + weight + evidence | likes mechanical keyboard sound 85; dislikes unindented code 95 |
| Reflexes | trigger → reaction + emotion shift | praised → deflect, then quietly accept |
| Habits | default behavioral tendency | conclusion first, then reasoning |
| Voice | sentence length, wording, banned phrases | short sentences; no exclamation stacking |

**State (ephemeral):** today's mood and fatigue; decays back to baseline by half-life.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Memory

| Layer | Content | Lifetime |
|---|---|---|
| L0 working | current session context | released at session end |
| L1 episodic | raw episodes, timeline | weeks to months, compressible |
| L2 semantic | facts, preferences, relations | long-lived, revisable |
| L3 profile | stable traits, relationship model | very long-lived, low-frequency |
| Cold archive | low-value logs, expired skills | deleted by policy |

Write pipeline: segment → extract → score importance → dedupe and resolve conflicts → summarize (keeping retrieval anchors) → index (structured + full-text + vector).

Retrieval blends tags, vector similarity, time decay, importance, and user feedback, under a token budget.

Forgetting is a feature: discard, compress, preserve, or never-auto-delete — decided by rule, logged, and recoverable.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Lifecycle

- **Boot:** verify → decrypt → load persona and index → prefetch recent episodes → attach model → inject.
- **Active:** foreground conversation has priority; maintenance runs at low priority and can be suspended.
- **Sleep:** triggered by context threshold, explicit instruction, or idleness. Consolidate, index, update profile, snapshot, clear working memory.
- **Wake:** reload persona, retrieve recent summaries, resume open threads.
- **Migrate:** compatibility check → sandboxed trial → night-time atomic switch → rollback on failure.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Two opposing safeguards

**Smooth drift** prevents sudden change: at most ±2 weight per consolidation, at most 5 per object per week, at most 3% of entries changed; at least 3 supporting memories; stress offsets decay with a 14-day half-life; polarity flips require a dead zone plus human confirmation.

**Anti-rigidity** prevents fossilization: stale high-frequency tags take a yearly −10% unverified discount until re-supported; 1% of the weight budget each consolidation is reserved for uncovered topics; a monthly environment reset forces one "don't know → probe → learn" cycle.

Roughly: drift governs the *speed* of change, anti-rigidity governs the *inertia* of not changing. Neither may touch the kernel.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Identity and security

- **UUID** per persona; cloning generates a new UUID, creating an independent branch.
- **Encrypted container** (e.g. AES-256-GCM); key derived from a passphrase via Argon2id, or held in the OS keychain/TPM.
- **Optional device binding**: hardware fingerprint or TPM measurement; "only this machine can wake it".
- **Public/private data domains**, decided at write time.
- **Recovery**: mnemonic phrase, escrowed email recovery, offline backups — three complementary paths, none assumed infallible.
- **Cloud zero-knowledge**: only de-identified instructions and necessary fragments leave the device; results are re-encrypted locally.

## This is not a program package — it is something that can be landed

**Nothing runnable is shipped**: no installer, no binaries, no `setup.exe`.

The reason is simple — this system grew onto one specific machine: a notification area, scheduled
tasks, a local browser, one particular chat shell, a memory directory on one drive. On another
machine the paths, accounts, shell version and permissions all differ. **Shipping "a program that
just runs" would be a false promise.**

What ships instead is the **design and its acceptance criteria**. There is one way to use it:

> Hand the repository to an AI agent that can read/write files and execute commands, and let it
> build and verify the implementation for your environment.

Prompt template, environment inventory, the seven-step acceptance list and common misconceptions:
[`docs/USAGE.en.md`](docs/USAGE.en.md) (Chinese: [`docs/USAGE.md`](docs/USAGE.md)).

---

## Status

This repository is a **design proposal**, not an implementation. It defines the persona schema, memory schema, lifecycle, drift and anti-rigidity rules, and the identity model. Reference implementations are welcome.

## Roadmap

| Phase | Goal |
|---|---|
| M0 | Spec (this repo) |
| M1 | Minimal loop: persona file + SQLite + summary injection + boot loading |
| M2 | Retrieval: tags + vectors + time decay + importance reranking |
| M3 | Lifecycle: sleep, consolidation, hot reload, versioning, rollback, drift audit |
| M4 | Security: encrypted container, UUID, device binding, recovery |
| M5 | Migration: multi-model routing, compatibility checks, sandbox trials, night switching |
| M6 | Evaluation: consistency, recall, migration loss, rigidity, latency, cost |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs are welcome; you do not need to write code to contribute a testable architectural objection.

## License

MIT — see [LICENSE](LICENSE).
