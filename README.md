# Shadow Agent

A local, terminal-based LLM harness. It interfaces with the operating system,
runs real shell commands, keeps persistent state, and uses an LLM as its
reasoning core.

Three modules run in strict sequence:

| Module | Stage | Responsibility |
|---|---|---|
| **The Monarch** | Analysis | Intercepts input, scans the working directory, rewrites the request into a structured directive with a risk assessment. |
| **The Eminence** | Execution | Runs shell commands and file operations against the real machine, behind guardrails. |
| **The Architect** | Versioning | Snapshots before mutation, journals every decision, restores state when a path proves wrong. |

---

## Install

```bash
git clone <your-remote> shadow-agent
cd shadow-agent
pip install -e .
```

The core framework has **no runtime dependencies**. Onboarding, state,
journalling, snapshots, rollback, and the entire interface run on the standard
library. Install the reasoning core only when you need it:

```bash
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...        # or: ant auth login
```

Run without installing at all:

```bash
python -m shadow_agent init
```

---

## Commands

```
shadow init              render the Shadow Garden Interface, create local state
shadow auth              credential state and the dual-path sign-in wizard
shadow status            print live system and framework state
shadow run "<idea>"      draft a directive from a raw request

shadow checkpoint [msg]  commit the work-tree to the checkpoint repository
shadow reflog            every state HEAD has pointed at, reachable or not
shadow restore <ref>     restore the work-tree from a checkpoint

shadow journal           replay the Architect's event log
shadow snapshots         list restorable snapshots
shadow rollback <id>     restore a snapshot
shadow memory            inspect or edit durable memory
```

Or run without installing:

```bash
python main.py                  boot, authenticate, idle
python main.py "your request"   boot and run one request through the loop
python main.py --no-auth        boot without touching credentials
python main.py --dry-run "…"    plan and gate, execute nothing
python main.py --heuristic "…"  force the offline stub planner
python arena.py                 live test of the permission wall
```

### The core loop

```
raw input → MONARCH → CORAL WALL → EMINENCE → ARCHITECT
            analyse    gate         execute    journal · checkpoint · forge · gc
```

Bounded by `max_steps` and by a failure-streak breaker. `CoreLoop.stream()`
yields an event per phase so the terminal renders progress live rather than
freezing until the run ends.

The planner is pluggable and chosen at boot:

| Condition | Planner |
|---|---|
| credential resolved + SDK installed | `ReasoningPlanner` — Claude Opus 5 |
| no credential, or no SDK | `HeuristicPlanner` — six recognised phrases |
| `--heuristic` | forced stub, for offline work |

The boot panel names which one is active. Degrading silently would let the
framework look like it was reasoning when it was pattern-matching.

### Closing the evolution loop

Before the Monarch calls the API, it queries the Skill Forge:

```
recall → confident match?  → apply the skill directly, no API call
       → weak match?       → inject as prior art the model may reject
       → no match?         → reason from scratch
```

A direct hit requires **relevance ≥ 1.6, confidence ≥ 75%, and at least two
prior uses**. Below that bar a skill is offered as a suggestion, not applied —
a wrong skill executed without review is worse than an API call, because the
permission wall becomes the only thing between a bad recall and a bad outcome.

This is what makes the framework get *faster*, not just more knowledgeable. A
forge that only writes skills is a diary.

### The CORAL permission wall

Every shell command and file write routes through the wall. There is no path
from the Eminence to the OS that bypasses it.

| Tier | Behaviour |
|---|---|
| **DENY** | Refused outright. Not promptable in any mode — `paranoid`, an `always` entry, and a scripted `y` all fail to unlock it. |
| **APPROVE** | The operator is asked, and the exact command is shown. |
| **ALLOW** | Proceeds, still recorded. `SHADOW_PARANOID=1` escalates these to prompts. |

Answers: `y` once · `a` always (this exact command **in this directory**) ·
`n` abort · `q` abandon the run. A bare Enter means abort — a reflex-approved
prompt protects nobody.

The `always` cache canonicalises before matching: paths resolved to absolute
form, whitespace collapsed, combined short flags sorted. Approve `rm -rf build/`
and `rm -rf build`, `rm  -fr  ./build`, and `rm -rf build/../build` are all
covered — while `rm -rf dist` and a different directory still ask.

