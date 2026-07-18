"""The append-only JSONL ledger and its revival fold."""

import pytest

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")


def test_append_only_jsonl(ledger):
    ledger.record_retirement("measure", "T[M]")
    ledger.record_remediation("measure.missing", "alpha", "T[N]", "insert")
    entries = ledger.entries()
    assert [e["event"] for e in entries] == ["retired", "remediated"]
    assert all("ts" in e for e in entries)
    # append-only: a second write extends, never rewrites
    ledger.record_retirement("column", "T[C]")
    assert len(ledger.entries()) == 3


def test_invalid_retirement_kind_rejected(ledger):
    with pytest.raises(DriftDoctorError, match="invalid retirement kind"):
        ledger.record_retirement("visual", "whatever")


def test_global_revival_cancels_everywhere(ledger):
    ledger.record_retirement("measure", "T[M]")
    assert ledger.is_retired("measure", "T[M]")
    assert ledger.is_retired("measure", "T[M]", model="alpha")
    ledger.record_revival("measure", "T[M]")          # no model = fleet-wide
    assert not ledger.is_retired("measure", "T[M]")
    assert not ledger.is_retired("measure", "T[M]", model="alpha")


def test_model_scoped_revival_cancels_only_there(ledger):
    ledger.record_retirement("measure", "T[M]")
    ledger.record_revival("measure", "T[M]", model="alpha")
    assert not ledger.is_retired("measure", "T[M]", model="alpha")
    assert ledger.is_retired("measure", "T[M]", model="bravo")
    assert ledger.is_retired("measure", "T[M]")       # fleet view unchanged


def test_re_retirement_after_revival_is_in_force_again(ledger):
    ledger.record_retirement("measure", "T[M]")
    ledger.record_revival("measure", "T[M]")
    ledger.record_retirement("measure", "T[M]")
    assert ledger.is_retired("measure", "T[M]")


def test_malformed_ledger_fails_loudly(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"event": "retired"}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(DriftDoctorError, match="malformed ledger line"):
        Ledger(path).entries()


def test_empty_ledger_reads_empty(ledger):
    assert ledger.entries() == []
    assert ledger.active_retirements() == {}
