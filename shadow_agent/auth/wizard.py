"""The Monarch's authentication wizard.

A public tool meets machines its author never saw. This wizard assumes nothing:
not that ``ant`` is installed, not that a browser exists, not that stdin is a
terminal, not that the network is reachable. Every one of those is a supported
configuration with its own path through the flow, and none of them is a crash.

Two paths, exactly as specified:

    [1] WEB AUTHENTICATION   hand off to `ant auth login`, then detect the
                             profile the CLI writes. Offered only when the
                             binary exists; otherwise shown as unavailable
                             with the install route, never as a dead option.

    [2] DIRECT API KEY       paste a key, validated live against the API
                             before anything touches disk. An invalid key is
                             never stored.

Non-interactive callers (CI, a pipe, a redirected stdin) get the same
information printed as instructions and a clean exit. Prompting into a closed
stdin is how a wizard becomes an infinite loop.
"""

from __future__ import annotations

import getpass
import subprocess
import sys
import time
from typing import List, Optional

from ..ui import ansi
from ..ui.render import bullets, kv_rows, pad, panel, resolve_width, rule, wrap
from ..ui.theme import ASH, BLOOD, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs
from .detect import Credential, Source, ant_available, ant_status, detect
from .store import CredentialStore
from .validate import Verdict, validate_ant_profile, validate_key

ANT_INSTALL_URL = "https://docs.claude.com/en/docs/agents-and-tools/ant-cli"
CONSOLE_KEYS_URL = "https://console.anthropic.com/settings/keys"

LOGIN_POLL_INTERVAL = 2.0
LOGIN_POLL_TIMEOUT = 300.0