> The module is named `coral.py` after this project's directive terminology.
> It is **not** derived from [Human-Agent-Society/CORAL](https://github.com/Human-Agent-Society/CORAL),
> which is a multi-agent research orchestrator. That project contributed
> elsewhere — see [NOTICE.md](NOTICE.md).

Headless (`SHADOW_HEADLESS`): `deny` (default) · `allow` (trusted automation
only) · `error` (fail a pipeline loudly). A question nobody can answer is not
a safety mechanism, so the wall never silently proceeds because the prompt
could not be displayed.

### The Skill Forge

A run that executed at least one step and succeeded gets abstracted into
`.shadow/skills/<name>/SKILL.md` — markdown with frontmatter, so the same file
is loadable, greppable, diffable, and hand-editable. Repeating a known
procedure reinforces it rather than duplicating it; a failure on a known skill
lowers its confidence. Skills surface to the Monarch's recall engine.

Honest scope: this is abstraction by **recording**, not generalisation. A
forged skill is a replayable recipe, not transferable understanding.

### Authentication

`shadow auth` never crashes on a machine that lacks credentials, and never
assumes the optional `ant` CLI is installed.

| Path | Requires | Behaviour |
|---|---|---|
| **[1] Web authentication** | `ant` on PATH | Hands off to `ant auth login`, polls until the profile appears, verifies it against the API. When `ant` is absent the option is shown as **unavailable** with an install route — never as a dead choice that faults. |
| **[2] Direct API key** | nothing | Hidden paste, validated against `GET /v1/models` before anything touches disk. An invalid key is never stored. |

Detection follows the SDK's own precedence: `ANTHROPIC_API_KEY` →
`ANTHROPIC_AUTH_TOKEN` → `~/.shadow-agent/credentials.json` → an active `ant`
profile. An unset environment variable does not mean there are no credentials.

Non-interactive callers (CI, a pipe, a redirect) get printed instructions and a
clean exit rather than a prompt into a closed stdin.

`--force` re-runs the wizard, `--forget` deletes the stored key, `--repair`
re-applies owner-only permissions.

> **The local store is permission-restricted, not encrypted.** `0600` on POSIX,
> owner-only ACL with inheritance broken on Windows, stored outside the project
> directory. Anything running as you can still read it. For real secret storage,
> use an OS keyring — `CredentialStore` is the seam for it.

### Checkpoints

`shadow checkpoint` commits the work-tree to a **separate git repository** whose
git-dir lives in `.shadow/checkpoints.git` and whose work-tree points at the
project. Your own `.git` is never touched — no commits on your branch, no
entries in your reflog, no shared index lock. This also means checkpoints work
in a directory that is already inside someone else's repository.

`shadow restore` takes an unconditional checkpoint before restoring, so the
state you overwrite stays recoverable, and `shadow reflog` addresses states that
`log` alone would have lost.

Global flags come **before** the subcommand:

```
--path PATH     project root (default: nearest .shadow/, else nearest .git/)
--width N       force a frame width in columns
--no-color      disable ANSI colour
--ascii         force the ASCII glyph set
```

`init` is the exception to root resolution: it anchors to the current
directory and never walks up. Initialising a parent by accident is not a
recoverable mistake.

---

## State layout

Everything the framework remembers lives in `.shadow/` at the project root:

```
.shadow/
  config.json      resolved configuration (no credentials, ever)
  memory.json      durable preferences and project facts
  journal.jsonl    append-only event log; the audit trail
  sessions/        one record per run
  snapshots/       pre-mutation file copies, each with a manifest
  checkpoints.git/ the out-of-band checkpoint repository
  skills/          forged procedures, one SKILL.md each
```

Two rules govern it. **Append, never overwrite** — an interrupted run leaves a
truncated journal tail, not a corrupted history. **Snapshot before you touch** —
rollback is a file restore against a recorded manifest, not an attempt to
reason backwards about what a command did.

---

## Configuration

`.shadow/config.json`, overlaid by `SHADOW_*` environment variables:

```json
{
  "llm": {
    "model": "claude-opus-5",
    "effort": "high",
    "thinking": "adaptive",
    "max_tokens": 16000,
    "stream": true
  }
}
```

`effort` is the spend dial — `low` for trivial routing, `high` for ordinary
work, `xhigh` for long agentic runs, `max` when correctness outranks cost.
Thinking is adaptive: the model decides when and how deeply to reason.

Environment overrides: `SHADOW_MODEL`, `SHADOW_EFFORT`, `SHADOW_WIDTH`,
`SHADOW_TIMEOUT`, `SHADOW_SHELL`, `SHADOW_HOME`, `SHADOW_HEADLESS`,
`SHADOW_PARANOID`, `SHADOW_ASCII`, `SHADOW_NO_COLOR`, `NO_COLOR`, `FORCE_COLOR`.

**Credentials are never written to disk by this framework.** The API key is
read from the environment or from the SDK's own credential store.

---

## Interface rules

These are enforced, not aspirational:

1. **Every frame closes.** Widths are measured with `visible_width()`, which
   discounts ANSI escapes, treats combining marks as zero-width, and counts
   East Asian wide characters as two columns. `len()` is never used to measure
   anything that will be printed. The test suite asserts rectangularity across
   four widths, with colour, with CJK text, and under the ASCII fallback.
2. **No faked damage.** There are no glitch, corruption, jitter, or scramble
   effects anywhere in this codebase, and none will be added. Atmosphere comes
   from restraint.
3. **Nothing is invented.** Every value in the state panel is read from the
   machine. A value that cannot be determined is reported as unknown.
4. **Every run ends in an Implementory** — status, what was built, what does
   not work, how it was done. A run that lists nothing under *what doesn't
   work* is making a claim, and that claim had better be true.

---

## Guardrails

The Eminence refuses commands matching known-destructive patterns — recursive
deletes rooted at `/` or `~`, disk writes, force-pushes, `git reset --hard`,
piping a network fetch into a shell, fork bombs. A refusal names the pattern
that caught it; the caller opts in explicitly with `allow_destructive=True`.

This is a shallow defence. It matches on command text, so it stops the
accident, not the adversary. Anything sourced from an untrusted place needs a
human in the loop, not a regex.

---

## Provenance

Several mechanisms here were designed after studying other open-source agent
frameworks. **No code was copied from any of them.** Full detail, including
which projects were read for design only because their licence would not permit
derivation, is in [NOTICE.md](NOTICE.md).

| Source | Licence | Mechanism |
|---|---|---|
| [EverMind-AI/Raven](https://github.com/EverMind-AI/Raven) | Apache-2.0 | Out-of-band shadow-git checkpoints · unwrap-before-classify shell policy · transient-vs-hard failure streaks |
| [EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS) | Apache-2.0 | Single path-safety primitive · lock-wait reasoning (implementation rewritten — theirs is POSIX-only) |
| [dolthub/dolt](https://github.com/dolthub/dolt) | Apache-2.0 | Reflog semantics: history alone is not recoverability |
| [lsdefine/GenericAgent](https://github.com/lsdefine/GenericAgent) | MIT | `StepOutcome` contract · phase hooks · bounded turns · streamed progress |
| [aiming-lab/AutoResearchClaw](https://github.com/aiming-lab/AutoResearchClaw) | MIT | Research as a pipeline stage · markdown `SKILL.md` format |
| [666ghj/MiroFish](https://github.com/666ghj/MiroFish) | **AGPL-3.0** | Read only — backoff-with-jitter written independently |
| [MemoriLabs/Memori](https://github.com/MemoriLabs/Memori) | **non-standard** | Read only — two-stage recall written independently |
| [ANative-Lab/EvoAgentX](https://github.com/ANative-Lab/EvoAgentX) | **non-standard** | Read only — self-evolution framing, no code derived |
| [Human-Agent-Society/CORAL](https://github.com/Human-Agent-Society/CORAL) | Apache-2.0 | Exit-quality classification · circuit-breaker rule · durable `fsync`-before-rename writes |

## Development

```bash
python -m unittest discover -s tests -v     # 158 tests
ruff check shadow_agent
```

## License

MIT. See [NOTICE.md](NOTICE.md) for third-party attribution.
