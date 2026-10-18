"""Shared v-fromdate parameters stay in parity with the template (G-expression-cascade).

expressions.tmdl is a mixed file: model-specific parameters (v-db, v-BQ_project) and
model-only expressions (e.g. spinout-10's 'v-fromdate tickets') must NEVER be cascaded, but a
fixed allowlist of shared date parameters is template-canonical. drift-doctor compares ONLY
the names in `allowlist.expressions` (kind `expression.drift`); this asserts every model's copy
of those named expressions matches the template's, and that the cascade set is exactly the
intended one.

Two things the gate must agree with remediation on, or it fails on non-drift:

  * DECLARED DIVERGENCE: an `expression` override on the fleet ENTRY says "this model keeps its
    own block" (here spinout-02's secondary outlet model keeps a 2020 data floor). It is
    reported as overridden with the declared reason, never as a failure.
  * COMPARISON IDENTITY: `lineageTag:` is per-model identity metadata and the `meta [...]` bag
    is an unordered property set Power BI re-serialises in any order/spacing. Neither is drift.

An UNDECLARED value difference still fails: that is the whole gate.

Runs on the roastery stand-in fleet (tests/fixtures/b1-l1a/fleet); the original read the real
client models, the sync provider's constants and the overrides kernel.
"""
from __future__ import annotations

import pytest

from port_helpers_b1_l1a import (
    copy_fleet,
    definition,
    detect,
    fleet_doc,
    findings,
    model_paths,
    overridden,
)

MODELS = sorted(model_paths().items())
IDS = [rel.removesuffix(".SemanticModel") for _, rel in MODELS]


def test_cascade_set_is_exactly_the_named_v_fromdate_params():
    assert tuple(fleet_doc()["allowlist"]["expressions"]) == (
        "v-fromdate", "v-fromdate seo", "v-fromdate speed",
        "v-fromdate ga4", "v-fromdate email",
    ), "the cascade allowlist must stay scoped to the named v-fromdate params"


@pytest.fixture(scope="module")
def fleet_report(tmp_path_factory):
    root = copy_fleet(tmp_path_factory.mktemp("cascade"))
    return detect(root)


@pytest.mark.parametrize("model", [n for n, _ in MODELS], ids=IDS)
def test_v_fromdate_params_match_universal(fleet_report, model):
    _, rep = fleet_report
    drifted = sorted(f["ref"] for f in findings(rep, model, "expression.drift"))
    for ack in overridden(rep, model, "expression.drift"):
        # a declared divergence stays visible: it carries its reason
        assert (ack.get("override") or {}).get("reason"), ack
    assert not drifted, (
        f"{model}: shared v-fromdate param(s) drifted from the template (should be cascaded, "
        f"or declared as an `expression` override): {drifted}")


def test_secondary_model_divergence_is_acked_with_reason(fleet_report):
    """Case 3c: the override declared on entry spinout-02 covers its secondary model."""
    r, rep = fleet_report
    acks = overridden(rep, "spinout-02-performance", "expression.drift")
    assert [a["ref"] for a in acks] == ["v-fromdate"], r.output
    assert "2020 data floor" in acks[0]["override"]["reason"]


def test_undeclared_value_difference_fails(tmp_path):
    """Case 3b: a different date value on a model without an override -> one finding."""
    root = copy_fleet(tmp_path)
    f = definition(root, "spinout-10") / "expressions.tmdl"
    text = f.read_text(encoding="utf-8")
    assert "#date(2024, 1, 1)" in text
    f.write_text(text.replace("#date(2024, 1, 1)", "#date(2019, 6, 6)", 1), encoding="utf-8")
    r, rep = detect(root)
    assert [x["ref"] for x in findings(rep, "spinout-10", "expression.drift")] == ["v-fromdate"]
    assert r.exit_code == 1, r.output


def test_removing_the_override_makes_the_secondary_fail(tmp_path):
    root = copy_fleet(tmp_path)
    fy = root / "fleet.yml"
    text = fy.read_text(encoding="utf-8")
    start = text.index("    overrides:\n")
    end = text.index("  spinout-03:")
    fy.write_text(text[:start] + text[end:], encoding="utf-8")
    r, rep = detect(root)
    assert [x["ref"] for x in findings(rep, "spinout-02-performance", "expression.drift")] \
        == ["v-fromdate"], r.output


def test_meta_order_and_lineage_tags_are_not_drift(fleet_report):
    """Case 3a: spinout-02 writes the meta bag unspaced in another order, with its own tags."""
    _, rep = fleet_report
    assert not findings(rep, "spinout-02", "expression.drift")
