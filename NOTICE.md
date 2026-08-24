# Attribution and provenance

Shadow Agent is MIT-licensed. Several of its mechanisms were designed after
studying other open-source projects. This file records exactly what came from
where, and on what terms — because "inspired by" is not a licence, and a public
repository should be precise about which of the two it means.

Nothing in this repository is a copy-paste of another project's source. Every
module is an independent implementation. The distinction below is between
projects whose licence *would* permit derivation, and projects whose licence
would not — the latter were read for design only, and are marked as such.

---

## Apache-2.0 sources — derivation permitted, attribution given

### EverMind-AI/Raven — https://github.com/EverMind-AI/Raven
Apache License 2.0.

| Their file | Our module | What was taken |
|---|---|---|
| `raven/agent/loop/checkpoint.py` | `shadow_agent/architect/shadowgit.py` | The out-of-band shadow-git design: a separate `--git-dir` with `--work-tree` pointed at the real project, so checkpoints never touch the user's own `.git`. Also the layered `info/exclude` defence and the best-effort failure policy. |
| `raven/agent/tools/shell_policy.py` | `shadow_agent/eminence/policy.py` | Unwrap-before-classify: peel `sudo`/`env`/`nohup`/`bash -c` and leading assignments before judging a command. Security-sensitive ordering (deny before approve) and fail-closed on parse failure. |
| `raven/agent/loop/failure_streak.py` | `shadow_agent/eminence/failure.py` | The two judgements that make stuck-loop detection work: transient failures must not count, and the streak must be keyed on failure *class*, not on the tool alone. |

### EverMind-AI/EverOS — https://github.com/EverMind-AI/EverOS
Apache License 2.0.

| Their file | Our module | What was taken |
|---|---|---|
| `src/everos/core/persistence/markdown/path_safety.py` | `shadow_agent/core/pathsafe.py` | A single path-safety primitive with an idempotence guarantee, as the one place CWE-22 is defended. |
| `src/everos/core/persistence/locking.py` | `shadow_agent/core/locking.py` | The *reasoning only* — poll non-blocking rather than block in a thread; make the wait visible; set the timeout for diagnosis rather than recovery. **Their implementation is POSIX-only (`fcntl.flock`) and was not portable to this project's primary platform.** Ours is a cross-platform rewrite. |

### dolthub/dolt — https://github.com/dolthub/dolt
Apache License 2.0.

| Concept | Our module | What was taken |
|---|---|---|
| `dolt reflog` | `shadow_agent/architect/shadowgit.py` | The insight that a commit history is only half of recoverability: a reset makes commits unreachable from `log` while the reflog still addresses them. `ShadowGit.reflog()` and the unconditional pre-restore checkpoint exist because of this. |

---

## Read for design only — no derivation

### 666ghj/MiroFish — https://github.com/666ghj/MiroFish
**AGPL-3.0.** Copyleft incompatible with this project's MIT licence.

`backend/app/utils/retry.py` was read and confirmed that exponential backoff
**with jitter** was worth having. `shadow_agent/core/retry.py` is written from
scratch against the standard formulation of that pattern, which is
long-established prior art independent of any single project. No MiroFish code
is present in this repository.

MiroFish is a swarm-simulation platform (OASIS agent profiles, Zep graph
memory, social-network simulation), not an agent harness. Its architecture does
not transfer to this one.

### MemoriLabs/Memori — https://github.com/MemoriLabs/Memori
**Non-standard licence** (GitHub reports `NOASSERTION`); terms cannot be
assumed compatible.

`core/src/retrieval/pipeline.rs` was read and confirmed the two-stage retrieval
shape — wide candidate generation, then narrow reranking, scoped by entity.
That shape is standard information-retrieval practice and predates any single
implementation. `shadow_agent/monarch/recall.py` is written from scratch. No
Memori code is present in this repository.

---

## If you believe this attribution is wrong

Open an issue. Provenance claims should be checkable, and a correction is
cheaper than an argument.
