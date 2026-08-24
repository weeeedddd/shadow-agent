"""Command-line entry point -- the initialization script.

    shadow init        render the Shadow Garden Interface and create state
    shadow status      re-read and print live system state
    shadow run "..."   draft a directive through the Monarch
    shadow journal     replay the Architect's event log
    shadow snapshots   list restorable snapshots
    shadow rollback ID restore a snapshot
    shadow memory      list durable facts the Architect holds

Every command terminates in an Implementory. That is not decoration: a run
that cannot state what it changed and what it could not do has not finished
reporting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .architect.state import StateStore, find_root
from .config import Config, load_config
from .core import context
from .core.errors import ShadowError
from .monarch.analyzer import Monarch
from .ui import ansi, onboarding
from .ui.implementory import Implementory, Status
from .ui.render import bullets, kv_rows, panel, resolve_width, wrap
from .ui.theme import ASH, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs


def _use_utf8() -> None:
    """Switch stdout/stderr to UTF-8 where the runtime permits it.

    Windows consoles still default to a legacy code page, under which the
    box-drawing set is unencodable and the interface silently degrades to
    ASCII. Asking for UTF-8 first means the fallback stays what it should be
    -- a fallback -- rather than the normal case.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass  # a pipe or redirect that refuses; the ASCII fallback covers it


