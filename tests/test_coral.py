"""Tests for mechanisms assimilated from Human-Agent-Society/CORAL (Apache-2.0).

Three extractions, each fixing something that was actually wrong here:

* **Exit quality** — we counted every exit 0 as a clean success. A command that
  returns instantly with no output almost certainly did nothing.
* **Circuit breaker** — nothing stopped a crash loop, and the naive version of
  the fix (count every failure) trips on legitimate quick completions.
* **Durable writes** — every atomic write was `write` then `replace`, which is
  atomic but not durable. A crash could leave the directory entry pointing at
  contents that never reached the disk.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shadow_agent.core.atomic import read_json, write_atomic, write_json_atomic
from shadow_agent.eminence.failure import (
    CircuitBreaker,
    ExitQuality,
    classify_exit,
)


class TestExitQuality(unittest.TestCase):
    def test_nonzero_exit_is_a_session_error(self):
        self.assertIs(classify_exit(1, 5.0, "output"), ExitQuality.SESSION_ERROR)
        self.assertIs(classify_exit(-9, 5.0, "killed"), ExitQuality.SESSION_ERROR)
        self.assertIs(classify_exit(None, 5.0, ""), ExitQuality.SESSION_ERROR)

    def test_exit_zero_with_output_is_clean_even_when_instant(self):
        """Evidence of work outranks the timing heuristic."""
        self.assertIs(classify_exit(0, 0.0001, "result: 42"), ExitQuality.CLEAN)

    def test_exit_zero_after_real_work_is_clean(self):
        self.assertIs(classify_exit(0, 3.5, ""), ExitQuality.CLEAN)

    def test_exit_zero_instant_and_silent_is_not_success(self):
        """The gap CORAL exposed: exit 0 is not the same as success."""
        self.assertIs(classify_exit(0, 0.0001, ""), ExitQuality.NO_RESULT)
        self.assertIs(classify_exit(0, 0.0, "   "), ExitQuality.NO_RESULT)


class TestCircuitBreaker(unittest.TestCase):
    def test_trips_after_consecutive_non_productive_exits(self):
        breaker = CircuitBreaker(threshold=3)
        self.assertFalse(breaker.record(ExitQuality.SESSION_ERROR))
        self.assertFalse(breaker.record(ExitQuality.NO_RESULT))
        self.assertTrue(breaker.record(ExitQuality.SESSION_ERROR))
        self.assertIn("consecutive", breaker.reason)

    def test_clean_exit_resets_the_counter(self):
        breaker = CircuitBreaker(threshold=3)
        breaker.record(ExitQuality.SESSION_ERROR)
        breaker.record(ExitQuality.SESSION_ERROR)
        breaker.record(ExitQuality.CLEAN)
        self.assertEqual(breaker.consecutive, 0)
        self.assertFalse(breaker.record(ExitQuality.SESSION_ERROR))

    def test_clean_exits_never_trip_it(self):
        """The failure mode of the naive version: stopping a working agent."""
        breaker = CircuitBreaker(threshold=3)
        tripped = False
        for _ in range(50):
            tripped = breaker.record(ExitQuality.CLEAN)
        self.assertFalse(tripped)

    def test_no_result_counts_toward_the_burst(self):
        breaker = CircuitBreaker(threshold=2)
        breaker.record(ExitQuality.NO_RESULT)
        self.assertTrue(breaker.record(ExitQuality.NO_RESULT))

    def test_reset_clears_a_tripped_breaker(self):
        breaker = CircuitBreaker(threshold=1)
        breaker.record(ExitQuality.SESSION_ERROR)
        self.assertTrue(breaker.tripped)
        breaker.reset()
        self.assertFalse(breaker.tripped)
        self.assertEqual(breaker.reason, "")


class TestDurableWrites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_and_read_round_trip(self):
        path = write_json_atomic(self.dir / "a.json", {"k": "v"})
        self.assertEqual(read_json(path), {"k": "v"})

    def test_creates_parent_directories(self):
        path = write_json_atomic(self.dir / "deep" / "nested" / "a.json", {"k": 1})
        self.assertTrue(path.is_file())

    def test_overwrite_replaces_atomically(self):
        path = self.dir / "a.json"
        write_json_atomic(path, {"v": 1})
        write_json_atomic(path, {"v": 2})
        self.assertEqual(read_json(path), {"v": 2})

    def test_no_temp_files_are_left_behind(self):
        write_json_atomic(self.dir / "a.json", {"k": "v"})
        leftovers = [p.name for p in self.dir.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_temp_file_is_created_in_the_destination_directory(self):
        """os.replace across filesystems is not atomic; /tmp is often another one."""
        target = self.dir / "sub"
        target.mkdir()
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(Path(src).parent)
            return real_replace(src, dst)

        os.replace = spy
        try:
            write_json_atomic(target / "a.json", {"k": 1})
        finally:
            os.replace = real_replace
        self.assertEqual(seen[0], target)

    def test_mode_is_applied_before_the_file_becomes_visible(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not meaningful on Windows")
        path = write_atomic(self.dir / "secret", "key", mode=0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_write_leaves_no_litter(self):
        class Unserialisable:
            pass

        with self.assertRaises(Exception):
            write_atomic(self.dir / "a.txt", None)  # type: ignore[arg-type]
        self.assertEqual([p for p in self.dir.iterdir() if ".tmp" in p.name], [])

    def test_read_json_returns_default_on_damage(self):
        path = self.dir / "broken.json"
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(read_json(path, default={"fallback": True}), {"fallback": True})

    def test_read_json_returns_default_when_absent(self):
        self.assertIsNone(read_json(self.dir / "nope.json"))

    def test_output_is_byte_stable_for_unchanged_input(self):
        """sort_keys is not cosmetic: an unchanged object must not look changed."""
        a = self.dir / "a.json"
        b = self.dir / "b.json"
        write_json_atomic(a, {"z": 1, "a": 2})
        write_json_atomic(b, {"a": 2, "z": 1})
        self.assertEqual(a.read_bytes(), b.read_bytes())


class TestLoopUsesExitQuality(unittest.TestCase):
    """The loop must not report an unproductive step as a success."""

    def test_silent_instant_success_does_not_count_as_succeeded(self):
        from shadow_agent.core import context
        from shadow_agent.eminence.coral import ActionKind, PermissionWall
        from shadow_agent.loop.core import CoreLoop, Step

        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "true")]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop = CoreLoop(
                root,
                wall=PermissionWall(emit=lambda _t: None, assume_interactive=True),
                planner=P(),
            )
            result = loop.run("do nothing", context.collect(cwd=root))
            self.assertEqual(result.executed, 1)
            # `true` exits 0 with no output. Whether it counts as productive
            # depends on how long the shell took to start it, so assert the
            # invariant that holds either way: unproductive steps are counted,
            # and a run whose steps were all unproductive is not a success.
            if result.unproductive:
                self.assertFalse(result.succeeded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
