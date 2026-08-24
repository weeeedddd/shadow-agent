"""The Information Claw -- autonomous documentation retrieval.

*Concept assimilated from* ``aiming-lab/AutoResearchClaw`` (MIT): research is a
**pipeline stage**, not a tool call. The Monarch gathers and synthesises before
the Eminence is handed anything, so execution starts from read sources instead
of from recall.

Built on ``urllib`` and ``html.parser`` from the standard library. The framework
has no runtime dependencies and research is not going to be the thing that
introduces one.

Three limits, stated up front rather than discovered later
----------------------------------------------------------
**No search engine.** There is no crawler and no index here. The Claw fetches
URLs it is *given*, or that a ``search_fn`` supplies. Without one it cannot
answer "find me documentation about X" -- it can only read what it is pointed
at. Wire a search backend into ``search_fn`` and the rest of the pipeline works
unchanged.

**No JavaScript.** ``urllib`` retrieves the HTML the server sends. A page that
renders its content client-side returns an empty shell, and the Claw reports a
thin extraction rather than pretending it read something.

**It is polite, and that is a real constraint.** ``robots.txt`` is honoured,
requests are rate-limited, and the user agent identifies itself truthfully. A
scraper that ignores these gets the whole framework blocked, which is a worse
outcome than a slow one.
"""

from __future__ import annotations

import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence

USER_AGENT = "shadow-agent/0.2 (+https://github.com/weeeedddd/shadow-agent)"
FETCH_TIMEOUT = 20.0
MAX_BYTES = 2_000_000
MIN_INTERVAL = 1.0          # per-host politeness floor
MAX_SOURCES = 6

# Tags whose contents are markup machinery, never prose.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head", "nav", "footer", "form"})
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4", "pre"})