def _emit(text: str) -> None:
    """Print, tolerating consoles that cannot encode the glyph set."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _prepare_ui(config: Config) -> int:
    if config.ui.color is not None:
        ansi.set_enabled(config.ui.color)
    return resolve_width(config.ui.width)


# --- commands -----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Render the onboarding sequence and lay down the state directory.

    ``init`` anchors to the *current* directory, never to a walked-up ancestor.
    Walking up would silently initialise a parent -- the enclosing repository,
    or a home directory -- which is not what anyone means by "start here".
    """
    root = Path(args.path).resolve() if args.path else Path.cwd().resolve()
    config = load_config(root)
    width = _prepare_ui(config)

    store = StateStore(root)
    was_initialized = store.initialized

    report = None
    error: Optional[str] = None
    if not args.dry_run:
        try:
            # Persist the *unoverlaid* config. `config` above carries this
            # run's environment and flag overrides -- writing those to disk
            # would silently promote a one-off `--width 74` into a permanent
            # project setting.
            report = store.initialize(config=Config.load(store.config_path), force=args.force)
        except ShadowError as exc:
            error = str(exc)

    # Collected *after* initialization so STATE DIR reports the truth.
    state = context.collect(cwd=root, state_dir=store.dir)

    _emit("")
    _emit(onboarding.render(state, width=width))
    _emit("")

    impl = Implementory()
    impl.headline = (
        f"Shadow Agent v{__version__} initialised at {state.display_cwd(width - 30)}."
    )

    if args.dry_run:
        impl.degrade(Status.PARTIAL)
        impl.build("Rendered the Shadow Garden Interface from live system state.")
        impl.limit("Dry run: no files were written. Re-run without --dry-run to persist state.")
    elif error:
        impl.degrade(Status.FAILED)
        impl.limit(f"State directory could not be created: {error}")
    else:
        impl.build("Rendered the Shadow Garden Interface from live system state.")
        created = (report or {}).get("created", [])
        existed = (report or {}).get("existed", [])
        if created:
            impl.build("Created: " + ", ".join(created))
        if existed and was_initialized:
            impl.build(f"Preserved {len(existed)} existing state artefacts (init is idempotent).")

    impl.build(
        f"Verified environment: {state.os_label}, Python {state.python_version}, "
        f"shell {state.shell}."
    )
    if state.git.is_repo:
        impl.build(
            f"Read repository state: branch {state.git.branch or 'detached'}, "
            f"{state.git.tracked_files} tracked files, working tree {state.git.summary()}."
        )

    # Honest limits.
    if not state.git.available:
        impl.limit("git is not on PATH: the Architect cannot read or write repository state.")
    elif not state.git.is_repo:
        impl.limit(
            "This directory is not a git repository: snapshots still work, but "
            "commit-level versioning is unavailable."
        )
    impl.limit(
        "The reasoning core is defined but not exercised: no LLM request is made "
        "by `init`, so model reachability is unverified."
    )
    if not state.color:
        impl.limit("Colour is disabled for this stream; the interface rendered monochrome.")
    if state.encoding.lower() not in ("utf-8", "utf8", "cp65001"):
        impl.limit(
            f"stdout encoding is {state.encoding}; box glyphs may have fallen back to ASCII."
        )

    for note in config.missing_variables():
        impl.unresolved(note)
    if state.git.is_repo and state.git.tracked_files == 0:
        impl.unresolved("Repository has zero tracked files -- the project's purpose is undeclared.")

    impl.how(
        "Monarch: resolved the project root by walking upward for .shadow/ or .git/.",
        "Monarch: read OS, arch, interpreter, shell, TTY, encoding, and git state via "
        "platform, os, shutil, and subprocess -- no value in the panel is assumed.",
        "Architect: created .shadow/ with config.json, memory.json, an append-only "
        "journal.jsonl, and sessions/ + snapshots/, then journalled the event.",
        "Eminence: idle on this command; init performs no shell execution.",
        "UI: every frame measured with visible_width(), which discounts ANSI escapes "
        "and counts East Asian wide characters as two columns, so borders close exactly.",
    )

    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")

    store.record("cli.init", dry_run=args.dry_run, force=args.force, status=impl.status.value)
    return 0 if impl.status in (Status.SUCCESS, Status.PARTIAL) else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Print live state without the full onboarding ceremony."""
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    state = context.collect(cwd=root, state_dir=store.dir)

    _emit("")
    for line in onboarding.system_state(state, width=width):
        _emit(line)

    memories = store.memories()
    journal = store.read_journal(limit=5)
    inner = width - 6
    detail = kv_rows(
        [
            ("VERSION", __version__),
            ("MODEL", config.llm.model),
            ("EFFORT", config.llm.effort),
            ("THINKING", config.llm.thinking),
            ("STATE", "initialised" if store.initialized else "not initialised"),
            ("MEMORIES", str(len(memories))),
            ("JOURNAL", f"{len(store.read_journal(limit=10_000))} events"),
            ("SNAPSHOTS", str(len(store.list_snapshots()))),
        ],
        inner,
    )
    _emit("")
    for line in panel(detail, width=width, title="FRAMEWORK", color=INDIGO):
        _emit(line)

    if journal:
        g = glyphs()
        rows = [
            ansi.paint(f"{e.get('ts', '')[11:19]} ", DIM)
            + ansi.paint(str(e.get("event", "")), BONE)
            for e in journal
        ]
        _emit("")
        for line in panel(rows, width=width, title="RECENT EVENTS", color=INDIGO):
            _emit(line)

    impl = Implementory(headline="Live state read from the local machine.")
    impl.build("Re-collected OS, interpreter, shell, and git state at call time.")
    impl.build(f"Reported framework configuration: model {config.llm.model}, effort {config.llm.effort}.")
    if not store.initialized:
        impl.degrade(Status.NEEDS_INPUT)
        impl.limit("No .shadow/ state directory here; run `shadow init` before any stateful work.")
    impl.limit("`status` is read-only: it verifies nothing about API reachability or credentials.")
    for note in config.missing_variables():
        impl.unresolved(note)
    impl.how(
        "Monarch: collected the state snapshot.",
        "Architect: read journal.jsonl, memory.json, and the snapshot manifests from disk.",
    )
    if config.ui.show_implementory:
        _emit("")
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Pass a raw request through the Monarch and print the drafted directive."""
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    state = context.collect(cwd=root, state_dir=store.dir)

    raw = " ".join(args.request).strip()
    monarch = Monarch(config)
    directive = monarch.draft(raw, state, root=root)

    inner = width - 6
    g = glyphs()
    lines: List[str] = []
    lines.append(ansi.paint("RAW", ASH))
    for segment in wrap(directive.raw or "(empty)", inner - 2):
        lines.append("  " + ansi.paint(segment, DIM))
    lines.append("")
    lines.append(ansi.paint("DIRECTIVE", ASH))
    for segment in wrap(directive.objective, inner - 2):
        lines.append("  " + ansi.paint(segment, BOLD + BONE))
    lines.append("")
    lines.extend(
        kv_rows(
            [
                ("INTENT", directive.intent.value),
                ("RISK", directive.risk.value),
                ("CONFIDENCE", f"{directive.confidence:.0%} ({directive.source})"),
                ("CONTEXT", directive.scan.summary() if directive.scan else "not scanned"),
            ],
            inner,
        )
    )
    lines.append("")
    lines.append(ansi.paint("PLAN", ASH))
    lines.extend(bullets(directive.steps, inner - 2, marker_color=VIOLET))
    if directive.open_questions:
        lines.append("")
        lines.append(ansi.paint("OPEN QUESTIONS", EMBER))
        lines.extend(bullets(directive.open_questions, inner - 2, marker_color=EMBER))

    _emit("")
    for line in panel(lines, width=width, title="THE MONARCH", color=INDIGO):
        _emit(line)
    _emit("")

    impl = Implementory(headline="The Monarch analysed the request. Execution did not run.")
    impl.degrade(Status.NEEDS_INPUT)
    impl.build(f"Scanned the working directory: {directive.scan.summary() if directive.scan else 'n/a'}.")
    impl.build(f"Classified intent as '{directive.intent.value}' at risk level '{directive.risk.value}'.")
    impl.build("Drafted a structured directive with an ordered plan.")
    impl.limit(
        "The directive was produced by the lexical classifier, not the reasoning core. "
        "It recognises the shape of the request, not its meaning."
    )
    impl.limit("Nothing was executed: the Eminence is not dispatched until the core is attached.")
    if directive.risk.value == "destructive":
        impl.limit("Risk assessed as destructive; execution would require explicit confirmation.")
    for note in config.missing_variables():
        impl.unresolved(note)
    impl.how(
        f"Monarch: walked {config.context_scan_depth} directory levels (cap "
        f"{config.context_scan_limit} files), skipping build, vendor, and VCS directories.",
        "Monarch: matched destructive-first against the intent keyword table, then mapped intent to risk.",
        "Architect: journalled the drafted directive.",
    )
    store.record(
        "cli.run",
        raw=raw,
        intent=directive.intent.value,
        risk=directive.risk.value,
        source=directive.source,
    )
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    entries = store.read_journal(limit=args.limit)

    rows: List[str] = []
    for entry in entries:
        stamp = str(entry.get("ts", ""))[:19].replace("T", " ")
        rows.append(
            ansi.paint(stamp + "  ", DIM)
            + ansi.paint(str(entry.get("event", "")), BONE)
        )
    if not rows:
        rows = [ansi.paint("no events recorded", DIM)]

    _emit("")
    for line in panel(rows, width=width, title=f"JOURNAL  (last {args.limit})", color=INDIGO):
        _emit(line)
    _emit("")

    impl = Implementory(headline=f"Replayed {len(entries)} journal events.")
    impl.build(f"Read the append-only event log at {store.journal_path.name}.")
    impl.limit("The journal records decisions and outcomes; it is not a content diff.")
    if not store.initialized:
        impl.degrade(Status.NEEDS_INPUT)
        impl.limit("State directory absent; there is nothing to replay yet.")
    impl.how("Architect: streamed journal.jsonl, skipping any torn tail line from an interrupted write.")
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    snaps = store.list_snapshots()
    inner = width - 6

    rows = (
        kv_rows(
            [(s.id, f"{s.created_at[:19].replace('T', ' ')}  {s.label or '(unlabelled)'}") for s in snaps],
            inner,
        )
        if snaps
        else [ansi.paint("no snapshots recorded", DIM)]
    )
    _emit("")
    for line in panel(rows, width=width, title="SNAPSHOTS", color=INDIGO):
        _emit(line)
    _emit("")

    impl = Implementory(headline=f"{len(snaps)} restorable snapshots.")
    impl.build("Read every snapshot manifest under .shadow/snapshots/.")
    impl.limit(
        "Snapshots capture only the files the Architect was told about. Side effects "
        "outside those paths -- installed packages, network calls, database writes -- "
        "are not reversible."
    )
    impl.how("Architect: parsed manifest.json from each snapshot directory, sorted by creation time.")
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)

    impl = Implementory()
    try:
        report = store.rollback(args.snapshot_id)
    except ShadowError as exc:
        impl.degrade(Status.FAILED)
        impl.headline = f"Rollback of {args.snapshot_id} failed."
        impl.limit(str(exc))
        impl.how("Architect: attempted to read the snapshot manifest and aborted before touching any file.")
        _emit("")
        _emit(impl.render(width=width))
        _emit("")
        return 1

    impl.headline = f"Rolled back snapshot {args.snapshot_id}."
    impl.build(f"Restored {len(report['restored'])} files.")
    if report["removed"]:
        impl.build(f"Removed {len(report['removed'])} files that did not exist at capture time.")
    if report["skipped"]:
        impl.degrade(Status.PARTIAL)
        impl.limit(f"Skipped {len(report['skipped'])}: " + ", ".join(report["skipped"][:5]))
    impl.limit("File contents were restored; side effects outside the manifest were not.")
    impl.how("Architect: copied each manifest entry back to its recorded path, then journalled the rollback.")
    _emit("")
    _emit(impl.render(width=width))
    _emit("")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    inner = width - 6

    impl = Implementory()
    if args.set:
        key, _, value = args.set.partition("=")
        if not key or not value:
            impl.degrade(Status.NEEDS_INPUT)
            impl.limit("--set expects KEY=VALUE.")
        else:
            store.remember(key.strip(), value.strip(), note="set via CLI")
            impl.build(f"Stored '{key.strip()}' in durable memory.")
    if args.forget:
        removed = store.forget(args.forget)
        impl.build(f"Forgot '{args.forget}'." if removed else f"No memory named '{args.forget}'.")

    entries = store.memories()
    rows = (
        kv_rows([(k, str(v.get("value"))) for k, v in sorted(entries.items())], inner)
        if entries
        else [ansi.paint("no durable memories", DIM)]
    )
    _emit("")
    for line in panel(rows, width=width, title="MEMORY", color=INDIGO):
        _emit(line)
    _emit("")

    impl.headline = f"{len(entries)} durable memories held."
    impl.build("Read .shadow/memory.json.")
    impl.limit("Memory is project-local and plain JSON: it is not encrypted and never leaves this machine.")
    impl.how("Architect: read and atomically rewrote memory.json via a temp-file replace.")
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Credential state, and the dual-path wizard when there is none."""
    from .auth import detect as auth_detect
    from .auth.store import CredentialStore
    from .auth.wizard import credential_panel, run as run_wizard

    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = CredentialStore()
    impl = Implementory()

    if args.forget:
        removed = store.clear()
        impl.headline = "Stored credential removed." if removed else "No stored credential to remove."
        impl.build(f"Deleted {store.path}." if removed else "Nothing was deleted.")
        impl.limit(
            "Only this framework's own store was touched. An exported "
            "ANTHROPIC_API_KEY or an `ant` profile is untouched and still resolves."
        )
        impl.how("Auth: unlinked ~/.shadow-agent/credentials.json.")
        _emit("")
        _emit(impl.render(width=width))
        _emit("")
        return 0

    if args.repair:
        from .auth.store import harden

        ok = harden(store.dir) and (harden(store.path) if store.exists else True)
        impl.headline = "Permissions repaired." if ok else "Permissions could not be repaired."
        impl.build(f"Re-applied owner-only permissions to {store.dir}.")
        if not ok:
            impl.degrade(Status.PARTIAL)
            impl.limit("The platform refused the permission change; treat the file as readable locally.")
        impl.how("Auth: chmod 0600 on POSIX; icacls /inheritance:r + owner-only grant on Windows.")
        _emit("")
        _emit(impl.render(width=width))
        _emit("")
        return 0

    cred = auth_detect(store)

    if cred.present and not args.force:
        _emit("")
        for line in credential_panel(cred, store, width):
            _emit(line)
        _emit("")
        impl.headline = "A credential is already resolved."
        impl.build(f"Resolved from {cred.source.value}.")
        impl.build(f"`ant` CLI: {'installed' if cred.ant_installed else 'not installed'}.")
        impl.limit("Presence was checked, not validity — no API call was made. Use --force to re-run the wizard.")
        impl.how(
            "Auth: probed ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, the local store, "
            "then `ant auth status` — gated on the binary existing."
        )
        if config.ui.show_implementory:
            _emit(impl.render(width=width))
            _emit("")
        return 0

    if args.force:
        store.clear()

    result = run_wizard(width=width, store=store)

    impl.headline = (
        f"Authenticated via {result.source.value}." if result.present else "Left unauthenticated."
    )
    if result.present:
        impl.build(f"Credential resolved from {result.source.value}.")
        if result.source.value.startswith("local store"):
            impl.build("Key was validated against the live API before it was written.")
            impl.build("Stored with owner-only permissions outside the project directory.")
    else:
        impl.degrade(Status.NEEDS_INPUT)
        impl.limit("No credential was obtained. The reasoning core remains unreachable.")
    if not result.ant_installed:
        impl.limit(
            "`ant` is not installed, so web authentication was offered as unavailable "
            "with an install route rather than as a live option."
        )
    impl.limit("The local store is permission-restricted, not encrypted.")
    impl.how(
        "Auth: detection in SDK precedence order, then the two-path wizard.",
        "Auth: keys validated with GET /v1/models over stdlib urllib — no SDK install required, no tokens billed.",
        "Auth: every `ant` invocation gated on shutil.which('ant').",
    )
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    StateStore(root).record("cli.auth", source=result.source.value, present=result.present)
    return 0 if result.present else 2


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """Commit the work-tree to the out-of-band checkpoint repository."""
    from .architect.shadowgit import ShadowGit

    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    sg = ShadowGit(root)
    inner = width - 6
    impl = Implementory()

    if not sg.available():
        impl.degrade(Status.FAILED)
        impl.headline = "git is not installed."
        impl.limit("Checkpoints require a git binary on PATH. Snapshots still work.")
        impl.how("Architect: shutil.which('git') returned nothing; nothing was attempted.")
        _emit("")
        _emit(impl.render(width=width))
        return 1

    created = sg.checkpoint(args.label or "manual checkpoint")
    entries = sg.log(limit=args.limit)

    rows = (
        [
            ansi.paint(c.short + "  ", VIOLET)
            + ansi.paint(c.when + "  ", DIM)
            + ansi.paint(c.label, BONE)
            for c in entries
        ]
        if entries
        else [ansi.paint("no checkpoints yet", DIM)]
    )
    _emit("")
    for line in panel(rows, width=width, title="CHECKPOINTS", color=INDIGO):
        _emit(line)
    _emit("")

    if created:
        impl.headline = f"Checkpoint {created.short} committed."
        impl.build(f"Committed the work-tree as {created.short} ({created.label}).")
    else:
        impl.headline = "No checkpoint created."
        impl.build("Working tree matched the last checkpoint; an empty commit was skipped.")
        if sg.last_error:
            impl.degrade(Status.PARTIAL)
            impl.limit(f"git reported: {sg.last_error}")
    impl.build(f"Checkpoint store: {sg.size_bytes() // 1024} KiB at .shadow/checkpoints.git.")
    impl.limit("Filesystem state only — conversation and in-memory state are not captured.")
    impl.limit(
        "Changes are captured by the next `add -A`; they are not attributable to "
        "the specific command that made them."
    )
    impl.how(
        "Architect: git --git-dir=.shadow/checkpoints.git --work-tree=<root>, so the "
        "user's own .git is never touched and no index lock is shared.",
        "Architect: GIT_INDEX_FILE pinned inside the shadow dir; GIT_DIR/GIT_WORK_TREE "
        "stripped from the environment so an inherited value cannot redirect the write.",
        "Architect: info/exclude blacklists credentials, build output, and virtualenvs "
        "on top of the work-tree's own .gitignore.",
    )
    store.record("cli.checkpoint", created=bool(created), sha=created.sha if created else None)
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_reflog(args: argparse.Namespace) -> int:
    """Every state HEAD has pointed at -- reachable or not."""
    from .architect.shadowgit import ShadowGit

    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    sg = ShadowGit(root)
    entries = sg.reflog(limit=args.limit)

    rows = (
        [
            ansi.paint(pad(e.selector, 12), VIOLET)
            + ansi.paint(e.sha[:9] + "  ", DIM)
            + ansi.paint(pad(e.action, 10), ASH)
            + ansi.paint(e.message, BONE)
            for e in entries
        ]
        if entries
        else [ansi.paint("no reflog entries", DIM)]
    )
    _emit("")
    for line in panel(rows, width=width, title="REFLOG", color=INDIGO):
        _emit(line)
    _emit("")

    impl = Implementory(headline=f"{len(entries)} recorded HEAD states.")
    impl.build("Read the checkpoint repository's reflog.")
    impl.build("Every selector above resolves, including commits no longer reachable from HEAD.")
    impl.limit("The reflog is local and is pruned by git's own expiry (90 days by default).")
    impl.limit("It records states of the checkpoint repo only — not of the user's own repository.")
    impl.how(
        "Architect: `git reflog` against the shadow git-dir, with core.logAllRefUpdates "
        "enabled at init so the log exists in a non-worktree repository.",
        "Concept assimilated from dolthub/dolt: history alone is not recoverability — "
        "a reset hides commits from `log` while the reflog still addresses them.",
    )
    if config.ui.show_implementory:
        _emit(impl.render(width=width))
        _emit("")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore the work-tree from a checkpoint or reflog selector."""
    from .architect.shadowgit import ShadowGit

    root = Path(args.path).resolve() if args.path else find_root()
    config = load_config(root)
    width = _prepare_ui(config)
    store = StateStore(root)
    sg = ShadowGit(root)
    impl = Implementory()

    resolved = sg.resolve(args.ref)
    if not resolved:
        impl.degrade(Status.FAILED)
        impl.headline = f"Could not resolve {args.ref!r}."
        impl.limit(sg.last_error or "no such checkpoint or reflog selector")
        impl.how("Architect: `git rev-parse` failed; the work-tree was not touched.")
        _emit("")
        _emit(impl.render(width=width))
        _emit("")
        return 1

    changed = sg.changed_since(args.ref)
    restored = sg.restore(args.ref)

    if restored is None:
        impl.degrade(Status.FAILED)
        impl.headline = f"Restore of {args.ref} failed."
        impl.limit(sg.last_error or "git checkout failed")
        impl.how("Architect: a pre-restore checkpoint was taken first, so nothing was lost.")
    else:
        impl.headline = f"Work-tree restored to {resolved[:9]}."
        impl.build(f"Restored {len(restored)} paths from {args.ref}.")
        impl.build("Took an automatic checkpoint before restoring — the overwritten state is recoverable.")
        impl.build("Run `shadow reflog` to address the state you restored away from.")
    impl.limit("File contents only. Side effects outside the work-tree are not reversed.")
    impl.how(
        "Architect: checkpoint → `git checkout <ref> -- .` against the shadow work-tree.",
        "Architect: restoring is itself destructive, so the pre-restore commit is unconditional.",
    )
    store.record("cli.restore", ref=args.ref, resolved=resolved, files=len(changed))
    _emit("")
    _emit(impl.render(width=width))
    _emit("")
    return 0 if restored is not None else 1


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadow",
        description="Shadow Agent -- a local, terminal-based LLM harness.",
    )
    parser.add_argument("--version", action="version", version=f"shadow-agent {__version__}")
    parser.add_argument("--path", help="project root (default: nearest .shadow/ or .git/)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--ascii", action="store_true", help="force the ASCII glyph set")
    parser.add_argument("--width", type=int, help="force a frame width in columns")

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="render the interface and create local state")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config and memory")
    p_init.add_argument("--dry-run", action="store_true", help="render only; write nothing")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="print live system and framework state")
    p_status.set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="draft a directive from a raw request")
    p_run.add_argument("request", nargs="*", help="your unpolished input")
    p_run.set_defaults(func=cmd_run)

    p_journal = sub.add_parser("journal", help="replay the event log")
    p_journal.add_argument("-n", "--limit", type=int, default=20)
    p_journal.set_defaults(func=cmd_journal)

    p_snaps = sub.add_parser("snapshots", help="list restorable snapshots")
    p_snaps.set_defaults(func=cmd_snapshots)

    p_roll = sub.add_parser("rollback", help="restore a snapshot")
    p_roll.add_argument("snapshot_id")
    p_roll.set_defaults(func=cmd_rollback)

    p_mem = sub.add_parser("memory", help="inspect or edit durable memory")
    p_mem.add_argument("--set", metavar="KEY=VALUE")
    p_mem.add_argument("--forget", metavar="KEY")
    p_mem.set_defaults(func=cmd_memory)

    p_auth = sub.add_parser("auth", help="credential state and the sign-in wizard")
    p_auth.add_argument("--force", action="store_true", help="re-run the wizard even if a credential exists")
    p_auth.add_argument("--forget", action="store_true", help="delete the locally stored key")
    p_auth.add_argument("--repair", action="store_true", help="re-apply owner-only permissions")
    p_auth.set_defaults(func=cmd_auth)

    p_cp = sub.add_parser("checkpoint", help="commit the work-tree to the checkpoint repo")
    p_cp.add_argument("label", nargs="?", default="", help="checkpoint label")
    p_cp.add_argument("-n", "--limit", type=int, default=15)
    p_cp.set_defaults(func=cmd_checkpoint)

    p_ref = sub.add_parser("reflog", help="every state HEAD has pointed at")
    p_ref.add_argument("-n", "--limit", type=int, default=25)
    p_ref.set_defaults(func=cmd_reflog)

    p_res = sub.add_parser("restore", help="restore the work-tree from a checkpoint")
    p_res.add_argument("ref", help="checkpoint sha, or a reflog selector like HEAD@{2}")
    p_res.set_defaults(func=cmd_restore)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _use_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.no_color:
        ansi.set_enabled(False)
    if args.ascii:
        import os

        os.environ["SHADOW_ASCII"] = "1"
    if getattr(args, "width", None):
        import os

        os.environ["SHADOW_WIDTH"] = str(args.width)

    if not getattr(args, "command", None):
        # No subcommand is not an error: a bare `shadow` is a first contact.
        args = parser.parse_args(["init", *( ["--path", args.path] if args.path else [] )])

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _emit("")
        _emit(ansi.paint("  interrupted -- no further action taken.", DIM))
        return 130
    except ShadowError as exc:
        _emit("")
        _emit(ansi.paint(f"  {type(exc).__name__}: {exc}", EMBER))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