def _emit(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _emit_block(lines: List[str]) -> None:
    for line in lines:
        _emit(line)


def interactive() -> bool:
    """True when both ends of the terminal are a real TTY."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


# --- panels -------------------------------------------------------------------


def credential_panel(cred: Credential, store: CredentialStore, width: int) -> List[str]:
    """Render the current credential state. The secret is never shown whole."""
    g = glyphs()
    inner = width - 6
    rows = [
        ("SOURCE", cred.source.value),
        ("DETAIL", cred.detail or "—"),
        ("SECRET", cred.masked),
        ("ant CLI", "installed" if cred.ant_installed else "not installed"),
    ]
    if cred.ant_profile:
        rows.append(("PROFILE", cred.ant_profile))
    if store.exists:
        rows.append(("LOCAL STORE", str(store.path)))

    lines = kv_rows(rows, inner)
    lines.append("")
    if cred.present:
        lines.append(
            ansi.paint(pad("STATUS", 12), ASH) + ansi.paint("AUTHENTICATED", BOLD + JADE)
        )
    else:
        lines.append(
            ansi.paint(pad("STATUS", 12), ASH) + ansi.paint("NO CREDENTIAL", BOLD + EMBER)
        )
    if store.world_readable():
        for segment in wrap(
            "the local store is readable beyond its owner; re-run `shadow auth --repair`",
            inner - 3,
        ):
            lines.append("   " + ansi.paint(segment, BLOOD))
    return panel(lines, width=width, title="CREDENTIALS", color=INDIGO)


def _choice_panel(ant_ok: bool, width: int) -> List[str]:
    g = glyphs()
    inner = width - 6
    lines: List[str] = []

    # Path 1 -- shown either way, but never as a live option when it cannot run.
    if ant_ok:
        head = ansi.paint("[1] ", VIOLET) + ansi.paint("WEB AUTHENTICATION", BOLD + BONE)
        body = "Hand off to `ant auth login`. Authenticate in your browser; the profile is detected when you return."
        tone = DIM
    else:
        head = (
            ansi.paint("[1] ", DIM)
            + ansi.paint("WEB AUTHENTICATION", BOLD + DIM)
            + ansi.paint("   UNAVAILABLE", EMBER)
        )
        body = "Requires the `ant` CLI, which is not on this machine's PATH. Choose it anyway to see the install route."
        tone = EMBER

    lines.append(head)
    for segment in wrap(body, inner - 4):
        lines.append("    " + ansi.paint(segment, tone))
    lines.append("")
    lines.append(ansi.paint("[2] ", VIOLET) + ansi.paint("DIRECT API KEY", BOLD + BONE))
    for segment in wrap(
        "Paste a key from the Anthropic Console. Validated against the live API "
        "before it is written; an invalid key is never stored.",
        inner - 4,
    ):
        lines.append("    " + ansi.paint(segment, DIM))
    lines.append("")
    lines.append(ansi.paint("[q] ", DIM) + ansi.paint("Leave unauthenticated", DIM))

    return panel(lines, width=width, title="THE MONARCH REQUIRES A KEY", color=INDIGO)


def _note(text: str, width: int, color: str = DIM) -> None:
    for segment in wrap(text, width - 4):
        _emit("  " + ansi.paint(segment, color))


# --- path 1: web authentication -----------------------------------------------

def _install_route(width: int) -> None:
    g = glyphs()
    _emit()
    _emit_block(
        panel(
            [
                ansi.paint("The `ant` CLI is not installed.", BOLD + BONE),
                "",
                ansi.paint("Web authentication is delegated to that binary; without it, there", DIM),
                ansi.paint("is nothing to hand the browser flow to. Two ways forward:", DIM),
                "",
                ansi.paint("  A.  Install the CLI, then re-run `shadow auth`.", BONE),
                ansi.paint(f"      {ANT_INSTALL_URL}", VIOLET),
                "",
                ansi.paint("  B.  Use path [2] and paste an API key instead.", BONE),
                ansi.paint(f"      {CONSOLE_KEYS_URL}", VIOLET),
                "",
                ansi.paint("Nothing has been changed on this machine.", DIM),
            ],
            width=width,
            title="PATH UNAVAILABLE",
            color=INDIGO,
        )
    )


def web_authenticate(width: int) -> Optional[Credential]:
    """Run ``ant auth login``, then wait for the profile to appear.

    stdio is inherited rather than captured -- the CLI prints a URL and may
    prompt, and swallowing that leaves the user staring at a frozen terminal
    while a browser waits for input they cannot see.
    """
    if not ant_available():
        _install_route(width)
        return None

    _emit()
    _note("Handing off to `ant auth login`. Complete the flow in your browser.", width, ASH)
    _emit()

    try:
        subprocess.run(["ant", "auth", "login"], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _emit()
        _note(f"`ant auth login` could not be launched: {exc}", width, BLOOD)
        _note("Falling back is safe — path [2] does not need the CLI.", width, DIM)
        return None

    _emit()
    _note("Waiting for the profile to be written…", width, ASH)

    deadline = time.monotonic() + LOGIN_POLL_TIMEOUT
    while time.monotonic() < deadline:
        logged_in, profile = ant_status()
        if logged_in:
            check = validate_ant_profile()
            _emit()
            if check.ok:
                _note(f"Profile active: {profile}. Verified against the API.", width, JADE)
                return detect()
            _note(f"Profile found but not usable: {check.message}", width, EMBER)
            return detect()
        time.sleep(LOGIN_POLL_INTERVAL)

    _emit()
    _note("Timed out waiting for a profile. Nothing was stored.", width, EMBER)
    _note("If you completed the login, re-run `shadow auth` to pick it up.", width, DIM)
    return None


# --- path 2: direct api key ---------------------------------------------------


def direct_key(width: int, store: CredentialStore, attempts: int = 3) -> Optional[Credential]:
    """Prompt for a key, validate it live, store it only if it works."""
    _emit()
    _note(f"Create or copy a key at {CONSOLE_KEYS_URL}", width, ASH)
    _note("Input is hidden. Paste and press Enter.", width, DIM)
    _emit()

    for attempt in range(1, attempts + 1):
        try:
            entered = getpass.getpass("  key ▸ ")
        except (EOFError, KeyboardInterrupt):
            _emit()
            _note("Cancelled. Nothing was stored.", width, DIM)
            return None

        entered = (entered or "").strip()
        if not entered:
            _note("Nothing entered.", width, EMBER)
            continue

        _note("Validating against the live API…", width, DIM)
        result = validate_key(entered)

        if result.ok:
            path = store.write_key(entered, note="added by shadow auth")
            _emit()
            lines = [ansi.paint("Key accepted and stored.", BOLD + JADE), ""]
            lines.extend(
                kv_rows(
                    [
                        ("LOCATION", str(path)),
                        ("PERMISSIONS", "owner only" if store.hardened else "COULD NOT RESTRICT"),
                        ("MODELS SEEN", ", ".join(result.models[:2]) if result.models else "—"),
                    ],
                    width - 6,
                )
            )
            lines.append("")
            for segment in wrap(
                "This file is permission-restricted, not encrypted. Anything running "
                "as you can read it. Remove it any time with `shadow auth --forget`.",
                width - 6,
            ):
                lines.append(ansi.paint(segment, DIM))
            if store.hardened is False:
                lines.append("")
                for segment in wrap(
                    "Permissions could not be tightened on this platform. Treat the "
                    "file as readable by other local accounts.",
                    width - 6,
                ):
                    lines.append(ansi.paint(segment, BLOOD))
            _emit_block(panel(lines, width=width, title="STORED", color=INDIGO))
            return detect(store)

        colour = EMBER if result.verdict is Verdict.MALFORMED else BLOOD
        _note(f"{result.verdict.value}: {result.message}", width, colour)
        if result.verdict is Verdict.UNREACHABLE:
            _note("The key was not stored — an unreachable API cannot confirm it.", width, DIM)
            return None
        if attempt < attempts:
            _note(f"attempt {attempt} of {attempts}", width, DIM)

    _emit()
    _note("No valid key provided. Nothing was stored.", width, EMBER)
    return None


# --- entry point --------------------------------------------------------------


def run(width: Optional[int] = None, store: Optional[CredentialStore] = None) -> Credential:
    """Run the wizard. Returns the resolved credential state, authenticated or not."""
    width = resolve_width(width)
    store = store or CredentialStore()
    cred = detect(store)

    _emit()
    _emit_block(credential_panel(cred, store, width))

    if cred.present:
        _emit()
        _note("A credential is already resolved. Nothing to do.", width, DIM)
        _note("Replace it with `shadow auth --force`, or remove it with `--forget`.", width, DIM)
        return cred

    if not interactive():
        _emit()
        _emit_block(
            panel(
                [
                    ansi.paint("No credential, and no terminal to ask on.", BOLD + BONE),
                    "",
                    ansi.paint("stdin is not a TTY — this is a pipe, a CI job, or a", DIM),
                    ansi.paint("redirect. Prompting here would block forever, so the", DIM),
                    ansi.paint("wizard is declining to start. Choose one:", DIM),
                    "",
                    ansi.paint("  export ANTHROPIC_API_KEY=sk-ant-…", VIOLET),
                    ansi.paint("  ant auth login          (installs a profile the SDK reads)", VIOLET),
                    ansi.paint("  shadow auth             (run it from a real terminal)", VIOLET),
                ],
                width=width,
                title="NON-INTERACTIVE",
                color=INDIGO,
            )
        )
        return cred

    ant_ok = ant_available()
    _emit()
    _emit_block(_choice_panel(ant_ok, width))

    while True:
        _emit()
        try:
            choice = input("  select ▸ ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _emit()
            _note("Cancelled.", width, DIM)
            return cred

        if choice in ("q", "quit", "exit", ""):
            _emit()
            _note("Leaving unauthenticated. The framework still runs; the", width, DIM)
            _note("reasoning core simply stays unreachable.", width, DIM)
            return cred

        if choice in ("1", "web"):
            result = web_authenticate(width)
            if result and result.present:
                return result
            _note("Returning to the choices.", width, DIM)
            _emit()
            _emit_block(_choice_panel(ant_available(), width))
            continue

        if choice in ("2", "key", "api"):
            result = direct_key(width, store)
            if result and result.present:
                return result
            _note("Returning to the choices.", width, DIM)
            continue

        _note("Enter 1, 2, or q.", width, EMBER)
