"""retire.mapping_row — single-row z-Map retirement from a SURVIVING category.

The template drops one row of a category (SEM's spend_facebook_z_slicers)
while the category itself stays. The whole-category channel
(retire.mapping_category) deliberately cannot express this — its revival
guard keys on category presence and its leftovers rule treats surviving
sibling rows as human-in-the-loop. This kind is the surgical inverse: drop
ONLY the ledger-listed key(s), keep the siblings, with row-level revival.

Two tiers, mirroring the rest of the retirement channel:
  - units (recapture ledger maintenance + loader + codec),
  - a mini-model pass through detect + remediate(sync=True) (the removal
    contract end-to-end).

Ported from the user's suite (D-encoded-mapping-row-retirement). The kernel
ledger helpers (inventory, recapture update, loader) become drift-doctor
``capture`` and its JSONL ledger: each original snapshot pair is a template
z-Map before and after a recapture. Original kinds ``zmap_row`` /
``zmap_category`` / ``retire.zmap_row`` -> ``mapping_row`` /
``mapping_category`` / ``retire.mapping_row``. The remediation engine calls
(build_context / collect / run_batch) become detect + ``remediate``; the
client_overrides document becomes a ``mapping_rows`` override section.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from port_helpers_b7_retire import (ZMAP_TABLE, capture, copy_pair, detect,
                                    detect_and_apply, git_commit_all,
                                    install_zmap, ledger_entries, make_fleet,
                                    read_zmap_rows, template_tree, zmap_file)

from tmdl_drift_doctor import DriftDoctorError

KIND = "retire.mapping_row"
KINDS = ["retire.mapping_row", "retire.mapping_category"]

# The retired row + its category siblings. Category 'Performance' SURVIVES in
# the template; only FB_KEY is retired from it.
FB_KEY = "spend_facebook_z_slicers"
SIBLINGS = ["Performance_x", "Date_y", "Blank Dimension_y", "perf_dims_y"]
UNI_ROWS = ([["Performance", "", k] for k in SIBLINGS]
            + [["Directory", "", "Account_y"], ["Directory", "", "Date_y"]])
# what the model still carries: the template's rows + the not-yet-dropped row
CLIENT_ROWS = UNI_ROWS + [["Performance", "", FB_KEY]]


def _rows_from_categories(cats: dict) -> list:
    return [[c, "", k] for c, keys in cats.items() for k in keys]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ─────────────────────────── harness ───────────────────────────

def _fleet(tmp_path, *, overrides=None):
    uni, gro = copy_pair(tmp_path)
    entry = {"path": gro}
    if overrides:
        entry["overrides"] = overrides
    return uni, gro, make_fleet(tmp_path, uni, {"fixture_growth": entry},
                                kinds=KINDS)


def _recapture(tmp_path, prev: dict, fresh: dict, *, commit=False):
    """Template z-Map = `prev`, capture; then = `fresh`, capture again.
    Returns (fleet.yml, template dir, new ledger events of the 2nd capture)."""
    uni, gro, fy = _fleet(tmp_path)
    install_zmap(uni, _rows_from_categories(prev))
    install_zmap(gro, _rows_from_categories(prev))
    capture(fy)
    n = len(ledger_entries(fy))
    install_zmap(uni, _rows_from_categories(fresh))
    if commit:
        git_commit_all(tmp_path, "c1")
    capture(fy)
    return fy, uni, ledger_entries(fy)[n:]


def _retired(events, kind=None):
    return [e for e in events if e.get("event") == "retired"
            and (kind is None or e.get("kind") == kind)]


def _copy_pair_with_retired_row(tmp_path, *, overrides=None):
    """Template and model start with CLIENT_ROWS; the template then drops
    FB_KEY and is recaptured, which records the row retirement in the ledger
    (the original seeded the same entry into a YAML ledger)."""
    uni, gro, fy = _fleet(tmp_path, overrides=overrides)
    install_zmap(uni, CLIENT_ROWS)
    install_zmap(gro, CLIENT_ROWS)
    capture(fy)
    install_zmap(uni, UNI_ROWS)
    capture(fy)
    return fy, uni, gro


# ─────────────────────────── units ───────────────────────────

def test_recapture_records_surviving_category_row(tmp_path):
    """A row dropped from a category that SURVIVES is recorded as a row
    retirement (not a category one), with structured category + row_key."""
    prev = {"SEM": SIBLINGS + [FB_KEY], "Facebook": [FB_KEY]}
    fresh = {"SEM": SIBLINGS, "Facebook": [FB_KEY]}
    _fy, _uni, events = _recapture(tmp_path, prev, fresh)
    rows = _retired(events, "mapping_row")
    assert {f"{e['category']}/{e['row_key']}" for e in rows} == {f"SEM/{FB_KEY}"}
    assert _retired(events, "mapping_category") == []          # SEM stays


def test_recapture_append_idempotent_and_revival(tmp_path):
    prev = {"SEM": SIBLINGS + [FB_KEY]}
    fresh = {"SEM": SIBLINGS}
    fy, uni, r1 = _recapture(tmp_path, prev, fresh, commit=True)
    assert [(e["kind"], e["category"], e["row_key"]) for e in _retired(r1)] \
        == [("mapping_row", "SEM", FB_KEY)]
    # idempotent: the same diff appends nothing
    n = len(ledger_entries(fy))
    capture(fy)
    assert _retired(ledger_entries(fy)[n:]) == []
    # row-level revival: the exact row returns to the template
    install_zmap(uni, _rows_from_categories(prev))
    git_commit_all(tmp_path, "c2")
    c2 = template_tree(uni)
    n = len(ledger_entries(fy))
    capture(fy)
    revived = [e for e in ledger_entries(fy)[n:] if e.get("event") == "revived"]
    assert [(e["kind"], c2 in e.values()) for e in revived] \
        == [("mapping_row", True)]


def test_recapture_whole_category_drop_not_double_counted(tmp_path):
    """When the WHOLE category disappears, only a category retirement is
    recorded — never per-row entries for the same rows."""
    prev = {"Email Marketing": ["a_x", "b_x"], "SEM": SIBLINGS}
    fresh = {"SEM": SIBLINGS}
    _fy, _uni, events = _recapture(tmp_path, prev, fresh)
    assert sorted(e["kind"] for e in _retired(events)) == ["mapping_category"]


def test_loader_rejects_zmap_row_missing_target(tmp_path):
    from tmdl_drift_doctor.ledger import Ledger
    bad = tmp_path / "ledger.jsonl"
    bad.write_text(json.dumps({
        "ts": "2026-07-17T00:00:00+00:00", "event": "retired",
        "kind": "mapping_row", "ref": f"{ZMAP_TABLE}/SEM/x",
        "source": "capture"}) + "\n", encoding="utf-8")
    with pytest.raises(DriftDoctorError, match="row_key"):
        Ledger(bad).entries()


def test_codec_removes_exact_row_keeps_siblings():
    import importlib
    mc = importlib.import_module("tmdl_drift_doctor.mapping_codec")
    new_rows, removed, leftovers = mc.remove_category_rows(
        CLIENT_ROWS, "Performance", {FB_KEY})
    assert [r[2] for r in removed] == [FB_KEY]
    assert FB_KEY not in [r[2] for r in new_rows if r[0] == "Performance"]
    assert {r[2] for r in leftovers} == set(SIBLINGS)  # every sibling survives


# ───────────────────────── mini integration ─────────────────────────

def test_sync_removes_exact_row_keeps_siblings(tmp_path):
    fy, uni, gro = _copy_pair_with_retired_row(tmp_path)

    items = detect(fy, KIND)
    assert len(items) == 1, [f.kind for f in detect(fy)]

    _findings, result = detect_and_apply(fy)
    assert result.count("applied") >= 1 and result.count("failed") == 0

    rows = read_zmap_rows(gro)
    perf_keys = [r[2] for r in rows if r[0] == "Performance"]
    assert FB_KEY not in perf_keys                 # target dropped
    assert set(SIBLINGS) <= set(perf_keys)          # siblings intact
    assert ["Account_y", "Date_y"] == sorted(       # other category untouched
        r[2] for r in rows if r[0] == "Directory")


def test_sync_is_idempotent_second_run_noop(tmp_path):
    fy, uni, gro = _copy_pair_with_retired_row(tmp_path)
    detect_and_apply(fy)
    after_first = _sha(zmap_file(gro))

    assert detect(fy, KIND) == []                   # nothing left to remove
    detect_and_apply(fy)
    assert _sha(zmap_file(gro)) == after_first      # byte-identical


def test_row_present_in_universal_is_revival_not_removed(tmp_path):
    # the live template STILL carries the row (not retired) -> row-level revival
    fy, uni, gro = _copy_pair_with_retired_row(tmp_path)
    install_zmap(uni, CLIENT_ROWS)
    assert detect(fy, KIND) == []
    # and the model's row is left in place
    assert FB_KEY in [r[2] for r in read_zmap_rows(gro)
                      if r[0] == "Performance"]


def test_client_override_retains_row(tmp_path):
    fy, uni, gro = _copy_pair_with_retired_row(tmp_path, overrides={
        "mapping_rows": [{"category": "Performance", "key": FB_KEY,
                          "reason": "client wired FB toggle on a blended SEM page"}]})
    assert detect(fy, KIND) == []                   # override wins
    detect_and_apply(fy)
    assert FB_KEY in [r[2] for r in read_zmap_rows(gro)
                      if r[0] == "Performance"]      # kept
