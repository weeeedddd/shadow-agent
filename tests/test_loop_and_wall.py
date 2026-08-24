"""Tests for the core loop, the CORAL permission wall, the Skill Forge, and the Claw.

The wall tests are the important ones. A permission gate that can be bypassed
is worse than none, because it manufactures the appearance of review — so these
assert the bypasses *do not exist*, not merely that the happy path works.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shadow_agent.architect.skills import Skill, SkillForge
from shadow_agent.core import context
from shadow_agent.eminence import policy
from shadow_agent.eminence.coral import (
    Action,
    ActionKind,
    Decision,
    HeadlessPolicy,
    PermissionDenied,
    PermissionWall,
    action_for_command,
    action_for_write,
)
from shadow_agent.loop.core import CoreLoop, HeuristicPlanner, Hooks, Phase, Step
from shadow_agent.monarch.research import InformationClaw, Source, _TextExtractor


def wall(answers=None, **kwargs) -> PermissionWall:
    """A wall wired to scripted answers and a silent emitter."""
    queue = list(answers or [])
    return PermissionWall(
        emit=lambda _text: None,
        prompt=lambda _p: queue.pop(0) if queue else "n",
        assume_interactive=True,
        **kwargs,
    )


class TestPermissionWall(unittest.TestCase):
    def test_deny_is_never_promptable(self):
        """No answer, no mode, and no session entry unlocks a denied action."""
        w = wall(["y", "y", "a"], paranoid=True)
        w.auto_approved.add(action_for_command("rm -rf /", cwd="/tmp").fingerprint())
        self.assertIs(w.request(action_for_command("rm -rf /", cwd="/tmp")), Decision.ABORT)

    def test_approve_tier_prompts_and_honours_yes(self):
        w = wall(["y"])
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.PROCEED)

    def test_approve_tier_honours_no(self):
        w = wall(["n"])
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.ABORT)

    def test_bare_enter_means_abort_not_proceed(self):
        """The dangerous default. A reflex Enter must never approve."""
        w = wall([""])
        self.assertIs(w.request(action_for_command("git push --force")), Decision.ABORT)

    def test_eof_aborts_rather_than_assuming_yes(self):
        def raising(_prompt):
            raise EOFError

        w = PermissionWall(emit=lambda _t: None, prompt=raising, assume_interactive=True)
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.ABORT)

    def test_allow_tier_does_not_prompt(self):
        w = wall([])  # any prompt would pop from an empty queue and answer "n"
        self.assertIs(w.request(action_for_command("ls -la")), Decision.PROCEED)

    def test_paranoid_escalates_allow_to_a_prompt(self):
        w = wall(["n"], paranoid=True)
        self.assertIs(w.request(action_for_command("ls -la")), Decision.ABORT)

    def test_always_is_scoped_to_command_and_directory(self):
        """`rm -rf build` approved in /tmp must not carry into /home."""
        w = wall(["a"])
        first = Action(ActionKind.SHELL, "rm -rf build", cwd="/tmp", verdict=policy.Verdict.APPROVE)
        self.assertIs(w.request(first), Decision.ALWAYS)

        same = Action(ActionKind.SHELL, "rm -rf build", cwd="/tmp", verdict=policy.Verdict.APPROVE)
        self.assertIs(w.request(same), Decision.PROCEED)  # remembered, no prompt

        elsewhere = Action(ActionKind.SHELL, "rm -rf build", cwd="/home", verdict=policy.Verdict.APPROVE)
        self.assertIs(w.request(elsewhere), Decision.ABORT)  # prompted; queue empty → "n"

    def test_quit_latches_for_the_rest_of_the_run(self):
        w = wall(["q"])
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.QUIT)
        self.assertIs(w.request(action_for_command("ls")), Decision.QUIT)

    def test_headless_denies_by_default(self):
        w = PermissionWall(emit=lambda _t: None, headless=HeadlessPolicy.DENY, assume_interactive=False)
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.ABORT)

    def test_headless_allow_is_opt_in_only(self):
        w = PermissionWall(emit=lambda _t: None, headless=HeadlessPolicy.ALLOW, assume_interactive=False)
        self.assertIs(w.request(action_for_command("rm -rf build/")), Decision.PROCEED)

    def test_headless_error_raises_rather_than_skipping(self):
        w = PermissionWall(emit=lambda _t: None, headless=HeadlessPolicy.ERROR, assume_interactive=False)
        with self.assertRaises(PermissionDenied):
            w.request(action_for_command("rm -rf build/"))

    def test_overwrite_requires_approval_but_creation_does_not(self):
        self.assertIs(action_for_write("a.py", "x\n").verdict, policy.Verdict.ALLOW)
        self.assertIs(action_for_write("a.py", "x\n", existing="old\nold\n").verdict, policy.Verdict.APPROVE)

    def test_every_decision_is_logged(self):
        w = wall(["y", "n"])
        w.request(action_for_command("rm -rf build/"))
        w.request(action_for_command("git push --force"))
        w.request(action_for_command("ls"))
        self.assertEqual(len(w.log), 3)
        self.assertEqual(w.summary()["proceed"], 2)


class TestCoreLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sample.txt").write_text("hello\n", encoding="utf-8")
        self.state = context.collect(cwd=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _loop(self, answers=None, planner=None):
        return CoreLoop(self.root, wall=wall(answers), planner=planner or HeuristicPlanner())

    def test_no_plan_yields_a_clean_no_op(self):
        loop = self._loop()
        result = loop.run("please refactor the entire codebase", self.state)
        self.assertEqual(result.executed, 0)
        self.assertFalse(result.aborted)
        self.assertEqual(result.steps, [])

    def test_recipe_executes_end_to_end(self):
        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "echo shadow-loop-ok")]

        loop = self._loop(planner=P())
        result = loop.run("say hello", self.state)
        self.assertEqual(result.executed, 1)
        self.assertTrue(result.succeeded)
        self.assertIn("shadow-loop-ok", result.outcomes[0].data.output)

    def test_wall_refusal_stops_execution(self):
        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "rm -rf build/")]

        loop = self._loop(answers=["n"], planner=P())
        result = loop.run("delete the build directory", self.state)
        self.assertEqual(result.refused, 1)
        self.assertEqual(result.executed, 0)

    def test_denied_command_never_executes(self):
        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "rm -rf /")]

        loop = self._loop(answers=["y", "y", "y"], planner=P())
        result = loop.run("delete everything", self.state)
        self.assertEqual(result.executed, 0)

    def test_stream_yields_phases_in_order(self):
        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "echo ok")]

        loop = self._loop(planner=P())
        phases = [event.phase for event in loop.stream("say ok", self.state)]
        self.assertEqual(phases[0], Phase.ANALYZE)
        self.assertEqual(phases[-1], Phase.DONE)
        self.assertIn(Phase.GATE, phases)
        self.assertIn(Phase.EXECUTE, phases)

    def test_hooks_fire_and_a_broken_hook_does_not_break_the_run(self):
        seen = []

        def boom(*_args):
            raise RuntimeError("hook exploded")

        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "echo ok")]

        hooks = Hooks(before_step=lambda step: seen.append(step.command), after_step=boom)
        loop = CoreLoop(self.root, wall=wall(), planner=P(), hooks=hooks)
        result = loop.run("say ok", self.state)
        self.assertEqual(seen, ["echo ok"])
        self.assertEqual(result.executed, 1)

    def test_run_is_bounded_by_max_steps(self):
        class P:
            def plan(self, directive, state):
                return [Step(ActionKind.SHELL, "echo x") for _ in range(50)]

        loop = CoreLoop(self.root, wall=wall(), planner=P(), max_steps=3)
        self.assertEqual(len(loop.run("spam", self.state).steps), 3)


class TestSkillForge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.forge = SkillForge(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_forges_from_success(self):
        skill = self.forge.forge("run the tests", ["python -m unittest"], succeeded=True)
        self.assertIsNotNone(skill)
        self.assertEqual(len(self.forge.all()), 1)

    def test_never_forges_from_failure(self):
        self.assertIsNone(self.forge.forge("break things", ["exit 1"], succeeded=False))
        self.assertEqual(self.forge.all(), [])

    def test_repeat_reinforces_rather_than_duplicating(self):
        self.forge.forge("run the tests", ["python -m unittest"])
        again = self.forge.forge("run tests again", ["python -m unittest"])
        self.assertEqual(len(self.forge.all()), 1)
        self.assertEqual(again.uses, 2)
        self.assertEqual(again.successes, 2)

    def test_failure_on_a_known_skill_lowers_confidence(self):
        self.forge.forge("run the tests", ["python -m unittest"])
        again = self.forge.forge("run the tests", ["python -m unittest"], succeeded=False)
        self.assertEqual(again.uses, 2)
        self.assertEqual(again.successes, 1)
        self.assertAlmostEqual(again.confidence, 0.5)

    def test_markdown_round_trip(self):
        original = Skill("deploy", "ship it", ["make build", "make deploy"], ["make"])
        parsed = Skill.from_markdown(original.to_markdown())
        self.assertEqual(parsed.name, "deploy")
        self.assertEqual(parsed.commands, ["make build", "make deploy"])

    def test_damaged_file_is_skipped_not_fatal(self):
        broken = self.forge.dir / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("not a skill at all", encoding="utf-8")
        self.forge.forge("ok", ["echo ok"])
        self.assertEqual(len(self.forge.all()), 1)

    def test_name_is_path_safe(self):
        skill = self.forge.forge("../../etc/passwd exfiltration", ["echo x"])
        self.assertNotIn("..", skill.name)
        self.assertNotIn("/", skill.name)

    def test_skills_surface_to_recall(self):
        self.forge.forge("run the tests", ["python -m unittest"])
        facts = self.forge.as_facts()
        self.assertEqual(len(facts), 1)
        self.assertIn("skill", facts[0].tags)


class TestInformationClaw(unittest.TestCase):
    def test_extracts_prose_and_drops_markup_machinery(self):
        parser = _TextExtractor()
        parser.feed(
            "<html><head><title>Docs</title><style>.x{}</style></head>"
            "<body><script>evil()</script><p>Real content here.</p>"
            "<p>More content.</p></body></html>"
        )
        text = parser.text()
        self.assertEqual(parser.title, "Docs")
        self.assertIn("Real content here.", text)
        self.assertNotIn("evil()", text)
        self.assertNotIn(".x{}", text)

    def test_excerpt_finds_the_dense_passage_not_the_header(self):
        text = "\n".join(
            [
                "Navigation home about contact links menu sidebar footer header nav.",
                "The deployment target for production is configured in fly.toml and "
                "deployment happens through the fly deploy command in production.",
                "Unrelated boilerplate paragraph about cookies and privacy policies here.",
            ]
        )
        excerpt = InformationClaw.relevant_excerpt("production deployment", text)
        self.assertIn("fly.toml", excerpt)

    def test_no_urls_and_no_backend_is_reported_not_faked(self):
        findings = InformationClaw().research("how do I deploy")
        self.assertEqual(findings.sources, [])
        self.assertTrue(any("cannot find it" in note for note in findings.notes))

    def test_non_http_scheme_refused_without_network(self):
        source = InformationClaw().fetch("file:///etc/passwd")
        self.assertIn("unsupported scheme", source.error)

    def test_thin_source_is_flagged(self):
        self.assertTrue(Source(url="u", text="tiny").thin)
        self.assertFalse(Source(url="u", text="x" * 500).thin)

    def test_synthesis_carries_provenance(self):
        findings = InformationClaw().research("x")
        findings.sources = [Source(url="https://e.com", title="E", text="y" * 500)]
        findings.excerpts = ["the answer"]
        self.assertIn("https://e.com", findings.synthesis())


if __name__ == "__main__":
    unittest.main(verbosity=2)
