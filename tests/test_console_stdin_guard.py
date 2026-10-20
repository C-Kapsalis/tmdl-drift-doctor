"""Piped stdin must never answer an interactive remediation prompt (ported).

The near-miss: `echo y | <remediate> --category fieldparam` answered item
[1/4] (a category sync, which deleted a row) instead of the intended item
[4/4]. The prompt reads one shared stdin, so the FIRST non-auto prompt of the
run consumes the piped byte and the caller cannot see which prompt got it.

Guards, in the interactive runner and its prompt (G-interactive-confirm,
ruling R3):
- non-tty stdin is never read: every prompt defaults to 'no';
- `--yes` (gated to require an explicit --kind) never reads stdin and applies
  ONLY what the batch mode would apply: batch-safe kind -> override ->
  divergence guard -> first NON-destructive action. Detectors do not reliably
  flag destructive=True (field-parameter parity rebuilds and refresh-policy
  windows are interactive-only by design), so the kind set is the authority.

Port: the original console (`run_console`, `_ask`, `SYNC_CATEGORIES`) and its
item/action classes are replaced by drift-doctor's interactive runner
(`tmdl_drift_doctor.interactive.run_interactive`, `ask`, `Item`, `Action`)
and its batch-safe kind set (`tmdl_drift_doctor.remediate.BATCH_SAFE_KINDS`).
The CLI case runs `drift-doctor remediate --yes --model spinout-05`.
"""

import io
import shutil
import sys

import pytest
from conftest import FIXTURES, drift, tree_state, write_fleet


# A kind the batch mode applies (batch-safe by owner decision).
SAFE_KIND = "fieldparam.cross_level"

ROASTERY = FIXTURES / "b5-overrides" / "roastery" / "Roastery.SemanticModel"


@pytest.fixture
def ui():
    """The tool's interactive runner (imported lazily so a missing module is
    a per-test error, not a collection error)."""
    from tmdl_drift_doctor import interactive
    from tmdl_drift_doctor.remediate import BATCH_SAFE_KINDS
    # Module-level assertion of the original: the safe kind is in the
    # batch-safe set exactly or as a dotted child of a listed kind (R4).
    assert any(SAFE_KIND == c or SAFE_KIND.startswith(c + ".")
               for c in BATCH_SAFE_KINDS)
    return interactive


class _PoisonedStdin(io.StringIO):
    """Fails the test if any prompt actually reads stdin."""

    def __init__(self):
        super().__init__("y\ny\ny\n")

    def read(self, *a, **k):
        raise AssertionError("prompt read stdin in a mode that must not")

    readline = read

    def isatty(self):
        return False


def _item(ui, name, applied_log, kind=SAFE_KIND, destructive=False,
          auto=False):
    def _apply(_path, name=name):
        applied_log.append(name)
        return f"applied {name}"

    return ui.Item(
        kind=kind,
        ref=name,
        detail=f"detail {name}",
        actions=[ui.Action(label=name, prompt=f"apply {name}?", apply=_apply,
                           destructive=destructive, auto=auto)],
    )


def _run(ui, tmp_path, items, **kw):
    return ui.run_interactive(items, model_path=tmp_path, overrides={},
                              dry_run=False, **kw)


def test_piped_stdin_never_answers_prompts(tmp_path, monkeypatch, ui):
    """The near-miss shape: piped 'y' + multiple prompts -> ZERO applies."""
    applied = []
    items = [_item(ui, "first", applied), _item(ui, "second", applied)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\ny\n"))  # isatty() False
    counts = _run(ui, tmp_path, items)
    assert applied == [], (
        "a piped 'y' answered an interactive prompt — the near-miss"
    )
    assert counts.applied == 0 and counts.skipped == 2


def test_tty_prompts_unchanged(tmp_path, monkeypatch, ui):
    """Interactive behaviour is untouched: a real tty answers per prompt."""
    applied = []
    items = [_item(ui, "first", applied), _item(ui, "second", applied)]
    fake_tty = io.StringIO()
    fake_tty.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_tty)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    counts = _run(ui, tmp_path, items)
    assert applied == ["second"]
    assert counts.applied == 1 and counts.skipped == 1


def test_tty_eof_is_no(tmp_path, monkeypatch, ui):
    """EOF on a real tty (ctrl-D) stays 'no' — no crash, no apply."""
    fake_tty = io.StringIO()
    fake_tty.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_tty)

    def _eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert ui.ask("anything?") is False


