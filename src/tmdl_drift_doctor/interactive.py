"""Interactive confirmation for remediation.

Rules (they exist because a piped "y" once answered the wrong prompt):

* stdin that is not a terminal is never read: every question is answered
  "no", and the caller says so once;
* on a terminal, ``y``/``yes`` and ``n``/``no`` (any case) answer; ``a``
  answers yes to this and every later non-destructive question, ``q`` stops
  asking (everything left is skipped); anything else asks again; end of
  input is "no";
* ``assume_yes`` never reads stdin and applies only what the batch mode
  would: a batch-safe kind, no override, no divergence guard, and the first
  NON-destructive action. Destructive actions are never taken without a
  person answering.
* automatic actions run without a question in every mode; a false
  precondition skips the item without asking.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Action:
    label: str
    prompt: str
    apply: Callable
    destructive: bool = False
    auto: bool = False


@dataclass
class Item:
    kind: str
    ref: str
    detail: str = ""
    actions: list = field(default_factory=list)
    precondition: Optional[Callable] = None
    precondition_reason: str = ""
    divergent_guard: bool = False
    divergent_guard_reason: str = ""


@dataclass
class Counts:
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    manual: int = 0
    log: list = field(default_factory=list)


class Quit(Exception):
    """The person asked to stop."""


def stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def ask(question: str, *, allow_all: bool = False) -> bool | str:
    """Ask one yes/no question on a terminal. Never reads a non-terminal
    stdin (the answer is then "no"). Returns True, False, or "all" when
    `allow_all` and the person answered ``a``."""
    if not stdin_is_interactive():
        return False
    suffix = " [y/n/a/q] " if allow_all else " [y/n] "
    while True:
        try:
            answer = input(question + suffix)
        except EOFError:
            return False
        word = (answer or "").strip().lower()
        if word in ("y", "yes"):
            return True
        if word in ("n", "no"):
            return False
        if allow_all and word in ("a", "all"):
            return "all"
        if allow_all and word in ("q", "quit"):
            raise Quit()


def _kind_in(kind: str, kinds) -> bool:
    return any(kind == k or kind.startswith(k.rstrip(".") + ".") for k in kinds)


def run_interactive(items, *, model_path=None, overrides=None,
                    dry_run: bool = False, assume_yes: bool = False,
                    batch_safe=None) -> Counts:
    """Walk `items`, applying the action each one is given (see the module
    docstring). `overrides` maps an item ref to the reason it is declared
    divergent; such items are skipped."""
    if batch_safe is None:
        from .remediate import BATCH_SAFE_KINDS as batch_safe
    overrides = overrides or {}
    counts = Counts()
    interactive = stdin_is_interactive() and not assume_yes
    always = False
    for n, item in enumerate(items, 1):
        def skip(reason, item=item):
            counts.skipped += 1
            counts.log.append((item.ref, "skipped", reason))

        if item.precondition is not None and not item.precondition():
            skip(item.precondition_reason or "precondition not met")
            continue
        if not item.actions:
            counts.manual += 1
            counts.log.append((item.ref, "manual", item.detail))
            continue
        chosen = next((a for a in item.actions if a.auto), None)
        if chosen is None:
            if item.ref in overrides:
                skip(f"declared divergence: {overrides[item.ref]}")
                continue
            if assume_yes:
                if not _kind_in(item.kind, batch_safe):
                    skip(f"kind '{item.kind}' is not batch-safe; it needs an "
                         f"interactive answer")
                    continue
                if item.divergent_guard:
                    skip(item.divergent_guard_reason or "divergence guard")
                    continue
                chosen = next((a for a in item.actions if not a.destructive),
                              None)
                if chosen is None:
                    skip("destructive: never applied under --yes")
                    continue
            elif not interactive:
                skip("non-interactive: questions are disabled (stdin is not "
                     "a terminal)")
                continue
            else:
                try:
                    for action in item.actions:
                        if always and not action.destructive:
                            chosen = action
                            break
                        answer = ask(f"[{n}/{len(items)}] {item.kind} "
                                     f"{item.ref}: {action.prompt}",
                                     allow_all=True)
                        if answer == "all":
                            always = True
                        if answer:
                            chosen = action
                            break
                except Quit:
                    for _rest in items[n - 1:]:
                        counts.skipped += 1
                    return counts
                if chosen is None:
                    skip("declined")
                    continue
        if dry_run:
            counts.log.append((item.ref, "would-apply", chosen.label))
            continue
        try:
            msg = chosen.apply(model_path)
            counts.applied += 1
            counts.log.append((item.ref, "applied", msg))
        except Exception as e:      # an action failing never stops the walk
            counts.failed += 1
            counts.log.append((item.ref, "failed", str(e)))
    return counts
