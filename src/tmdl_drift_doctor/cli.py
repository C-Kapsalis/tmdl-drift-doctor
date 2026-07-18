"""drift-doctor — the command-line interface.

    drift-doctor capture   --fleet fleet.yml
    drift-doctor detect    --fleet fleet.yml [--json] [--model NAME] [--kind PFX]
    drift-doctor remediate --fleet fleet.yml [--kind PFX] [--dry-run] [--sync]
    drift-doctor ledger    --fleet fleet.yml [--json]
    drift-doctor ledger    --fleet fleet.yml --revive REF --kind KIND [--model NAME]
"""

from __future__ import annotations

import json
import sys

import click

from . import DriftDoctorError, StaleBaselineError, __version__
from .baseline import capture as run_capture
from .baseline import ensure_baseline_fresh, load_baseline
from .detect import detect_fleet
from .fleet import load_fleet
from .ledger import RETIREMENT_KINDS, Ledger
from .remediate import remediate as run_remediate


def _fleet_option(fn):
    return click.option("--fleet", "fleet_path", default="fleet.yml",
                        show_default=True,
                        help="Path to the fleet configuration.")(fn)


def _load(fleet_path):
    try:
        return load_fleet(fleet_path)
    except DriftDoctorError as e:
        raise click.ClickException(str(e))


def _fresh_baseline(fleet, allow_stale: bool):
    try:
        baseline = load_baseline(fleet)
        if not allow_stale:
            ensure_baseline_fresh(fleet, baseline)
        return baseline
    except StaleBaselineError as e:
        raise click.ClickException(str(e))


@click.group()
@click.version_option(__version__, prog_name="drift-doctor")
def main() -> None:
    """Drift detection and remediation for TMDL semantic-model fleets."""
    # Model content (and our own rules/ellipses) can exceed the console's
    # charset on legacy Windows code pages — degrade to '?' instead of
    # crashing mid-report.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover — exotic streams
                pass


@main.command()
@_fleet_option
def capture(fleet_path) -> None:
    """Snapshot the template into the baseline; record retirements/revivals.

    Run this THE MOMENT the template changes — detection and remediation both
    refuse to act on a stale baseline."""
    fleet = _load(fleet_path)
    summary = run_capture(fleet)
    click.echo(f"baseline written: {summary['baseline']}")
    click.echo(f"  tables={summary['tables']} columns={summary['columns']} "
               f"measures={summary['measures']} "
               f"mapping_rows={summary['mapping_rows']} "
               f"expressions={summary['expressions']}")
    for e in summary["retired"]:
        click.echo(f"  [ledger] retired {e['kind']}: {e['ref']}")
    for e in summary["revived"]:
        click.echo(f"  [ledger] revived {e['kind']}: {e['ref']} "
                   f"(returned to the template)")


@main.command()
@_fleet_option
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--kind", "kind_filter", default=None,
              help="Only findings whose kind starts with this prefix.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed even if the template changed since the last "
                   "capture (misclassification risk — see the docs).")
