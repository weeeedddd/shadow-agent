#!/usr/bin/env python3
"""Shadow Agent -- boot.

    python main.py                  boot, authenticate if needed, then idle
    python main.py "your request"   boot and run one request through the loop
    python main.py --no-auth        boot without touching credentials

This is the front door. It does four things in order, and each one is allowed
to fail without taking the next one down:

    1. Render the Shadow Garden Interface from live system state.
    2. Resolve credentials -- running the dual-path wizard only when there are
       none and there is a terminal to ask on.
    3. Initialise the pipeline: Monarch, Eminence, Architect, permission wall.
    4. Hand control to the core loop.

Step 2 never blocks step 3. The framework is useful without a reasoning core --
checkpoints, restore, the journal, the skill registry, and every guardrail run
offline -- so an unauthenticated boot proceeds with the core marked unreachable
rather than exiting.

For the installed entry point (``shadow``), see ``shadow_agent/cli.py``. This
script is the single-file boot path for anyone who cloned the repo and wants to
run it without installing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shadow_agent import __version__
from shadow_agent.architect.shadowgit import ShadowGit
from shadow_agent.architect.skills import SkillForge
from shadow_agent.architect.state import StateStore, find_root
from shadow_agent.auth import detect as detect_credentials
from shadow_agent.auth.store import CredentialStore
from shadow_agent.config import load_config
from shadow_agent.core import context
from shadow_agent.eminence.coral import PermissionWall
from shadow_agent.loop.core import CoreLoop, Phase
from shadow_agent.ui import ansi, onboarding
from shadow_agent.ui.implementory import Implementory, Status
from shadow_agent.ui.render import kv_rows, pad, panel, resolve_width, wrap
from shadow_agent.ui.theme import ASH, BLOOD, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs

PHASE_GLYPH = {
    Phase.ANALYZE: "M",
    Phase.RESEARCH: "M",
    Phase.PLAN: "M",
    Phase.GATE: "!",
    Phase.EXECUTE: "E",
    Phase.RECORD: "A",
    Phase.FORGE: "A",
    Phase.DONE: "·",
}


def emit(text: str = "") -> None:
    """Print, surviving a console that cannot encode the glyph set."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def use_utf8() -> None:
    """Ask for UTF-8 on stdout/stderr before anything is rendered."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


# --- boot stages --------------------------------------------------------------


def stage_interface(state, width: int) -> None:
    emit()
    emit(onboarding.render(state, width=width))
    emit()


def stage_auth(width: int, skip: bool):
    """Resolve credentials. Never exits the process."""
    store = CredentialStore()
    credential = detect_credentials(store)

    if skip or credential.present:
        return credential

    if not PermissionWall.interactive():
        # Headless and unauthenticated. Say what to do and carry on -- the
        # offline half of the framework still works.
        emit()
        for line in panel(
            [
                ansi.paint("No credential, and no terminal to ask on.", BOLD + BONE),
                "",
                ansi.paint("Booting with the reasoning core unreachable. Checkpoints,", DIM),
                ansi.paint("restore, the journal, skills, and every guardrail still run.", DIM),
                "",
                ansi.paint("  export ANTHROPIC_API_KEY=sk-ant-…", VIOLET),
                ansi.paint("  ant auth login", VIOLET),
                ansi.paint("  shadow auth        (from a real terminal)", VIOLET),
            ],
            width=width,
            title="UNAUTHENTICATED",
            color=EMBER,
        ):
            emit(line)
        return credential

    from shadow_agent.auth.wizard import run as run_wizard

    return run_wizard(width=width, store=store)


def stage_pipeline(root: Path, config, width: int):
    """Initialise the three tiers and the permission wall."""
    store = StateStore(root)
    if not store.initialized:
        store.initialize(config=config)

    shadowgit = ShadowGit(root)
    shadowgit.initialize()

    forge = SkillForge(store.dir)
    wall = PermissionWall.from_env(width=width)
    loop = CoreLoop(root, config=config, wall=wall)
    return store, shadowgit, forge, wall, loop


def render_pipeline(state, store, shadowgit, forge, wall, credential, config, width: int) -> None:
    g = glyphs()
    inner = width - 6
    skills = forge.all()
    checkpoints = shadowgit.log(limit=1000)

    rows = kv_rows(
        [
            ("MONARCH", f"ready · scan depth {config.context_scan_depth}"),
            ("EMINENCE", f"ready · timeout {config.execution.timeout_seconds:.0f}s"),
            ("ARCHITECT", f"ready · {len(checkpoints)} checkpoints · {len(skills)} skills"),
            ("WALL", f"{wall.headless.value} when headless" + (" · paranoid" if wall.paranoid else "")),
            ("CORE", credential.source.value if credential.present else "unreachable"),
            ("MODEL", config.llm.model),
        ],
        inner,
    )
    lines = list(rows)
    lines.append("")
    if credential.present:
        lines.append(ansi.paint(pad("STATUS", 12), ASH) + ansi.paint("PIPELINE ARMED", BOLD + JADE))
    else:
        lines.append(ansi.paint(pad("STATUS", 12), ASH) + ansi.paint("PIPELINE ARMED — CORE OFFLINE", BOLD + EMBER))
        for segment in wrap(
            "Analysis, execution, checkpoints, and guardrails all run. Planning "
            "from natural language does not, until the core is reachable.",
            inner - 3,
        ):
            lines.append("   " + ansi.paint(segment, DIM))

    emit()
    for line in panel(lines, width=width, title="PIPELINE", color=INDIGO):
        emit(line)
    emit()


def stage_run(loop: CoreLoop, request: str, state, width: int):
    """Drive one request through the loop, rendering events as they arrive."""
    emit()
    for line in panel(
        [ansi.paint(segment, BONE) for segment in wrap(request, width - 6)],
        width=width,
        title="DIRECTIVE",
        color=INDIGO,
    ):
        emit(line)
    emit()

    for event in loop.stream(request, state):
        marker = PHASE_GLYPH.get(event.phase, "·")
        colour = BONE if event.ok else EMBER
        emit(
            ansi.paint(f"  {marker} ", VIOLET if event.ok else EMBER)
            + ansi.paint(pad(event.phase.value, 9), DIM)
            + ansi.paint(event.message, colour)
        )
        if event.detail:
            for segment in wrap(event.detail, width - 18):
                emit("      " + ansi.paint(segment, DIM))
    emit()
    return loop.result


# --- entry point --------------------------------------------------------------


def main(argv=None) -> int:
    use_utf8()

    parser = argparse.ArgumentParser(prog="main.py", description="Shadow Agent — boot.")
    parser.add_argument("request", nargs="*", help="a request to run through the loop")
    parser.add_argument("--no-auth", action="store_true", help="skip credential resolution")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--width", type=int)
    parser.add_argument("--version", action="version", version=f"shadow-agent {__version__}")
    args = parser.parse_args(argv)

    if args.no_color:
        ansi.set_enabled(False)
    width = resolve_width(args.width)

    root = find_root()
    config = load_config(root)
    state = context.collect(cwd=root)

    # 1 — the interface
    stage_interface(state, width)

    # 2 — credentials (never fatal)
    credential = stage_auth(width, skip=args.no_auth)

    # 3 — the pipeline
    store, shadowgit, forge, wall, loop = stage_pipeline(root, config, width)
    state = context.collect(cwd=root, state_dir=store.dir)
    render_pipeline(state, store, shadowgit, forge, wall, credential, config, width)

    # 4 — the loop
    request = " ".join(args.request).strip()
    if not request:
        emit(ansi.paint("  Provide your unpolished input. The Monarch will refine it.", BOLD + BONE))
        emit(ansi.paint('  python main.py "<your raw idea>"', VIOLET))
        emit()
        store.record("boot", authenticated=credential.present, ran=False)
        return 0

    result = stage_run(loop, request, state, width)

    # Forge a skill from a run that actually worked.
    forged = None
    if result and result.succeeded:
        commands = [step.command for step in result.steps]
        forged = forge.forge(request, commands, intent=result.directive.intent.value if result.directive else "")

    impl = Implementory()
    impl.headline = (
        f"{result.executed} step(s) executed in {result.duration:.2f}s."
        if result and result.executed
        else "Nothing was executed."
    )
    if result:
        if result.executed:
            impl.build(f"Executed {result.executed} step(s) through the permission wall.")
        if result.checkpoint:
            impl.build(f"Committed checkpoint {result.checkpoint} after the run.")
        if forged:
            impl.build(f"Forged skill '{forged.name}' (used {forged.uses}×, confidence {forged.confidence:.0%}).")
        if result.refused:
            impl.degrade(Status.PARTIAL)
            impl.limit(f"{result.refused} step(s) refused at the wall.")
        if result.failed:
            impl.degrade(Status.PARTIAL)
            impl.limit(f"{result.failed} step(s) failed.")
        if result.aborted:
            impl.degrade(Status.NEEDS_INPUT)
            impl.limit(f"Run aborted: {result.abort_reason}")
        if not result.steps:
            impl.degrade(Status.NEEDS_INPUT)
            impl.limit(
                "The heuristic planner produced no runnable step. Turning prose "
                "into commands needs the reasoning core."
            )
        for nudge in result.nudges:
            impl.limit(f"Loop-break nudge fired: {nudge}")
    if not credential.present:
        impl.unresolved("No credential resolved — the reasoning core is unreachable.")

    impl.how(
        "Monarch: scanned the working directory, classified intent, assessed risk.",
        "CORAL wall: every step classified and gated before reaching the OS.",
        "Eminence: executed approved steps with timeout and output caps.",
        "Architect: checkpointed before and after, journalled the run, ran periodic gc.",
    )
    if config.ui.show_implementory:
        emit(impl.render(width=width))
        emit()

    store.record("boot", authenticated=credential.present, ran=True, executed=result.executed if result else 0)
    return 0 if (result and not result.aborted) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        emit()
        emit("  interrupted — no further action taken.")
        raise SystemExit(130)