def test_yes_mode_applies_batch_safe_without_reading_stdin(tmp_path,
                                                          monkeypatch, ui):
    applied = []
    items = [
        _item(ui, "safe", applied),
        _item(ui, "dangerous", applied, destructive=True),
    ]
    monkeypatch.setattr(sys, "stdin", _PoisonedStdin())
    counts = _run(ui, tmp_path, items, assume_yes=True)
    assert applied == ["safe"], "assume_yes must never reach a destructive action"
    assert counts.applied == 1 and counts.skipped == 1


def test_yes_mode_refuses_non_batch_safe_categories(tmp_path, monkeypatch, ui):
    """Field-parameter parity / refresh-policy actions are interactive-only by
    design but NOT flagged destructive — the kind gate, not the flag, must
    stop --yes from applying them."""
    applied = []
    items = [
        _item(ui, "parity", applied, kind="fieldparam.parity"),
        _item(ui, "refresh", applied, kind="refresh_policy.window_drift"),
    ]
    monkeypatch.setattr(sys, "stdin", _PoisonedStdin())
    counts = _run(ui, tmp_path, items, assume_yes=True)
    assert applied == []
    assert counts.applied == 0 and counts.skipped == 2


def test_yes_mode_honors_divergence_guard(tmp_path, monkeypatch, ui):
    """The batch mode skips divergence-guarded items; --yes must apply the
    same gate."""
    applied = []
    guarded = _item(ui, "guarded", applied)
    guarded.divergent_guard = True
    guarded.divergent_guard_reason = "source-divergent endpoint"
    monkeypatch.setattr(sys, "stdin", _PoisonedStdin())
    counts = _run(ui, tmp_path, [guarded], assume_yes=True)
    assert applied == []
    assert counts.applied == 0 and counts.skipped == 1


def test_yes_mode_still_honors_auto_and_preconditions(tmp_path, monkeypatch,
                                                      ui):
    applied = []
    # auto actions apply regardless of kind (they never prompt anyway)
    auto_item = _item(ui, "auto", applied, kind="test.auto", auto=True)
    gated = _item(ui, "gated", applied)
    gated.precondition = lambda: False
    gated.precondition_reason = "gated off"
    monkeypatch.setattr(sys, "stdin", _PoisonedStdin())
    counts = _run(ui, tmp_path, [auto_item, gated], assume_yes=True)
    assert applied == ["auto"]
    assert counts.applied == 1 and counts.skipped == 1


def _roastery_fleet(tmp_path):
    """Template + spinout-05 lacking two template measures (two allowlisted
    measure.missing findings)."""
    from tmdl_drift_doctor import tmdl_edit as te
    template = tmp_path / "template" / "Roastery.SemanticModel"
    derived = tmp_path / "spinout-05" / "Roastery.SemanticModel"
    shutil.copytree(ROASTERY, template)
    shutil.copytree(ROASTERY, derived)
    sales = derived / "definition" / "tables" / "Sales.tmdl"
    text = sales.read_text(encoding="utf-8")
    for m in ("Cups Sold", "Average Ticket"):
        text = te.remove_block(text, "measure", m)
    sales.write_text(text, encoding="utf-8")
    return write_fleet(tmp_path, template, {"spinout-05": derived},
                       allowlist={"kinds": ["measure.missing"]})


def test_yes_flag_requires_category(tmp_path):
    """`--yes` without `--kind` is a usage error (exit 2)."""
    fy = _roastery_fleet(tmp_path)
    r = drift("remediate", "--yes", "--model", "spinout-05", fleet=fy)
    assert r.exit_code == 2
    assert "--yes requires an explicit --kind" in r.stderr


def test_noninteractive_remediate_applies_nothing_without_yes(tmp_path):
    """New (G-interactive-confirm case 9, tool contract): a non-TTY run with
    no --yes on two allowlisted measure.missing findings writes nothing,
    counts both skipped, and says once that questions are disabled."""
    fy = _roastery_fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    before = tree_state(tmp_path / "spinout-05")
    r = drift("remediate", fleet=fy, input="y\ny\n")
    assert tree_state(tmp_path / "spinout-05") == before
    assert "applied=0" in r.output and "skipped=2" in r.output
    assert r.output.count("non-interactive") >= 1