def detect(fleet_path, as_json, model_filter, kind_filter, allow_stale) -> None:
    """Detect drift in every derived model. Read-only.

    Exit code 1 when remediable findings exist (advisories alone exit 0) —
    wire it straight into CI."""
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    findings = detect_fleet(fleet, baseline, Ledger(fleet.ledger_path),
                            model_filter=model_filter, kind_filter=kind_filter)
    if as_json:
        click.echo(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        if not findings:
            scope = []
            if model_filter:
                scope.append(f"model '{model_filter}'")
            if kind_filter:
                scope.append(f"kind prefix '{kind_filter}'")
            if scope:
                click.echo(f"no drift detected within {' and '.join(scope)} "
                           f"— other findings may exist outside this filter.")
            else:
                click.echo("no drift detected — the fleet matches the template.")
        by_model: dict = {}
        for f in findings:
            by_model.setdefault(f.model, []).append(f)
        for model, items in by_model.items():
            click.echo(f"\n── {model}: {len(items)} finding(s)")
            for f in items:
                tag = "  [advisory]" if f.advisory else ""
                click.echo(f"  {f.kind:<28} {f.ref}{tag}")
                if f.template_value is not None or f.derived_value is not None:
                    click.echo(f"      template: {f.template_value}")
                    click.echo(f"      derived:  {f.derived_value}")
    if any(not f.advisory for f in findings):
        sys.exit(1)


@main.command()
@_fleet_option
@click.option("--kind", default=None,
              help="Only remediate findings whose kind starts with this prefix.")
@click.option("--dry-run", is_flag=True,
              help="Compute every edit and print unified diffs; write nothing "
                   "(not even the ledger).")
@click.option("--sync", is_flag=True,
              help="Also apply retire.* removals (deletions of provably "
                   "ex-template objects). Never on by default.")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed on a stale baseline (misclassification risk).")
def remediate(fleet_path, kind, dry_run, sync, model_filter, allow_stale) -> None:
    """Cascade template truth into the derived models (allowlist-gated)."""
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    findings = detect_fleet(fleet, baseline, Ledger(fleet.ledger_path),
                            model_filter=model_filter)
    result = run_remediate(fleet, findings, kind=kind, dry_run=dry_run,
                           sync=sync)
    for o in result.outcomes:
        f = o.finding
        if o.status in ("applied", "would-apply"):
            verb = "would apply" if o.status == "would-apply" else "applied"
            click.echo(f"[{verb}] {f.model}: {f.kind} {f.ref} — {o.reason}")
            if dry_run:
                for c in o.changes:
                    click.echo(c.diff())
        elif o.status == "failed":
            click.echo(f"[FAILED] {f.model}: {f.kind} {f.ref} — {o.reason}",
                       err=True)
        else:
            click.echo(f"[skipped] {f.model}: {f.kind} {f.ref} — {o.reason}")
    click.echo(f"\n── summary: applied={result.count('applied')} "
               f"would-apply={result.count('would-apply')} "
               f"skipped={result.count('skipped')} "
               f"failed={result.count('failed')}")
    if result.count("failed"):
        sys.exit(1)


@main.command()
@_fleet_option
@click.option("--json", "as_json", is_flag=True, help="Raw JSONL entries.")
@click.option("--revive", "revive_ref", default=None, metavar="REF",
              help="Cancel a retirement for REF (e.g. \"Visits[Peak Hour "
                   "Visits]\") so it is never re-removed. Requires --kind.")
@click.option("--kind", default=None,
              help=f"Retirement kind for --revive: one of {RETIREMENT_KINDS}.")
@click.option("--model", default=None,
              help="Scope the revival to one derived model (default: fleet-wide).")
@click.option("--note", default="", help="Reason recorded with the revival.")
def ledger(fleet_path, as_json, revive_ref, kind, model, note) -> None:
    """Show the remediation ledger, or record a manual revival."""
    fleet = _load(fleet_path)
    led = Ledger(fleet.ledger_path)

    if revive_ref:
        if kind not in RETIREMENT_KINDS:
            raise click.ClickException(
                f"--revive requires --kind with the retirement kind of "
                f"{revive_ref!r}. Valid kinds: {', '.join(RETIREMENT_KINDS)}.")
        if not led.is_retired(kind, revive_ref, model):
            raise click.ClickException(
                f"no active retirement found for {kind} {revive_ref!r}"
                + (f" in model '{model}'" if model else "")
                + ". Run `drift-doctor ledger` to list retirement events and "
                  "check the reference and kind — this usually means a typo.")
        e = led.record_revival(kind, revive_ref, model=model, note=note)
        scope = f"model '{model}'" if model else "the whole fleet"
        click.echo(f"revived {kind} {revive_ref} for {scope} — it will not "
                   f"be re-removed.")
        return

    entries = led.entries()
    if as_json:
        for e in entries:
            click.echo(json.dumps(e, ensure_ascii=False))
        return
    if not entries:
        click.echo("ledger is empty.")
        return
    for e in entries:
        parts = [e.get("ts", "?"), e.get("event", "?"), e.get("kind", "")]
        if e.get("model"):
            parts.append(f"model={e['model']}")
        parts.append(e.get("ref", ""))
        if e.get("action"):
            parts.append(f"— {e['action']}")
        if e.get("note"):
            parts.append(f"({e['note']})")
        click.echo("  ".join(p for p in parts if p))


if __name__ == "__main__":
    main()
