"""The Architect -- persistence, journalling, and rollback.

Everything the framework remembers lives under ``.shadow/`` at the project
root:

    .shadow/
      config.json      resolved configuration
      memory.json      durable user preferences and project facts
      journal.jsonl    append-only event log; the audit trail
      sessions/        one JSON record per run
      snapshots/       pre-mutation file copies, each with a manifest

Two principles govern this module.

**Append, never overwrite.** The journal is the record of what happened. It is
only ever extended, so an interrupted run leaves a truncated tail rather than a
corrupted history.

**Snapshot before you touch.** Any file the Eminence is about to modify is
copied first. Rollback is then a file restore against a recorded manifest --
not an attempt to reason backwards about what a command did.

Scope note: snapshots are content copies of files the Architect was explicitly
told about. They are not a content-addressed object store, and they do not
capture side effects outside those paths.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import Config, STATE_DIRNAME, config_path_for, state_dir_for
from ..core.errors import ArchitectError

GITIGNORE_BODY = """\
# Shadow Agent runtime state -- machine-local, never committed.
sessions/
snapshots/
journal.jsonl
memory.json
*.tmp
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SnapshotEntry:
    """One file captured inside a snapshot."""

    path: str          # original path, relative to root where possible
    stored: str        # filename inside the snapshot directory
    existed: bool      # False means "this file did not exist" -> rollback deletes it
    size: int = 0


