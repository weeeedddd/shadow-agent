"""Alignment tests.

The interface makes one promise -- every frame closes and every line in a
block is the same visible width. These tests are what hold that promise to
account, across colour, wide characters, and the ASCII fallback.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shadow_agent.core import context
from shadow_agent.ui import ansi, onboarding
from shadow_agent.ui.implementory import Implementory, Status
from shadow_agent.ui.render import (
    bullets,
    kv_rows,
    pad,
    panel,
    strip_ansi,
    truncate,
    visible_width,
    wrap,
)

WIDTHS = (56, 64, 74, 88)


class TestVisibleWidth(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(visible_width("abc"), 3)

    def test_ansi_is_free(self):
        self.assertEqual(visible_width("\x1b[38;5;97mabc\x1b[0m"), 3)

    def test_wide_characters_cost_two(self):
        self.assertEqual(visible_width("日本"), 4)

    def test_combining_marks_are_free(self):
        self.assertEqual(visible_width("é"), 1)

    def test_len_disagrees(self):
        """The reason this module exists."""
        styled = "\x1b[1mx\x1b[0m"
        self.assertNotEqual(len(styled), visible_width(styled))


class TestPadAndTruncate(unittest.TestCase):
    def test_pad_exact(self):
        for width in WIDTHS:
            self.assertEqual(visible_width(pad("x", width)), width)

    def test_pad_wide_char_exact(self):
        self.assertEqual(visible_width(pad("日", 10)), 10)

    def test_pad_centre_exact(self):
        self.assertEqual(visible_width(pad("odd", 10, align="center")), 10)

    def test_pad_overlong_is_truncated_not_overflowed(self):
        self.assertEqual(visible_width(pad("x" * 40, 10)), 10)

    def test_truncate_never_exceeds(self):
        for width in range(1, 20):
            self.assertLessEqual(visible_width(truncate("日本語テスト", width)), width)


class TestPanel(unittest.TestCase):
    def _assert_rectangular(self, lines, width):
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(visible_width(line), width, f"ragged line: {strip_ansi(line)!r}")

    def test_panel_is_rectangular(self):
        for width in WIDTHS:
            self._assert_rectangular(panel(["alpha", "beta"], width=width), width)

    def test_titled_panel_is_rectangular(self):
        for width in WIDTHS:
            self._assert_rectangular(panel(["alpha"], width=width, title="STATE"), width)

    def test_panel_with_colour_is_rectangular(self):
        ansi.set_enabled(True)
        try:
            body = ["\x1b[38;5;97mcoloured\x1b[0m", "plain"]
            for width in WIDTHS:
                self._assert_rectangular(panel(body, width=width, title="X"), width)
        finally:
            ansi.set_enabled(None)

    def test_panel_with_overlong_content_is_rectangular(self):
        self._assert_rectangular(panel(["x" * 500], width=74), 74)

    def test_panel_with_wide_characters_is_rectangular(self):
        self._assert_rectangular(panel(["日本語のテキスト"], width=74), 74)

    def test_ascii_fallback_is_rectangular(self):
        os.environ["SHADOW_ASCII"] = "1"
        try:
            self._assert_rectangular(panel(["alpha"], width=74, title="ASCII"), 74)
        finally:
            os.environ.pop("SHADOW_ASCII", None)


class TestRows(unittest.TestCase):
    def test_kv_rows_share_a_width(self):
        rows = kv_rows([("A", "1"), ("LONGER KEY", "a much longer value")], 60)
        for row in rows:
            self.assertEqual(visible_width(row), 60)

    def test_kv_rows_truncate_rather_than_overflow(self):
        rows = kv_rows([("K", "v" * 500)], 40)
        self.assertEqual(visible_width(rows[0]), 40)

    def test_wrap_respects_width(self):
        for line in wrap("the quick brown fox " * 10, 24):
            self.assertLessEqual(visible_width(line), 24)

    def test_wrap_hard_splits_long_words(self):
        for line in wrap("x" * 90, 20):
            self.assertLessEqual(visible_width(line), 20)

    def test_bullets_respect_width(self):
        for line in bullets(["a short one", "b " * 80], 40):
            self.assertLessEqual(visible_width(line), 40)


class TestOnboarding(unittest.TestCase):
    def test_full_sequence_is_rectangular(self):
        state = context.collect()
        for width in WIDTHS:
            rendered = onboarding.render(state, width=width)
            for line in rendered.splitlines():
                if not line:
                    continue
                self.assertLessEqual(
                    visible_width(line), width, f"overflow at width {width}: {strip_ansi(line)!r}"
                )

    def test_call_to_action_text_is_exact(self):
        self.assertEqual(
            onboarding.CALL_TO_ACTION,
            "Provide your unpolished input. The Monarch will refine it.",
        )


class TestImplementory(unittest.TestCase):
    def test_renders_rectangular(self):
        impl = Implementory(headline="A run happened.")
        impl.build("one").limit("two").how("three").unresolved("four")
        for width in WIDTHS:
            for line in impl.render(width=width).splitlines():
                self.assertEqual(visible_width(line), width)

    def test_empty_sections_still_render(self):
        rendered = strip_ansi(Implementory().render(width=74))
        self.assertIn("WHAT WAS BUILT", rendered)
        self.assertIn("WHAT DOESN'T WORK", rendered)
        self.assertIn("HOW IT WAS DONE", rendered)

    def test_degrade_never_raises_status(self):
        impl = Implementory(status=Status.FAILED)
        impl.degrade(Status.SUCCESS)
        self.assertIs(impl.status, Status.FAILED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
