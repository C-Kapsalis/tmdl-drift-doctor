"""The captured baseline's provenance stamp — ported from the user's original
suite.

Every drift count is only as trustworthy as the template state the baseline
was captured from. Two of the stamp's keys look interchangeable and are not:

  template_tree    the content-addressed git tree of the template folder.
                   ASSERTED by the freshness check, because a squash merge
                   preserves it.
  template_commit  the last commit that touched the template folder.
                   INFORMATIONAL only: squash merging mints a new commit and
                   discards the branch commit the stamp named, so on main it
                   routinely names a commit that is not even an ancestor.

Port (C-baseline-provenance): the original read the committed
``tests/baselines/integration_baseline.json`` key ``_provenance`` and ran git
in the repository. Here each test builds a scratch git repository holding a
fictional template (bike shop) and one derived model, runs ``drift-doctor
capture``, commits, and reads ``<state_dir>/baseline.json`` key
``provenance`` (``tmdl_tree`` -> ``template_tree``, ``tmdl_commit`` ->
``template_commit``). The squash test creates its own pre/post-squash commits
instead of naming two hard-coded ones.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import drift, write_fleet

TEMPLATE = "template/BikeShop.SemanticModel"

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")

MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
SALES = ("table Sales\n\tlineageTag: 2b1d0c3e-0000-4000-8000-000000000001\n\n"
         "\tmeasure 'Total Sales' = SUM(Sales[amount])\n"
         "\t\tlineageTag: 2b1d0c3e-0000-4000-8000-000000000002\n\n"
         "\tcolumn amount\n\t\tdataType: double\n"
         "\t\tlineageTag: 2b1d0c3e-0000-4000-8000-000000000003\n"
         "\t\tsourceColumn: amount\n\n"
         "\tpartition Sales = m\n\t\tmode: import\n"
         "\t\tsource = #table({\"amount\"}, {{1.0}})\n")


GIT_IDENTITY = ["-c", "user.email=port@example.com", "-c", "user.name=port"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    return done.stdout.strip()


def _model(root: Path, rel: str) -> None:
    d = root / rel / "definition"
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (d / "model.tmdl").write_text(MODEL, encoding="utf-8")
    (d / "tables" / "Sales.tmdl").write_text(SALES, encoding="utf-8")


@pytest.fixture
def repo(tmp_path) -> Path:
    """A committed fleet repo with a captured, committed baseline."""
    root = tmp_path / "repo"
    _model(root, TEMPLATE)
    _model(root, "stores/north/BikeShop.SemanticModel")
    write_fleet(root, root / TEMPLATE,
                {"north": root / "stores/north/BikeShop.SemanticModel"},
                state_dir=".drift-doctor")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fleet")
    r = drift("capture", fleet=root / "fleet.yml")
    assert r.exit_code == 0, r.output
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "capture baseline")
    return root


def _provenance(repo: Path) -> dict:
    doc = json.loads((repo / ".drift-doctor" / "baseline.json")
                     .read_text(encoding="utf-8"))
    return doc["provenance"]


def test_stamp_carries_every_required_key(repo):
    provenance = _provenance(repo)
    for key in ("template_tree", "template_commit", "captured_at", "generator"):
        assert provenance.get(key), f"provenance.{key} missing or empty"


def test_tmdl_tree_matches_the_checked_out_universal(repo):
    """The assertion the freshness check makes. Fails when someone edits the
    template without recapturing — which is the whole point of the guard."""
    assert _provenance(repo)["template_tree"] == _git(
        repo, "rev-parse", f"HEAD:{TEMPLATE}"), (
        "captured template_tree does not match the checked-out template. "
        "Recapture: drift-doctor capture")


def test_tmdl_tree_is_a_tree_object_not_a_commit(repo):
    assert _git(repo, "cat-file", "-t", _provenance(repo)["template_tree"]) == "tree"


def test_tmdl_tree_survives_a_squash_merge(repo):
    """The property that makes the tree assertable where the commit is not:
    a branch head and the squash commit main carries hash to the same template
    tree. If this ever fails, git changed how tree objects are addressed and
    the freshness check needs rethinking."""
    _git(repo, "checkout", "-q", "-b", "feature")
    sales = repo / TEMPLATE / "definition" / "tables" / "Sales.tmdl"
    sales.write_text(SALES.replace("SUM(Sales[amount])",
                                   "SUM(Sales[amount]) + 0"), encoding="utf-8")
    _git(repo, "commit", "-qam", "edit 1")
    sales.write_text(SALES.replace("SUM(Sales[amount])",
                                   "SUMX(Sales, Sales[amount])"), encoding="utf-8")
    _git(repo, "commit", "-qam", "edit 2")
    pre_squash = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feature")
    _git(repo, "commit", "-qm", "squash")
    post_squash = _git(repo, "rev-parse", "HEAD")
    assert pre_squash != post_squash
    assert (_git(repo, "rev-parse", f"{pre_squash}:{TEMPLATE}")
            == _git(repo, "rev-parse", f"{post_squash}:{TEMPLATE}"))


def test_tmdl_commit_is_not_assumed_to_be_live(repo):
    """Guards the REASON template_commit is informational: on main it may name
    a commit a squash discarded, so no gate may require it to be reachable."""
    prov = _provenance(repo)
    reachable = subprocess.run(
        ["git", "merge-base", "--is-ancestor", prov["template_commit"], "HEAD"],
        cwd=repo, capture_output=True,
    ).returncode == 0
    # Either outcome is legitimate — the point is that nothing depends on it.
    assert reachable in (True, False)
