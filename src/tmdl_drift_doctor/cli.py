"""drift-doctor: the command-line interface.

    drift-doctor capture   --fleet fleet.yml
    drift-doctor detect    --fleet fleet.yml [--json] [--model NAME] [--kind PFX]
    drift-doctor remediate --fleet fleet.yml [--kind PFX] [--dry-run] [--sync] [--converge]
    drift-doctor ratchet   check|tighten --fleet fleet.yml [--from-json FILE]
    drift-doctor audit     --fleet fleet.yml [--model NAME] [--json]
    drift-doctor review    --fleet fleet.yml [--out REVIEW.md]
    drift-doctor ledger    --fleet fleet.yml [--json]
    drift-doctor ledger    --fleet fleet.yml --revive REF --kind KIND [--model NAME]

    drift-doctor fleet     check|connection|family --fleet fleet.yml
    drift-doctor overrides check|files|is-exempt --fleet fleet.yml

Exit codes: 0 success / clean; 1 actionable drift, a ratchet violation or a
failed fix; 2 a usage or configuration error (including a report the
ratchet refuses to trust); 3 a model that could not be parsed; 4 audit
found a reverted fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import DriftDoctorError, StaleBaselineError, __version__
from .baseline import capture as run_capture
from .baseline import ensure_baseline_fresh, load_baseline
from .detect import build_report, detect_fleet, is_actionable, is_gating
from .fleet import load_fleet
from .ledger import RETIREMENT_KINDS, Ledger
from .overrides import format_ack
from .remediate import remediate as run_remediate
from .remediate import remediate_converge

PARSE_ERROR_EXIT = 3
USAGE_EXIT = 2


class ConfigError(click.ClickException):
    """A usage or configuration problem: exit 2, nothing was acted on."""
    exit_code = USAGE_EXIT


def _fleet_option(fn):
    return click.option("--fleet", "fleet_path", default="fleet.yml",
                        show_default=True,
                        help="Path to the fleet configuration.")(fn)


def _load(fleet_path):
    try:
        return load_fleet(fleet_path)
    except DriftDoctorError as e:
        raise ConfigError(str(e))


def _fresh_baseline(fleet, allow_stale: bool):
    try:
        baseline = load_baseline(fleet)
        if not allow_stale:
            ensure_baseline_fresh(fleet, baseline)
        return baseline
    except StaleBaselineError as e:
        raise click.ClickException(str(e))


def _detect(fleet, baseline, errors=None, **kw):
    try:
        return detect_fleet(fleet, baseline, Ledger(fleet.ledger_path),
                            errors=errors, **kw)
    except DriftDoctorError as e:
        raise ConfigError(str(e))


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
               f"relationships={summary['relationships']} "
               f"mapping_rows={summary['mapping_rows']} "
               f"expressions={summary['expressions']}")
    for e in summary["retired"]:
        click.echo(f"  [ledger] retired {e['kind']}: {e['ref']}")
    for e in summary["revived"]:
        click.echo(f"  [ledger] revived {e['kind']}: {e['ref']} "
                   f"(returned to the template)")


@main.command()
@_fleet_option
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable report (schema version 2).")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--kind", "kind_filter", default=None,
              help="Only findings whose kind starts with this prefix.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed even if the template changed since the last "
                   "capture (misclassification risk — see the docs).")
def detect(fleet_path, as_json, model_filter, kind_filter, allow_stale) -> None:
    """Detect drift in every derived model. Read-only.

    Exit code 1 when actionable findings exist (allowlisted, not advisory,
    not overridden, on a model whose gating is not advisory); 3 when a model
    could not be parsed. Wire it straight into CI."""
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    errors: list = []
    findings = _detect(fleet, baseline, errors, model_filter=model_filter,
                       kind_filter=kind_filter)
    if as_json:
        click.echo(json.dumps(build_report(fleet, findings, errors,
                                           model_filter, kind_filter),
                              indent=2, ensure_ascii=False))
    else:
        _print_findings(fleet, findings, errors, model_filter, kind_filter)
    if errors:
        sys.exit(PARSE_ERROR_EXIT)
    if any(is_gating(f, fleet) for f in findings):
        sys.exit(1)


def _print_findings(fleet, findings, errors, model_filter, kind_filter):
    shown = [f for f in findings if not f.overridden]
    if not shown and not errors:
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
        visible = [f for f in items if not f.overridden]
        acks = [f for f in items if f.overridden]
        gating = "  (gating: advisory)" if fleet.is_advisory(model) else ""
        click.echo(f"\n── {model}: {len(visible)} finding(s){gating}")
        for f in visible:
            tags = []
            if f.advisory:
                tags.append("[advisory]")
            elif not fleet.allows(f.kind):
                tags.append("[not allowlisted]")
            if f.blocked:
                tags.append("[blocked]")
            tag = ("  " + " ".join(tags)) if tags else ""
            click.echo(f"  {f.kind:<28} {f.ref}{tag}")
            if f.template_value is not None or f.derived_value is not None:
                click.echo(f"      template: {f.template_value}")
                click.echo(f"      derived:  {f.derived_value}")
            for reason in f.blocked:
                click.echo(f"      blocked:  {reason}")
        if acks:
            click.echo(f"  declared divergence honored (overrides): "
                       f"{len(acks)} item(s)")
            for f in acks:
                click.echo(format_ack(f, f.override))
    for e in errors:
        click.echo(f"\n[ERROR] {e['model']}: {e['error']}", err=True)
    actionable = sum(1 for f in findings if is_actionable(f, fleet))
    click.echo(f"\n── summary: actionable={actionable} "
               f"advisory={sum(1 for f in findings if f.advisory)} "
               f"overridden={sum(1 for f in findings if f.overridden)} "
               f"errors={len(errors)}")


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
@click.option("--converge", is_flag=True,
              help="Re-detect and re-apply until a pass changes nothing "
                   "(at most 4 passes).")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed on a stale baseline (misclassification risk).")
def remediate(fleet_path, kind, dry_run, sync, converge, model_filter,
              allow_stale) -> None:
    """Cascade template truth into the derived models (allowlist-gated)."""
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    detect_once = lambda: _detect(fleet, baseline, model_filter=model_filter)
    kw = dict(kind=kind, dry_run=dry_run, sync=sync, baseline=baseline)
    if converge:
        results = remediate_converge(fleet, detect_once, **kw)
    else:
        results = [run_remediate(fleet, detect_once(), **kw)]
    if converge and dry_run:
        click.echo("note: --converge has no effect with --dry-run (nothing "
                   "changes on disk between passes); showing one pass.")
    failed = 0
    for n, result in enumerate(results, 1):
        if len(results) > 1:
            click.echo(f"\n══ pass {n}")
        _print_outcomes(result, dry_run)
        failed += result.count("failed")
    if converge and not dry_run:
        last = results[-1]
        state = ("converged" if last.count("applied") == 0
                 else f"stopped after {len(results)} pass(es) without "
                      f"converging; run again")
        click.echo(f"── {state}")
    if failed:
        sys.exit(1)


def _print_outcomes(result, dry_run):
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
        elif o.status == "blocked":
            click.echo(f"[BLOCKED] {f.model}: {f.kind} {f.ref} — {o.reason}")
        elif o.status == "auto_skipped_precondition":
            click.echo(f"[precondition] {f.model}: {f.kind} {f.ref} — "
                       f"{o.reason}")
        elif o.status == "overridden":
            click.echo(f"[ACK] {f.model}: {f.kind} {f.ref} — {o.reason}")
        else:
            click.echo(f"[skipped] {f.model}: {f.kind} {f.ref} — {o.reason}")
    click.echo(f"\n── summary: applied={result.count('applied')} "
               f"would-apply={result.count('would-apply')} "
               f"skipped={result.count('skipped')} "
               f"blocked={result.count('blocked')} "
               f"auto_skipped_precondition="
               f"{result.count('auto_skipped_precondition')} "
               f"overridden={result.count('overridden')} "
               f"failed={result.count('failed')}")


# ───────────────────────────── ratchet ─────────────────────────────

@main.group()
def ratchet() -> None:
    """Fail CI when drift rises above the recorded per-model floors."""


def _ratchet_report(fleet, from_json, allow_stale):
    if from_json:
        try:
            return json.loads(Path(from_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            click.echo(f"ERROR: cannot read {from_json}: {e}", err=True)
            sys.exit(2)
    baseline = _fresh_baseline(fleet, allow_stale)
    errors: list = []
    findings = _detect(fleet, baseline, errors)
    return build_report(fleet, findings, errors)


def _refuse(problems):
    for p in problems:
        click.echo(f"ERROR: {p}", err=True)
    sys.exit(2)


_from_json = click.option(
    "--from-json", default=None, metavar="FILE",
    help="Use a saved `detect --json` report instead of detecting now.")
_allow_stale = click.option("--allow-stale", is_flag=True,
                            help="Detect on a stale baseline.")


@ratchet.command("check")
@_fleet_option
@_from_json
@_allow_stale
def ratchet_check(fleet_path, from_json, allow_stale) -> None:
    """Exit 1 if any model's actionable findings (total or per kind) rose
    above its floor, or any retired object came back."""
    from .ratchet import check, load_floors, report_problems
    fleet = _load(fleet_path)
    report = _ratchet_report(fleet, from_json, allow_stale)
    problems = report_problems(report, writing=False)
    if problems:
        _refuse(problems)
    try:
        floors = load_floors(fleet.floors_path)
    except DriftDoctorError as e:
        _refuse([str(e)])
    if floors is None:
        _refuse([f"no floors file at {fleet.floors_path}; seed it with "
                 f"`drift-doctor ratchet tighten` and commit it"])
    res = check(report, floors, fleet.gating)
    for line in res.violations:
        click.echo(f"[RISE] {line}")
    if res.advisories:
        click.echo("advisory models (reported, never fail the check):")
        for line in res.advisories:
            click.echo(f"  {line}")
    for line in res.improved:
        click.echo(f"[improved] {line} (lower the floor with `ratchet "
                   f"tighten`)")
    for line in res.notes:
        click.echo(f"note: {line}")
    if res.ok:
        click.echo("ratchet: every gated model is at or below its floor.")
    else:
        sys.exit(1)


@ratchet.command("tighten")
@_fleet_option
@_from_json
@_allow_stale
@click.option("--allow-new", is_flag=True,
              help="Record models that have no floor yet at their current "
                   "counts.")
@click.option("--prune", is_flag=True,
              help="Drop floors of models no longer in fleet.yml.")
def ratchet_tighten(fleet_path, from_json, allow_stale, allow_new, prune) -> None:
    """Lower every floor to min(floor, current). Never raises a floor.
    Only a full-fleet, unfiltered, error-free report is accepted."""
    from .ratchet import load_floors, report_problems, save_floors, tighten
    fleet = _load(fleet_path)
    report = _ratchet_report(fleet, from_json, allow_stale)
    problems = report_problems(report, writing=True)
    if problems:
        _refuse(problems)
    try:
        floors = load_floors(fleet.floors_path)
    except DriftDoctorError as e:
        _refuse([str(e)])
    doc, changes = tighten(report, floors, allow_new=allow_new, prune=prune,
                           fleet_models=list(fleet.models))
    for line in changes:
        click.echo(f"  {line}")
    if floors is None or doc["models"] != floors.get("models"):
        save_floors(fleet.floors_path, doc)
        click.echo(f"floors written: {fleet.floors_path}")
    else:
        click.echo("floors unchanged.")


# ───────────────────────────── audit / review ─────────────────────────────

@main.command()
@_fleet_option
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def audit(fleet_path, model_filter, as_json) -> None:
    """Re-verify every applied fix in the ledger. Exit 4 if one was reverted
    (an added object is gone, or a removed one is back)."""
    from .audit import REVERT_EXIT_CODE
    from .audit import audit as run_audit
    fleet = _load(fleet_path)
    try:
        baseline = load_baseline(fleet)
        items = run_audit(fleet, Ledger(fleet.ledger_path), baseline,
                          model_filter=model_filter)
    except DriftDoctorError as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps([{"status": i.status, "detail": i.detail,
                                **{k: i.event.get(k) for k in
                                   ("ts", "model", "kind", "ref")}}
                               for i in items], indent=2, ensure_ascii=False))
    else:
        for i in items:
            e = i.event
            if i.status == "ok":
                continue
            label = {"reverted": "REVERTED", "error": "ERROR"}.get(
                i.status, i.status)
            click.echo(f"[{label}] {e.get('model')}: {e.get('kind')} "
                       f"{e.get('ref')} (applied {e.get('ts')}) — {i.detail}")
        counts = {s: sum(1 for i in items if i.status == s)
                  for s in ("ok", "reverted", "superseded", "error")}
        click.echo(f"── audit: checked={len(items)} "
                   + " ".join(f"{k}={v}" for k, v in counts.items()))
    if any(i.status == "reverted" for i in items):
        sys.exit(REVERT_EXIT_CODE)
    if any(i.status == "error" for i in items):
        sys.exit(PARSE_ERROR_EXIT)


@main.command()
@_fleet_option
@click.option("--out", default="REVIEW.md", show_default=True,
              help="Where to write the checklist.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed on a stale baseline (misclassification risk).")
def review(fleet_path, out, allow_stale) -> None:
    """Write a per-model checklist of everything routed to a human."""
    from .review import collect, render
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    findings = _detect(fleet, baseline)
    queue = collect(fleet, findings, baseline)
    Path(out).write_text(render(fleet, queue), encoding="utf-8")
    total = sum(len(v) for sections in queue.values()
                for v in sections.values())
    click.echo(f"{total} review item(s) -> {out}")


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
        led.record_revival(kind, revive_ref, model=model, note=note)
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


# ───────────────────────────── fleet ─────────────────────────────

@main.group("fleet")
def fleet_group() -> None:
    """Validate fleet.yml and answer questions about its models."""


@fleet_group.command("check")
@_fleet_option
@click.option("--stamps", "stamps_path", default=None, metavar="FILE",
              help="A connection-parameter stamping table (YAML keyed by "
                   "model folder) to check against the fleet's declarations.")
def fleet_check(fleet_path, stamps_path) -> None:
    """Validate fleet.yml, the per-model overlay files and (with --stamps) a
    stamping table. Exit 2 on a configuration problem, 1 when the stamping
    table disagrees with the fleet, else 0."""
    import yaml
    from .fleet import overlay_problems, stamp_problems
    fleet = _load(fleet_path)
    problems = overlay_problems(fleet)
    for line in problems:
        click.echo(line)
    if problems:
        sys.exit(USAGE_EXIT)
    if stamps_path:
        try:
            stamps = yaml.safe_load(Path(stamps_path).read_text(
                encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot read {stamps_path}: {e}")
        bad = stamp_problems(fleet, stamps)
        for line in bad:
            click.echo(line)
        if bad:
            sys.exit(1)
    click.echo(f"fleet.yml is valid: {len(fleet.models)} model(s).")


@fleet_group.command("connection")
@_fleet_option
@click.argument("model")
def fleet_connection(fleet_path, model) -> None:
    """Print PROJECT<TAB>BILLING<TAB>DATASET for MODEL (always three fields;
    an empty project means "keep the template's default")."""
    fleet = _load(fleet_path)
    if model not in fleet.models:
        raise ConfigError(f"unknown model '{model}'; fleet.yml defines: "
                          f"{', '.join(fleet.models)}")
    click.echo("\t".join(fleet.connection(model)))


@fleet_group.command("family")
@_fleet_option
@click.argument("family")
def fleet_family(fleet_path, family) -> None:
    """Print the ids of FAMILY's members, sorted and comma-separated. An
    empty family is an error (exit 1): a zero must never pass silently."""
    from .families import members
    fleet = _load(fleet_path)
    names = members(fleet, family)
    if not names:
        click.echo(f"no fleet model belongs to the '{family}' family",
                   err=True)
        sys.exit(1)
    click.echo(",".join(names))


# ───────────────────────────── overrides ─────────────────────────────

@main.group("overrides")
def overrides_group() -> None:
    """Inspect declared divergence (fleet.yml `overrides`)."""


def _known_model(fleet, model):
    if model not in fleet.models:
        raise ConfigError(f"unknown model '{model}'; fleet.yml defines: "
                          f"{', '.join(fleet.models)}")


@overrides_group.command("check")
@_fleet_option
def overrides_check(fleet_path) -> None:
    """Check that every file exemption protects something: it resolves in
    the template and in the declaring model, and the model's copy still
    differs. Exit 1 when any declaration is stale."""
    from .hygiene import file_exemption_problems
    fleet = _load(fleet_path)
    problems = file_exemption_problems(fleet)
    for line in problems:
        click.echo(line)
    if problems:
        sys.exit(1)
    click.echo("every override still protects something.")


@overrides_group.command("files")
@_fleet_option
@click.option("--model", required=True, help="The declaring model.")
@click.option("--item", default=None,
              help="Only exemptions under this top-level item (for example "
                   "Shop.SemanticModel), printed relative to it.")
def overrides_files(fleet_path, model, item) -> None:
    """Print the model's exempt file patterns, one bare pattern per line."""
    fleet = _load(fleet_path)
    _known_model(fleet, model)
    for pat in fleet.exempt_patterns(model):
        pat = (pat or "").strip()
        if not pat:
            continue
        if item is None:
            click.echo(pat)
        elif pat.startswith(item.rstrip("/") + "/"):
            click.echo(pat[len(item.rstrip("/")) + 1:])


@overrides_group.command("is-exempt")
@_fleet_option
@click.option("--model", required=True, help="The declaring model.")
@click.argument("path")
def overrides_is_exempt(fleet_path, model, path) -> None:
    """Exit 0 when PATH (relative to the model root) is exempt from the
    template cascade, 3 when it is not."""
    fleet = _load(fleet_path)
    _known_model(fleet, model)
    if fleet.file_override(model, path) is None:
        sys.exit(3)


if __name__ == "__main__":
    main()
