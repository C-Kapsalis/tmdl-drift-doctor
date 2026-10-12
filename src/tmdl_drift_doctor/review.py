"""The human review queue: everything the rules route to a person.

``drift-doctor review --out REVIEW.md`` runs detection plus a dry-run
remediation (``--sync`` semantics, nothing written) and writes a per-model
checklist of the findings no rule may decide on its own:

* blocked retirements (derived work still depends on a retired object),
* precondition skips (a fix that cannot run yet, and why),
* wrong-host and duplicate-host measures,
* advisories (derived-only objects on shared tables),
* overrides in force (declared divergence, listed for periodic re-review).

The doctrine: anything that recurs here is a missing rule. Encode the
decision once (an override, a ledger revival, an allowlist entry, a template
fix) instead of judging the same item again on every run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .detect import is_actionable
from .fleet import Fleet
from .remediate import remediate

DOCTRINE = ("Anything that recurs here is a missing rule: encode the decision "
            "once (an override, a ledger revival, an allowlist entry or a "
            "template fix) instead of judging it again on every run.")

SECTIONS = (
    ("blocked", "Blocked retirements"),
    ("precondition", "Fixes waiting on a precondition"),
    ("host", "Measures on the wrong or a duplicate host"),
    ("advisory", "Advisories: derived-only objects on shared tables"),
    ("overridden", "Overrides in force (re-review periodically)"),
)


def collect(fleet: Fleet, findings: list, baseline: dict) -> dict:
    """{model: {section: [(finding, note)]}}."""
    run = remediate(fleet, findings, dry_run=True, sync=True,
                    baseline=baseline)
    by_finding = {id(o.finding): o for o in run.outcomes}
    out: dict = {name: {key: [] for key, _ in SECTIONS} for name in fleet.models}
    for f in findings:
        o = by_finding.get(id(f))
        bucket = out.setdefault(f.model, {key: [] for key, _ in SECTIONS})
        if f.overridden:
            ov = f.override
            bucket["overridden"].append(
                (f, f"{ov.kind}: {ov.reason} [owner {ov.owner or '?'}, "
                    f"added {ov.added or '?'}]"))
        elif f.blocked:
            bucket["blocked"].append((f, "; ".join(f.blocked)))
        elif o is not None and o.status == "auto_skipped_precondition":
            bucket["precondition"].append((f, o.reason))
        elif f.kind in ("measure.wrong_host", "measure.duplicate_host"):
            note = (f"template host '{f.template_value}', found on "
                    f"'{f.derived_value}'")
            if f.kind == "measure.wrong_host" and not is_actionable(f, fleet):
                note += " (not allowlisted: move it by hand or allowlist the kind)"
            bucket["host"].append((f, note))
        elif f.advisory:
            bucket["advisory"].append((f, "keep it (a legitimate extension) or "
                                          "remove it by hand"))
    return out


def render(fleet: Fleet, queue: dict) -> str:
    lines = [
        "# Drift review queue",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"from a dry run (nothing was written).",
        "",
        f"> {DOCTRINE}",
        "",
        "Resolutions: (A) a `fleet.yml` override, (B) a ledger revival "
        "(`drift-doctor ledger --revive`), (C) an edit to the derived model, "
        "(D) a template fix, (E) a new allowlist entry.",
        "",
    ]
    total = 0
    for name in sorted(queue):
        sections = queue[name]
        n = sum(len(v) for v in sections.values())
        if not n:
            continue
        total += n
        gating = " (gating: advisory)" if fleet.is_advisory(name) else ""
        lines += [f"## {name}: {n} item(s){gating}", ""]
        for key, title in SECTIONS:
            items = sections.get(key) or []
            if not items:
                continue
            lines += [f"### {title}", ""]
            for f, note in items:
                lines.append(f"- [ ] `{f.kind}` {f.ref}")
                if note:
                    lines.append(f"  - {note}")
            lines.append("")
    if total == 0:
        lines += ["**Empty: the rules cover everything.**", ""]
    return "\n".join(lines)
