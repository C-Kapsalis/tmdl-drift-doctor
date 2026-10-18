"""Layer 1: file-level template exemptions (`overrides.files`) must actually
protect something.

Why this exists: a template cascade that copies template-owned files wholesale
makes any client edit to such a file transient by construction. A client's
conversion repoint was reverted that way by a bot sync with green checks. A
declared exemption that does not gate (a typo'd path, a path that stopped
resolving after a rename, a client copy that is identical again) produces
zero protection while still reading like protection.

Port: the original read `mirror_exempt` from the repo-root overrides YAML and
ran `scripts/ci/mirror_excludes_for.py`. Here the declarations are the fleet
entry's `overrides.files` (D-overrides-typed-sections; paths relative to the
model root, i.e. the folder holding `<Name>.SemanticModel`), the hygiene
checks are `drift-doctor overrides check` (B-override-hygiene) and the
exclude-list script is `drift-doctor overrides files` / `overrides is-exempt`.
The client folder lookup (`<id>_db` or `<id>`) is the fleet entry's path.

Output contract of `overrides check` the tests read (one line per problem,
naming the model and the path; exit 1 when any): an empty declaration ->
"empty path"; not a file under the template root -> "does not exist in the
template"; not a file under the model root -> "missing in the model"; client
copy byte-identical to the template's -> "byte-identical".

Not ported: test_workflow_consumes_the_exclude_list and the workflow half of
test_top_level_file_exemption_is_honoured (CI YAML text), and
test_the_cv1_exemptions_are_still_declared (pins one client's declarations).
"""
from __future__ import annotations

import pytest

from conftest import drift, write_fleet
from port_helpers_b2_l1b import load_scenario

pytestmark = [pytest.mark.p0, pytest.mark.layer1]

ITEM = "Shop.SemanticModel"

# scenario -> verdicts recorded from the ORIGINAL tests on the same data:
# (resolves in template, resolves in client, diverges, bare patterns, top-level)
VERDICTS = {
    "ok":             ("pass", "pass", "pass", "pass", "pass"),
    "identical":      ("pass", "pass", "fail", "pass", "pass"),
    "client_missing": ("pass", "fail", "pass", "pass", "pass"),
    "typo":           ("fail", "fail", "pass", "fail", "pass"),
    "empty":          ("fail", "fail", "pass", None, None),   # hygiene tests only
    "none":           ("pass", "pass", "pass", "skip", "skip"),
}


def _cases(i):
    return [(s, v[i]) for s, v in VERDICTS.items() if v[i] is not None]


def _fleet(stand_in, scenario):
    root, data = load_scenario(stand_in, "exempt", scenario)
    m = data["models"]["bike-shop"]
    entry = {"path": m["path"]}
    if m["files"]:
        entry["overrides"] = {"files": [
            {"path": p, "reason": "client-only edit the template cascade must not revert",
             "owner": "data-team", "added": "2026-09-04"} for p in m["files"]]}
    fy = write_fleet(root, data["template"], {"bike-shop": entry})
    return root, fy, m["files"]


def _hygiene(stand_in, scenario, verdict, needles, what):
    _, fy, _ = _fleet(stand_in, scenario)
    r = drift("overrides check", fleet=fy)
    assert r.exit_code in (0, 1), r.output
    bad = [ln.strip() for ln in r.output.splitlines() if any(n in ln for n in needles)]
    if verdict == "pass":
        assert not bad, f"{what}:\n  " + "\n  ".join(bad)
    else:
        assert bad and r.exit_code == 1, r.output
        assert all("bike-shop" in ln for ln in bad), r.output
    return bad


@pytest.mark.parametrize("scenario, verdict", _cases(0))
def test_every_exempt_path_resolves_in_universal(stand_in, scenario, verdict):
    """An exempt path must name a real template-owned file, or it protects
    nothing while looking like it does."""
    bad = _hygiene(stand_in, scenario, verdict, ("empty path", "does not exist in the template"),
                   "exempt paths must resolve under the template root")
    if scenario == "empty":
        assert any("empty path" in ln for ln in bad)
    if scenario == "typo":
        assert any("Store Order.tmdl" in ln for ln in bad)


@pytest.mark.parametrize("scenario, verdict", _cases(1))
def test_every_exempt_path_resolves_in_the_declaring_client(stand_in, scenario, verdict):
    """The point of the exemption is to preserve the CLIENT's copy, so that
    copy has to exist."""
    bad = _hygiene(stand_in, scenario, verdict, ("missing in the model", "empty path"),
                   "an exemption must name a file the model actually carries")
    if scenario == "client_missing":
        assert any("Store Orders.tmdl" in ln for ln in bad)


@pytest.mark.parametrize("scenario, verdict", _cases(2))
def test_exempt_paths_actually_diverge_from_universal(stand_in, scenario, verdict):
    """An exemption whose content is IDENTICAL to the template is dead weight:
    it stops the file receiving template changes while protecting nothing."""
    bad = _hygiene(stand_in, scenario, verdict, ("byte-identical",),
                   "these exempt files no longer diverge from the template (reverted "
                   "divergence, or a stale declaration)")
    if scenario == "identical":
        assert any("Store Orders.tmdl" in ln for ln in bad)
        assert not any("NOTES.md" in ln for ln in bad)


@pytest.mark.parametrize("scenario, verdict", _cases(3))
def test_script_emits_one_bare_pattern_per_line_for_a_declaring_client(stand_in, scenario, verdict):
    """Patterns feed a pattern file, so they must be bare and newline-separated
    — NOT quoted option arguments. Paths with a space must survive."""
    root, fy, declared = _fleet(stand_in, scenario)
    if not declared:
        pytest.skip("no file exemptions to exercise")
    path = declared[0]
    item = path.split("/", 1)[0]
    r = drift("overrides files", "--model", "bike-shop", "--item", item, fleet=fy)
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.stdout.split("\n") if ln.strip()]
    assert lines, f"bike-shop/{item} declares exemptions but none were listed"
    problems = []
    for ln in lines:
        if ln.startswith("--exclude") or ln.startswith("-"):
            problems.append(f"pattern must be bare, got: {ln!r}")
        if ln.startswith("'") or ln.endswith("'"):
            problems.append(f"pattern must not be shell-quoted, got: {ln!r}")
        if not (root / "universal" / item / ln).is_file():
            problems.append(f"listed pattern does not resolve under the template's {item}: {ln!r}")
    if verdict == "pass":
        assert not problems, "\n".join(problems)
        assert "definition/tables/Store Orders.tmdl" in lines
    else:
        assert problems
        assert any("Store Order.tmdl" in p for p in problems)


@pytest.mark.parametrize("scenario, verdict", _cases(4))
def test_top_level_file_exemption_is_honoured(stand_in, scenario, verdict):
    """A top-level template FILE is copied whole, so a pattern list cannot
    protect it: `is-exempt` must exit 0 for a declared file and 3 for an
    undeclared one. (The original's workflow half is not ported.)"""
    _, fy, declared = _fleet(stand_in, scenario)
    files = [p for p in declared if "/" not in p]
    if not files:
        pytest.skip("no top-level file exemptions declared")
    for path in files:
        rc = drift("overrides is-exempt", "--model", "bike-shop", path, fleet=fy).exit_code
        assert rc == 0, f"bike-shop: is-exempt {path} exited {rc}, expected 0"
    rc = drift("overrides is-exempt", "--model", "bike-shop", "no-such-file.md", fleet=fy).exit_code
    assert rc == 3, f"undeclared file must exit 3, got {rc}"
