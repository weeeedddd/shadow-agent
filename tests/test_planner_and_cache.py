"""Tests for the reasoning planner, skill reuse, and the canonicalising cache.

The planner is tested against a fake core. That is deliberate: these assert the
framework's *decisions* — when it reuses a skill instead of calling the API,
what it injects, how it degrades when the core fails — none of which should
depend on a network or a live model.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shadow_agent.architect.skills import Skill, SkillForge
from shadow_agent.core import context
from shadow_agent.eminence.coral import (
    Action,
    ActionKind,
    Decision,
    PermissionWall,
    action_for_command,
    canonicalize,
)
from shadow_agent.llm.client import (
    CoreAuthError,
    CoreRateLimited,
    Reply,
    build_request,
    extract_json,
    translate_error,
)
from shadow_agent.monarch.analyzer import Monarch
from shadow_agent.monarch.planner import PLAN_SCHEMA, ReasoningPlanner


class FakeCore:
    """A reasoning core that returns whatever it is told to."""

    def __init__(self, text: str = "", raises: Exception = None) -> None:
        self.text = text
        self.raises = raises
        self.calls = []

    def complete_with_retry(self, system, messages, *, schema=None, max_tokens=None, **kw):
        self.calls.append({"system": system, "messages": messages, "schema": schema})
        if self.raises:
            raise self.raises
        return Reply(text=self.text, model="fake", input_tokens=100, output_tokens=50)

    complete = complete_with_retry


class TestCanonicalisation(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.gettempdir()

    def same(self, a: str, b: str) -> bool:
        return canonicalize(a, self.cwd) == canonicalize(b, self.cwd)

    def test_trailing_slash_is_the_same_command(self):
        """The exact case the brittle cache got wrong."""
        self.assertTrue(self.same("rm -rf build/", "rm -rf build"))

    def test_whitespace_is_normalised(self):
        self.assertTrue(self.same("rm  -rf   build", "rm -rf build"))

    def test_combined_short_flag_order_does_not_matter(self):
        self.assertTrue(self.same("rm -fr build", "rm -rf build"))

    def test_relative_prefixes_and_dot_segments_collapse(self):
        self.assertTrue(self.same("rm -rf ./build", "rm -rf build"))
        self.assertTrue(self.same("rm -rf build/../build", "rm -rf build"))

    def test_different_targets_never_collide(self):
        self.assertFalse(self.same("rm -rf build", "rm -rf dist"))
        self.assertFalse(self.same("rm -rf build", "rm -rf /"))

    def test_different_flags_never_collide(self):
        self.assertFalse(self.same("git push --force", "git push"))

    def test_long_flags_are_not_reordered(self):
        self.assertFalse(self.same("cmd --alpha --beta", "cmd --beta --alpha"))

    def test_program_name_is_not_treated_as_a_path(self):
        self.assertTrue(canonicalize("rm -rf build", self.cwd).startswith("rm "))

    def test_subcommand_is_not_treated_as_a_path(self):
        self.assertEqual(canonicalize("git push --force", self.cwd), "git push --force")

    def test_unlexable_command_falls_back_to_exact_match(self):
        broken = 'rm -rf "unterminated'
        self.assertEqual(canonicalize(broken, self.cwd), broken)


class TestSmartCache(unittest.TestCase):
    def wall(self, answers):
        queue = list(answers)
        return PermissionWall(
            emit=lambda _t: None,
            prompt=lambda _p: queue.pop(0) if queue else "n",
            assume_interactive=True,
        )

    def test_always_covers_a_respelled_command(self):
        """Approve `rm -rf build/`; `rm -rf build` must not ask again."""
        cwd = tempfile.gettempdir()
        w = self.wall(["a"])  # only one answer available
        self.assertIs(w.request(action_for_command("rm -rf build/", cwd=cwd)), Decision.ALWAYS)
        self.assertIs(w.request(action_for_command("rm -rf build", cwd=cwd)), Decision.PROCEED)
        self.assertIs(w.request(action_for_command("rm  -fr  ./build", cwd=cwd)), Decision.PROCEED)

    def test_always_does_not_cover_a_different_target(self):
        cwd = tempfile.gettempdir()
        w = self.wall(["a"])
        w.request(action_for_command("rm -rf build/", cwd=cwd))
        self.assertIs(w.request(action_for_command("rm -rf dist", cwd=cwd)), Decision.ABORT)

    def test_always_remains_scoped_to_its_directory(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            w = self.wall(["a"])
            w.request(Action(ActionKind.SHELL, "rm -rf build", cwd=a, verdict=action_for_command("rm -rf build").verdict))
            second = w.request(Action(ActionKind.SHELL, "rm -rf build", cwd=b, verdict=action_for_command("rm -rf build").verdict))
            self.assertIs(second, Decision.ABORT)


class TestSkillReuse(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.forge = SkillForge(self.root)
        self.state = context.collect(cwd=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def directive(self, text: str):
        return Monarch().draft(text, self.state, root=self.root)

    def proven_skill(self, name="run-the-tests", uses=9, successes=9):
        skill = Skill(
            name=name,
            description="run the project test suite",
            commands=["python -m unittest discover -s tests"],
            tags=["python", "unittest", "tests"],
            uses=uses,
            successes=successes,
        )
        self.forge.save(skill)
        return skill

    def test_confident_skill_is_applied_without_calling_the_core(self):
        """The closed loop: remembered work replaces reasoning."""
        self.proven_skill()
        core = FakeCore(text="{}")
        planner = ReasoningPlanner(core, self.forge)
        steps = planner.plan(self.directive("run the project test suite"), self.state)
        self.assertEqual(core.calls, [], "the API was called despite a confident skill match")
        self.assertEqual(steps[0].command, "python -m unittest discover -s tests")
        self.assertEqual(planner.last_source.kind, "skill")

    def test_unproven_skill_is_injected_but_not_applied(self):
        """One success is not evidence. Offer it; do not act on it."""
        self.proven_skill(uses=1, successes=1)
        core = FakeCore(text='{"reasoning":"ok","steps":[{"command":"echo hi","rationale":"r"}]}')
        planner = ReasoningPlanner(core, self.forge)
        planner.plan(self.directive("run the project test suite"), self.state)
        self.assertEqual(len(core.calls), 1)
        self.assertIn("PRIOR SKILLS", core.calls[0]["messages"][0]["content"])

    def test_unreliable_skill_is_not_applied(self):
        """A skill that fails half the time must not short-circuit anything."""
        self.proven_skill(uses=10, successes=4)
        core = FakeCore(text='{"reasoning":"ok","steps":[]}')
        planner = ReasoningPlanner(core, self.forge)
        planner.plan(self.directive("run the project test suite"), self.state)
        self.assertEqual(len(core.calls), 1)

    def test_reuse_increments_the_skill_usage_count(self):
        skill = self.proven_skill()
        planner = ReasoningPlanner(FakeCore("{}"), self.forge)
        planner.plan(self.directive("run the project test suite"), self.state)
        self.assertEqual(self.forge.get(skill.name).uses, skill.uses + 1)

    def test_irrelevant_skills_are_not_injected(self):
        self.proven_skill(name="deploy-to-fly")
        core = FakeCore(text='{"reasoning":"ok","steps":[]}')
        planner = ReasoningPlanner(core, self.forge)
        planner.plan(self.directive("compile the rust binary"), self.state)
        self.assertNotIn("deploy-to-fly", core.calls[0]["messages"][0]["content"])


class TestPlannerDegradation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = context.collect(cwd=self.root)
        self.directive = Monarch().draft("do something", self.state, root=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rate_limit_returns_an_empty_plan_not_an_exception(self):
        planner = ReasoningPlanner(FakeCore(raises=CoreRateLimited("429")), None)
        self.assertEqual(planner.plan(self.directive, self.state), [])
        self.assertIn("429", planner.last_source.error)

    def test_auth_error_returns_an_empty_plan_not_an_exception(self):
        planner = ReasoningPlanner(FakeCore(raises=CoreAuthError("bad key")), None)
        self.assertEqual(planner.plan(self.directive, self.state), [])
        self.assertIn("bad key", planner.last_source.error)

    def test_unexpected_exception_is_contained(self):
        """A bug in the core must not reach the terminal as a traceback."""
        planner = ReasoningPlanner(FakeCore(raises=ValueError("boom")), None)
        self.assertEqual(planner.plan(self.directive, self.state), [])
        self.assertIn("ValueError", planner.last_source.error)

    def test_garbage_response_is_rejected(self):
        planner = ReasoningPlanner(FakeCore(text="I'm sorry, I can't do that"), None)
        self.assertEqual(planner.plan(self.directive, self.state), [])
        self.assertIn("usable plan", planner.last_source.error)

    def test_blocked_reason_is_surfaced(self):
        planner = ReasoningPlanner(
            FakeCore(text='{"reasoning":"no","steps":[],"blocked_reason":"needs a database URL"}'),
            None,
        )
        self.assertEqual(planner.plan(self.directive, self.state), [])
        self.assertIn("database", planner.last_source.blocked_reason)

    def test_plan_is_capped(self):
        many = ",".join('{"command":"echo %d","rationale":"r"}' % i for i in range(50))
        planner = ReasoningPlanner(FakeCore(text='{"reasoning":"x","steps":[%s]}' % many), None, max_steps=3)
        self.assertEqual(len(planner.plan(self.directive, self.state)), 3)

    def test_fenced_json_is_parsed(self):
        planner = ReasoningPlanner(
            FakeCore(text='here you go\n```json\n{"reasoning":"x","steps":[{"command":"ls","rationale":"r"}]}\n```'),
            None,
        )
        self.assertEqual(planner.plan(self.directive, self.state)[0].command, "ls")


class TestRequestShape(unittest.TestCase):
    """Verified against anthropic 1.0.0's own type stubs, not recalled."""

    def setUp(self):
        from shadow_agent.config import LLMConfig

        self.config = LLMConfig()

    def test_thinking_is_adaptive_and_budget_tokens_is_absent(self):
        request = build_request(self.config, "s", [{"role": "user", "content": "x"}])
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertNotIn("budget_tokens", str(request))

    def test_effort_nests_inside_output_config(self):
        request = build_request(self.config, "s", [{"role": "user", "content": "x"}])
        self.assertEqual(request["output_config"]["effort"], "high")
        self.assertNotIn("effort", set(request) - {"output_config"})

    def test_schema_uses_the_json_schema_format_shape(self):
        request = build_request(
            self.config, "s", [{"role": "user", "content": "x"}], schema=PLAN_SCHEMA
        )
        fmt = request["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertIs(fmt["schema"], PLAN_SCHEMA)

    def test_model_default_is_opus_5(self):
        self.assertEqual(build_request(self.config, "", [])["model"], "claude-opus-5")


class TestErrorTranslation(unittest.TestCase):
    def make(self, name: str, status=None):
        exc = type(name, (Exception,), {})("boom")
        if status is not None:
            exc.status_code = status
        return exc

    def test_rate_limit_is_retryable(self):
        self.assertTrue(translate_error(self.make("RateLimitError", 429)).retryable)

    def test_overload_is_retryable(self):
        self.assertTrue(translate_error(self.make("OverloadedError", 529)).retryable)

    def test_connection_error_is_retryable(self):
        self.assertTrue(translate_error(self.make("APIConnectionError")).retryable)

    def test_auth_error_is_not_retryable(self):
        """Retrying a bad key is four times slower and no more likely to work."""
        self.assertFalse(translate_error(self.make("AuthenticationError", 401)).retryable)

    def test_bad_request_is_not_retryable(self):
        self.assertFalse(translate_error(self.make("BadRequestError", 400)).retryable)


class TestExtractJson(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_padded_with_prose(self):
        self.assertEqual(extract_json('Sure!\n{"a": 1}\nHope that helps'), {"a": 1})

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json("no json anywhere here"))
        self.assertIsNone(extract_json(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