class _TextExtractor(HTMLParser):
    """Pull readable prose out of HTML, preserving block structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        # The title check must precede the skip check: <title> lives inside
        # <head>, which is a skip tag, so testing skip first discards the one
        # piece of metadata worth keeping from that subtree.
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped + " ")

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


@dataclass
class Source:
    """One retrieved document."""

    url: str
    title: str = ""
    text: str = ""
    status: int = 0
    error: str = ""
    bytes_read: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    @property
    def thin(self) -> bool:
        """True when retrieval succeeded but yielded almost no prose.

        Usually a client-rendered page. Worth surfacing: a source that looks
        fetched but says nothing is more misleading than one that plainly
        failed.
        """
        return not self.error and len(self.text) < 400


@dataclass
class Findings:
    """What the Claw brought back."""

    query: str
    sources: List[Source] = field(default_factory=list)
    excerpts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def usable(self) -> List[Source]:
        return [s for s in self.sources if s.ok and not s.thin]

    def brief(self) -> str:
        good = len(self.usable)
        return f"{good}/{len(self.sources)} sources usable"

    def synthesis(self, limit: int = 2400) -> str:
        """The block handed to the Eminence, with provenance attached.

        Every excerpt carries its URL. Research that cannot be traced back to
        a source is indistinguishable from a guess.
        """
        if not self.excerpts:
            return ""
        out: List[str] = [f"Research for: {self.query}", ""]
        budget = limit
        for source, excerpt in zip(self.usable, self.excerpts):
            if budget <= 0:
                break
            chunk = excerpt[:budget]
            out.append(f"— {source.title or source.url}")
            out.append(f"  {source.url}")
            out.append(f"  {chunk}")
            out.append("")
            budget -= len(chunk)
        return "\n".join(out)


class InformationClaw:
    """Fetches, extracts, and synthesises external documentation."""

    def __init__(
        self,
        timeout: float = FETCH_TIMEOUT,
        max_sources: int = MAX_SOURCES,
        respect_robots: bool = True,
        search_fn: Optional[Callable[[str, int], Sequence[str]]] = None,
    ) -> None:
        self.timeout = timeout
        self.max_sources = max_sources
        self.respect_robots = respect_robots
        self.search_fn = search_fn
        self._last_hit: Dict[str, float] = {}
        self._robots: Dict[str, urllib.robotparser.RobotFileParser] = {}

    # --- politeness ----------------------------------------------------------

    def _throttle(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_hit[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        """Check robots.txt. A robots file we cannot read is treated as permissive."""
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(host)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{host}/robots.txt")
            try:
                parser.read()
            except Exception:
                # Unreachable robots.txt is not a prohibition. Treat as allowed,
                # since the throttle still bounds our impact either way.
                self._robots[host] = parser
                return True
            self._robots[host] = parser
        try:
            return parser.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    # --- retrieval -----------------------------------------------------------

    def fetch(self, url: str) -> Source:
        """Retrieve one URL. Never raises."""
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            return Source(url=url, error=f"unsupported scheme: {parts.scheme or 'none'}")
        if not self._allowed(url):
            return Source(url=url, error="disallowed by robots.txt")

        self._throttle(parts.netloc)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(MAX_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
                content_type = (response.headers.get_content_type() or "").lower()
        except urllib.error.HTTPError as exc:
            return Source(url=url, status=exc.code, error=f"HTTP {exc.code}")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return Source(url=url, error=f"unreachable: {getattr(exc, 'reason', exc)}")

        body = raw.decode(charset, errors="replace")
        if "html" in content_type:
            parser = _TextExtractor()
            try:
                parser.feed(body)
            except Exception:
                return Source(url=url, status=status, error="malformed HTML", bytes_read=len(raw))
            return Source(url, parser.title, parser.text(), status, bytes_read=len(raw))

        return Source(url, url.rsplit("/", 1)[-1], body.strip(), status, bytes_read=len(raw))

    # --- the pipeline --------------------------------------------------------

    def research(self, query: str, urls: Optional[Sequence[str]] = None) -> Findings:
        """Gather sources for ``query`` and synthesise the relevant parts."""
        findings = Findings(query=query)

        targets = list(urls or [])
        if not targets and self.search_fn:
            try:
                targets = list(self.search_fn(query, self.max_sources))
            except Exception as exc:
                findings.notes.append(f"search backend failed: {type(exc).__name__}")
        if not targets:
            findings.notes.append(
                "No URLs supplied and no search backend configured — the Claw can "
                "read what it is pointed at, but cannot find it."
            )
            return findings

        for url in targets[: self.max_sources]:
            source = self.fetch(url)
            findings.sources.append(source)
            if source.error:
                findings.notes.append(f"{url}: {source.error}")
            elif source.thin:
                findings.notes.append(
                    f"{url}: only {len(source.text)} chars extracted — likely client-rendered"
                )

        for source in findings.usable:
            findings.excerpts.append(self.relevant_excerpt(query, source.text))

        if not findings.usable:
            findings.notes.append("No usable source text was retrieved.")
        return findings

    @staticmethod
    def relevant_excerpt(query: str, text: str, window: int = 900) -> str:
        """The passage densest in query terms.

        A fixed head-of-document slice is the obvious approach and the wrong
        one: the answer is rarely in the navigation. Scoring paragraphs by term
        density finds the part of the page that is actually about the question.
        """
        from .recall import tokenize

        terms = set(tokenize(query))
        if not terms:
            return text[:window]

        paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 60]
        if not paragraphs:
            return text[:window]

        scored = []
        for index, paragraph in enumerate(paragraphs):
            tokens = tokenize(paragraph)
            if not tokens:
                continue
            hits = sum(1 for t in tokens if t in terms)
            scored.append((hits / (len(tokens) ** 0.5), index, paragraph))

        if not scored:
            return text[:window]

        scored.sort(reverse=True)
        # Keep the best paragraph plus its neighbours: prose has context, and a
        # sentence lifted out of its surroundings often inverts its meaning.
        best = scored[0][1]
        chunk = "\n".join(paragraphs[max(0, best - 1) : best + 2])
        return chunk[:window]
