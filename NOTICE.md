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

### lsdefine/GenericAgent — https://github.com/lsdefine/GenericAgent
MIT.

| Their file | Our module | What was taken |
|---|---|---|
| `agent_loop.py` | `shadow_agent/loop/core.py` | The `StepOutcome(data, next_prompt, should_exit)` contract; hook points around every phase; a hard turn bound; and generator-yielded progress, so a long run is observably working rather than indistinguishable from a hang. |

### Human-Agent-Society/CORAL — https://github.com/Human-Agent-Society/CORAL
Apache License 2.0.

**Correction.** An earlier revision of this file recorded CORAL as "not found".
That was wrong — the search during the assimilation pass missed it, and the
repository is real: an open-source autoresearch system built on autonomous
coding agents, accepted at COLM 2026.

It is also **not what our `eminence/coral.py` implements.** That module is a
human-in-the-loop permission wall, named for the terminology in this project's
own directive. CORAL is a multi-agent evolutionary research orchestrator with
grading, shared knowledge, and island-model evolution. The two have nothing to
do with each other, and the module name is retained only for continuity with
the directive. What CORAL actually contributed is below.

| Their file | Our module | What was taken |
|---|---|---|
| `coral/agent/exit_classifier.py` | `shadow_agent/eminence/failure.py` | **Exit 0 is not the same as success.** A process that returns instantly with no output probably did nothing. Also the circuit-breaker rule that only non-productive exits may count toward a burst — counting clean completions turns a safety mechanism into a bug that stops a working agent. |
| `coral/hub/auto_stop.py` | `shadow_agent/core/atomic.py` | Durable atomic write: `fsync` the temp file **before** the rename, and create it in the destination directory because `os.replace` across filesystems is not atomic. This fixed a real defect here — every atomic write in this framework was atomic but not durable. |
| `coral/hub/_island.py` | — | Read for the island-model design and its path-safety validation. Not adopted: this framework runs one agent, and an island model without a population is ceremony. |
| `coral/hub/skills.py` | — | Independent confirmation of the `skills/<name>/SKILL.md` + frontmatter layout we had already built from AutoResearchClaw. They parse YAML frontmatter; ours is JSON, to stay dependency-free. |

### aiming-lab/AutoResearchClaw — https://github.com/aiming-lab/AutoResearchClaw
MIT.

| Their pattern | Our module | What was taken |
|---|---|---|
| `.claude/skills/<name>/SKILL.md` | `shadow_agent/architect/skills.py` | Skills as markdown with frontmatter — loadable, greppable, diffable, hand-editable. A skill a human cannot read is a skill nobody can audit. |
| Research as a pipeline stage | `shadow_agent/monarch/research.py` | Gathering and synthesis happen *before* execution is handed anything, rather than as a tool the executor calls. |

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

### ANative-Lab/EvoAgentX — https://github.com/ANative-Lab/EvoAgentX
**Non-standard licence** (`NOASSERTION`); terms cannot be assumed compatible.

Read for the self-evolution framing — that an agent should abstract solved
problems into reusable capability. `shadow_agent/architect/skills.py` is
written from scratch. No EvoAgentX code is present in this repository.

---

## If you believe this attribution is wrong

Open an issue. Provenance claims should be checkable, and a correction is
cheaper than an argument.
