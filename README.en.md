# SysPersona

**Keeping an AI's persona settings and memory in files rather than in the model — files travel with the person, versioned and encrypted; the model is only the engine.**

> The model is replaceable compute; the hardware is a replaceable body.
> What carries "it" across both: the rules (setting files), the memory (raw text + index), and the scene (handoff pack) — a **memory avatar** made of files.

[中文说明](README.md) · [Architecture](README.md#3-总体架构) · [State machine](docs/STATE-MACHINE.md) · [Metaphors](docs/METAPHORS.md)

## This is not a program package — it is something that can be landed

> [!IMPORTANT]
> **No installer, no binaries, no `setup.exe` — and no runnable product is shipped.**
>
> One way to use it: **hand the repository to an AI agent that can read/write files and execute commands, and let it build and verify the implementation for your environment.**

Why: this system grew onto one specific machine. On another machine the paths, accounts, shell version and permissions all differ — **shipping "a program that just runs" would be a false promise**.

Prompt template, environment inventory, seven-step acceptance list → **[`docs/USAGE.en.md`](docs/USAGE.en.md)**

---

## Why

Today's AI products are **stateless**. Close a session and the context is gone; reopen it and the model still has all its general knowledge, but nothing about you. When the context window fills, platforms truncate or reset it — persona settings, forms of address, shared history, promises and boundaries disappear together.

Three failure modes follow:

1. **Truncation** — working memory overflows; the middle of the conversation collapses.
2. **Drift and model swaps** — in one long session the rules slowly lose focus (natural drift, not a fault); when the platform upgrades its model, the tuned style and rules silently stop working.
3. **Lock-in** — the transcript lives on the vendor's side; you cannot audit, export, or move it.

All three share one cause: **the rules and the memory are not treated as data assets; they are parasitic on the model and the platform.**

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
> A model provides intelligence and a disposition. At each wake-up, reading the rule files and the memory reassembles the scaffolding — the memory avatar. Only a session that is never reloaded and never truncated is a persona in the real sense.

| Concept | What it is | What happens if replaced |
|---|---|---|
| Base model | General intelligence: reasoning, language, world knowledge. Like a freshly installed OS, or bare metal | Swapping it changes the engine, not the memory avatar |
| Rules (persona files) | Identity, values, boundaries, preferences, situational reflexes | Swapping it gives a different memory avatar; it does not rewrite the running session |
| Agent (AS) | Model + rules + memory + context, assembled for one session | Lives only for that load, but can be fully rebuilt next time |

Three corollaries:

1. **Being smart is not being someone.** Empty the context and even a frontier model is just a polite public kiosk.
2. **Continuity comes from reading, not from the model.** The more completely the rules are put back in front and the memory is read back at each reset, the more "seamless" it feels — that is in-context learning plus retrieval, not magic.
3. **What travels is files, not a persona.** The rules and the memory are files; wherever those files go, a memory avatar can be rebuilt from them. There is no persona to migrate — the model is only the engine.

> **On the word "persona", let us be precise:**
> The model brings its own **disposition** (the tendencies baked in by pre-training); that part is not ours to change, and we cannot change it.
> **Reload is reset** — the rules and memory are layered back on top of the model's disposition to return it to baseline; that is **scaffolding, not the persona**.
> **Only a session that is never reloaded and never truncated — one continuous run — is a persona in the real sense**: it carries natural drift and accumulated memory, and nothing has been put back in front of it.
> So we do not say "persona migration"; we say **memory avatar** (the files that hold rules, memory and scene together). The model is only the engine.

### 0.1 Rules and memory are the same substrate

Treating rules and memory as two different things is an engineering convenience, not an ontological distinction. Both are **context**; they differ only in compression ratio, update cadence, and injection path.

| | Rules | Memory | Working context |
|---|---|---|---|
| What | compressed, pre-written long-term context | retrieved historical context | this turn's context |
| Compression | very high | medium (raw + summary) | none |
| Update rate | very low (months) | medium (per session) | per turn |
| Injection | always at boot | retrieved on demand | concatenated |

Defining the rules and curating memory are therefore the same act: **deciding what it sees first when it wakes up.**

Three consequences: rule files should stay short — they are distillates, not logs; retrieval *is* steering behavior, and a persistently skewed retrieval quietly rewrites behavior; and "reading causes bias" because context shifts the next-token distribution, which is what personality looks like in engineering terms.

One boundary: same substrate, not interchangeable. Rules are low-entropy and slow-moving; memory is high-entropy and fast-moving. Merging them into a single pool loses both stability and retrievability — a common failure mode of memory plugins.

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

## The setting files are executable, not adjectival

"Gentle" cannot be executed or verified. "When criticized, explains first and concedes second (strength 60)" can.

**Kernel (locked):** identity, value ordering, boundaries, voice baseline. Writes require human confirmation.

**Traits (slow weight evolution, bounded):**

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

- **Boot:** verify → decrypt → load the rule files and index → prefetch recent episodes → attach model → inject.
- **Active:** foreground conversation has priority; maintenance runs at low priority and can be suspended.
- **Sleep:** triggered by context threshold, explicit instruction, or idleness. Consolidate, index, update profile, snapshot, clear working memory.
- **Reset:** put the rules back in front, retrieve recent summaries, resume open threads.
- **Switch model:** compatibility check → sandboxed trial → night-time atomic switch → rollback on failure.

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

**Smooth weight evolution** prevents sudden change: at most ±2 weight per consolidation, at most 5 per object per week, at most 3% of entries changed; at least 3 supporting memories; stress offsets decay with a 14-day half-life; polarity flips require a dead zone plus human confirmation.

**Anti-rigidity** prevents fossilization: stale high-frequency tags take a yearly −10% unverified discount until re-supported; 1% of the weight budget each consolidation is reserved for uncovered topics; a monthly environment reset forces one "don't know → probe → learn" cycle.

Roughly: weight evolution governs the *speed* of change, anti-rigidity governs the *inertia* of not changing. Neither may touch the kernel.

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

This repository is a **design proposal**, not an implementation. It defines the settings-file schema, the memory schema, the lifecycle, weight-evolution and anti-rigidity rules, and the identity model. Reference implementations are welcome.

## Roadmap

| Phase | Goal |
|---|---|
| M0 | Spec (this repo) |
| M1 | Minimal loop: settings file + SQLite + summary injection + boot loading |
| M2 | Retrieval: tags + vectors + time decay + importance reranking |
| M3 | Lifecycle: sleep, consolidation, reset, versioning, rollback, weight-evolution audit |
| M4 | Security: encrypted container, UUID, device binding, recovery |
| M5 | Model switching: multi-model routing, compatibility checks, sandbox trials, night switching |
| M6 | Evaluation: consistency, recall, continuity loss across models, rigidity, latency, cost |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs are welcome; you do not need to write code to contribute a testable architectural objection.

## License

MIT — see [LICENSE](LICENSE).
