# Architecture

## The pipeline

```
        raw input
            │
            ▼
   ┌─────────────────┐
   │   THE MONARCH   │   scan the ground · classify intent · assess risk
   │    analysis     │   → Directive(objective, intent, risk, steps, scan)
   └────────┬────────┘
            │
            ▼
   ┌─────────────────┐
   │  THE ARCHITECT  │   snapshot every path the directive will touch
   │   pre-commit    │   → Snapshot(id, manifest)
   └────────┬────────┘
            │
            ▼
   ┌─────────────────┐
   │  THE EMINENCE   │   run commands · write files · capture everything
   │    execution    │   → ExecutionResult(code, stdout, stderr, duration)
   └────────┬────────┘
            │
            ▼
   ┌─────────────────┐
   │  THE ARCHITECT  │   journal the outcome · rollback on failure
   │   post-commit   │   → journal.jsonl, sessions/
   └────────┬────────┘
            │
            ▼
      IMPLEMENTORY
```

The Architect appears twice on purpose. Snapshotting after execution is
worthless — the state you wanted is already gone.

## Module boundaries

| Package | Owns | Must not |
|---|---|---|
| `core/` | System state retrieval, error hierarchy | Import from `ui/` at module scope |
| `monarch/` | Directory scanning, intent classification, directive drafting | Execute anything |
| `eminence/` | Subprocess execution, file I/O, guardrails | Decide *whether* something should run |
| `architect/` | `.shadow/` layout, journal, memory, snapshots, rollback | Interpret intent |
| `llm/` | The single LLM boundary | Be imported by `monarch` or `eminence` directly |
| `ui/` | All rendering | Read the filesystem |
| `config.py` | Defaults, load/save, env overlay | Hold credentials |
| `cli.py` | Argument parsing, command dispatch, composition | Contain business logic |

The one rule that keeps this honest: **`ui/` never touches the filesystem and
`core/` never renders.** State collection and state display are separate
concerns, which is why the onboarding sequence can be tested against a
synthetic `SystemState` with no machine attached.

## Why the LLM sits behind one method

`ReasoningCore` is a `Protocol` with a single method, `complete()`. Everything
above it — analysis, planning, execution — is exercisable with no network and
no API key. That is what makes 26 alignment tests run in 0.13 seconds, and it
is what will let a second provider drop in without touching the Monarch.

`build_request()` is deliberately pure. The request shape can be asserted in a
test without a client.

## The alignment contract

`ui/render.py` exists to enforce one invariant:

> Every line in a rendered block occupies the same number of terminal columns.

`len()` counts code points. A terminal counts columns. They disagree whenever
ANSI escapes, combining marks, or East Asian wide characters are present, and
all three occur in real output. `visible_width()` is the only measurement any
renderer may use; the tests assert rectangularity across widths, colour states,
scripts, and glyph sets.

Corollary: **colour is applied last.** Truncating a string that already carries
escapes can orphan a colour code and bleed it down the rest of the line.

## Degradation ladder

Each rung is a real terminal someone will run this in:

| Condition | Behaviour |
|---|---|
| Legacy console code page | `sys.stdout.reconfigure("utf-8")` at startup |
| ...and that fails | Glyph set falls back to ASCII; widths unchanged |
| `NO_COLOR` / not a TTY / `TERM=dumb` | Colour off; widths unchanged |
| Windows without VT support | `SetConsoleMode` attempted, then colour off |
| Terminal narrower than 56 columns | Clamped to 56; content truncates, frames still close |
| `git` absent, or not a repository | Reported honestly; snapshots still function |
| `anthropic` not installed | Everything except `complete()` still runs |

Nothing on this ladder is an error path. Each is a supported configuration.

## Scope boundaries (current build)

- **Rollback is file-level.** It restores the contents of paths recorded in a
  manifest. It does not reverse side effects outside those paths — installed
  packages, network calls, database writes.
- **Intent classification is lexical.** It recognises the shape of a request,
  not its meaning. `Directive.confidence` caps at 0.6 on this path and
  `Directive.source` reports `"heuristic"`.
- **Guardrails match command text.** They stop the accident, not the
  adversary.