@dataclass
class Snapshot:
    """A restorable set of file copies."""

    id: str
    label: str
    created_at: str
    entries: List[SnapshotEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "entries": [vars(e) for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        return cls(
            id=data["id"],
            label=data.get("label", ""),
            created_at=data.get("created_at", ""),
            entries=[SnapshotEntry(**e) for e in data.get("entries", [])],
        )


class StateStore:
    """Owns everything under ``.shadow/`` for one project root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.dir = state_dir_for(self.root)
        self.config_path = config_path_for(self.root)
        self.journal_path = self.dir / "journal.jsonl"
        self.memory_path = self.dir / "memory.json"
        self.sessions_dir = self.dir / "sessions"
        self.snapshots_dir = self.dir / "snapshots"
        self.session_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # --- lifecycle -----------------------------------------------------------

    @property
    def initialized(self) -> bool:
        return self.dir.is_dir() and self.config_path.is_file()

    def initialize(self, config: Optional[Config] = None, force: bool = False) -> Dict[str, Any]:
        """Create the state directory. Idempotent unless ``force`` is set.

        Returns a report describing what was created versus what already
        existed, so the caller can tell the user the truth about the run.
        """
        created: List[str] = []
        existed: List[str] = []

        for directory in (self.dir, self.sessions_dir, self.snapshots_dir):
            if directory.is_dir():
                existed.append(str(directory.relative_to(self.root)))
            else:
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise ArchitectError(f"could not create {directory}: {exc}") from exc
                created.append(str(directory.relative_to(self.root)))

        gitignore = self.dir / ".gitignore"
        if force or not gitignore.is_file():
            gitignore.write_text(GITIGNORE_BODY, encoding="utf-8")
            created.append(str(gitignore.relative_to(self.root)))
        else:
            existed.append(str(gitignore.relative_to(self.root)))

        if force or not self.config_path.is_file():
            (config or Config()).save(self.config_path)
            created.append(str(self.config_path.relative_to(self.root)))
        else:
            existed.append(str(self.config_path.relative_to(self.root)))

        if force or not self.memory_path.is_file():
            self._write_json(self.memory_path, {"version": 1, "entries": {}})
            created.append(str(self.memory_path.relative_to(self.root)))
        else:
            existed.append(str(self.memory_path.relative_to(self.root)))

        if not self.journal_path.is_file():
            self.journal_path.touch()
            created.append(str(self.journal_path.relative_to(self.root)))
        else:
            existed.append(str(self.journal_path.relative_to(self.root)))

        report = {"created": created, "existed": existed, "root": str(self.root)}
        self.record("architect.initialize", **report)
        return report

    # --- journal -------------------------------------------------------------

    def record(self, event: str, **payload: Any) -> None:
        """Append one event to the journal. Never raises; never blocks work.

        A failure to journal is logged into the payload of nothing -- it is
        swallowed. Losing an audit line must not abort a user's task.
        """
        if not self.dir.is_dir():
            return
        entry = {
            "ts": _utc_now(),
            "session": self.session_id,
            "event": event,
            **payload,
        }
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    def read_journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent ``limit`` journal entries, oldest first."""
        if not self.journal_path.is_file():
            return []
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn tail from an interrupted write; skip it
        return out

    # --- memory --------------------------------------------------------------

    def remember(self, key: str, value: Any, note: str = "") -> None:
        """Persist a durable fact or preference."""
        data = self._read_json(self.memory_path, {"version": 1, "entries": {}})
        data.setdefault("entries", {})[key] = {
            "value": value,
            "note": note,
            "updated_at": _utc_now(),
        }
        self._write_json(self.memory_path, data)
        self.record("architect.remember", key=key)

    def recall(self, key: str, default: Any = None) -> Any:
        data = self._read_json(self.memory_path, {"entries": {}})
        entry = data.get("entries", {}).get(key)
        return entry.get("value") if isinstance(entry, dict) else default

    def forget(self, key: str) -> bool:
        data = self._read_json(self.memory_path, {"entries": {}})
        if key in data.get("entries", {}):
            del data["entries"][key]
            self._write_json(self.memory_path, data)
            self.record("architect.forget", key=key)
            return True
        return False

    def memories(self) -> Dict[str, Any]:
        return self._read_json(self.memory_path, {"entries": {}}).get("entries", {})

    # --- snapshots -----------------------------------------------------------

    def snapshot(self, paths: Iterable[Path], label: str = "") -> Snapshot:
        """Copy ``paths`` into a new snapshot before they are modified.

        Paths that do not exist are recorded with ``existed=False`` so that a
        rollback removes files the run created.
        """
        if not self.snapshots_dir.is_dir():
            raise ArchitectError("state directory is not initialised; run `shadow init` first")

        snap = Snapshot(id=uuid.uuid4().hex[:12], label=label, created_at=_utc_now())
        target = self.snapshots_dir / snap.id
        target.mkdir(parents=True, exist_ok=True)

        for index, raw in enumerate(paths):
            path = Path(raw)
            absolute = path if path.is_absolute() else (self.root / path)
            try:
                relative = str(absolute.relative_to(self.root))
            except ValueError:
                relative = str(absolute)

            if absolute.is_file():
                stored = f"{index:04d}_{absolute.name}"
                shutil.copy2(absolute, target / stored)
                snap.entries.append(
                    SnapshotEntry(path=relative, stored=stored, existed=True, size=absolute.stat().st_size)
                )
            else:
                snap.entries.append(SnapshotEntry(path=relative, stored="", existed=False))

        self._write_json(target / "manifest.json", snap.to_dict())
        self.record("architect.snapshot", snapshot=snap.id, label=label, files=len(snap.entries))
        return snap

    def list_snapshots(self) -> List[Snapshot]:
        if not self.snapshots_dir.is_dir():
            return []
        out: List[Snapshot] = []
        for directory in sorted(self.snapshots_dir.iterdir()):
            manifest = directory / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                out.append(Snapshot.from_dict(json.loads(manifest.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(out, key=lambda s: s.created_at)

    def rollback(self, snapshot_id: str) -> Dict[str, Any]:
        """Restore every file in a snapshot to its captured content.

        Files recorded as not having existed are deleted. Returns a report of
        what was restored, removed, and skipped.
        """
        directory = self.snapshots_dir / snapshot_id
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            raise ArchitectError(f"no snapshot named {snapshot_id!r}")

        try:
            snap = Snapshot.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise ArchitectError(f"snapshot {snapshot_id!r} manifest is unreadable: {exc}") from exc

        restored: List[str] = []
        removed: List[str] = []
        skipped: List[str] = []

        for entry in snap.entries:
            destination = Path(entry.path)
            if not destination.is_absolute():
                destination = self.root / destination
            if entry.existed:
                source = directory / entry.stored
                if not source.is_file():
                    skipped.append(entry.path)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                restored.append(entry.path)
            else:
                if destination.is_file():
                    try:
                        destination.unlink()
                        removed.append(entry.path)
                    except OSError:
                        skipped.append(entry.path)

        report = {
            "snapshot": snapshot_id,
            "restored": restored,
            "removed": removed,
            "skipped": skipped,
        }
        self.record("architect.rollback", **report)
        return report

    # --- sessions ------------------------------------------------------------

    def write_session(self, payload: Dict[str, Any]) -> Optional[Path]:
        if not self.sessions_dir.is_dir():
            return None
        path = self.sessions_dir / f"{self.session_id}.json"
        self._write_json(path, {"id": self.session_id, "closed_at": _utc_now(), **payload})
        return path

    # --- json helpers --------------------------------------------------------

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not Path(path).is_file():
            return default
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ArchitectError(f"could not write {path}: {exc}") from exc


def find_root(start: Optional[Path] = None) -> Path:
    """Walk upward for an existing project root.

    ``.shadow/`` wins over ``.git/``, and it wins at *any* depth -- the whole
    ancestor chain is checked for a state directory before ``.git`` is
    considered at all. Without that ordering, a project nested inside an
    unrelated repository resolves to the wrong root: the outer repo's
    directory, whose files have nothing to do with the work at hand.

    Falls back to the starting directory when neither marker is found.
    """
    current = Path(start or Path.cwd()).resolve()
    chain = (current, *current.parents)

    for candidate in chain:
        if (candidate / STATE_DIRNAME).is_dir():
            return candidate
    for candidate in chain:
        if (candidate / ".git").is_dir():
            return candidate
    return current
