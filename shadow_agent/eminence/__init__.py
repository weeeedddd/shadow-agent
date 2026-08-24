"""The Eminence -- execution against the real machine, behind the wall."""

from .coral import Action, ActionKind, Decision, HeadlessPolicy, PermissionDenied, PermissionWall
from .executor import Eminence, ExecutionResult, inspect_command
from .failure import StreakTracker, failure_class, is_hard_failure
from .policy import Judgement, Verdict, classify, unwrap

__all__ = [
    "Action",
    "ActionKind",
    "Decision",
    "Eminence",
    "ExecutionResult",
    "HeadlessPolicy",
    "Judgement",
    "PermissionDenied",
    "PermissionWall",
    "StreakTracker",
    "Verdict",
    "classify",
    "failure_class",
    "inspect_command",
    "is_hard_failure",
    "unwrap",
]
