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

from . import DriftDoctorError, StaleBaselineError, TmdlEditError, __version__
from .baseline import capture as run_capture
from .baseline import ensure_baseline_fresh, load_baseline
from .detect import build_report, detect_fleet, is_actionable, is_gating
from .fleet import load_fleet
from .ledger import RETIREMENT_KINDS, Ledger
from .overrides import format_ack
from .remediate import is_destructive
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
    for e in summary.get("repointed") or []:
        click.echo(f"  [ledger] source view of {e['table']}: {e['old_view']} "
                   f"-> {e['new_view']}")


@main.command()
@_fleet_option
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable report (schema version 2).")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--kind", "kind_filter", default=None,
              help="Only findings whose kind starts with this prefix.")
@click.option("--csv", "as_csv", is_flag=True,
              help="One CSV row per model: shape and drift by bucket.")
@click.option("--sync", is_flag=True,
              help="Also report the sync-only channel (pristine-category "
                   "purge items).")
@click.option("--allow-stale", is_flag=True,
              help="Proceed even if the template changed since the last "
                   "capture (misclassification risk — see the docs).")
def detect(fleet_path, as_json, model_filter, kind_filter, as_csv, sync,
           allow_stale) -> None:
    """Detect drift in every derived model. Read-only.

    Exit code 1 when actionable findings exist (allowlisted, not advisory,
    not overridden, on a model whose gating is not advisory); 3 when a model
    could not be parsed. Wire it straight into CI."""
    fleet = _load(fleet_path)
    if not fleet.baseline_path.exists():
        # nothing captured yet: compare against the live template (never
        # stale) without writing anything
        from .baseline import build_snapshot
        from .tmdl import parse_model
        click.echo("note: no baseline captured yet; comparing against the "
                   "live template. Run `drift-doctor capture` to commit one.",
                   err=True)
        baseline = build_snapshot(parse_model(fleet.template_definition),
                                  fleet.mapping_tables, fleet.raw)
    else:
        baseline = _fresh_baseline(fleet, allow_stale)
    if (kind_filter or "").rstrip(".") == "retire.stranded_ref" and not any(
            k == "table" for k, _r in
            Ledger(fleet.ledger_path).active_retirements()):
        raise ConfigError(
            f"the stranded-reference check was requested but the ledger "
            f"({fleet.ledger_path}) records no retired table; it is empty or "
            f"unreadable, so the check would pass vacuously")
    if (kind_filter or "").rstrip(".") == "fieldparam.blank_row":
        from .fieldparams import TemplateUnavailable, canonical_blank_rows
        try:
            canonical_blank_rows(fleet)
        except TemplateUnavailable as e:
            raise ConfigError(f"the blank-row check was requested but {e}")
    errors: list = []
    findings = _detect(fleet, baseline, errors, model_filter=model_filter,
                       kind_filter=kind_filter, sync=sync)
    from .cascade import CSV_HEADER, in_cascade, summarize
    names = [model_filter] if model_filter else list(fleet.models)
    if as_json:
        click.echo(json.dumps(build_report(fleet, findings, errors,
                                           model_filter, kind_filter,
                                           baseline=baseline),
                              indent=2, ensure_ascii=False))
    elif as_csv:
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for row in summarize(fleet, baseline, findings, names):
            writer.writerow([row[k] for k in CSV_HEADER])
        click.echo(buf.getvalue(), nl=False)
    else:
        _print_findings(fleet, findings, errors, model_filter, kind_filter)
        from .detect import divergence_report
        for name in names:
            div = divergence_report(fleet, name, baseline)
            if div:
                click.echo(f"source-divergent tables in {name}: "
                           + ", ".join(f"{t} ({v['model']} vs the template's "
                                       f"{v['template']})"
                                       for t, v in div.items()))
        if not kind_filter:
            rows = summarize(fleet, baseline, findings, names)
            click.echo("")
            for row in rows:
                ok = in_cascade(row)
                click.echo(f"[{'OK  ' if ok else 'GAP '}] {row['model']}")
                if row["missing"]:
                    click.echo(f"    - missing: {', '.join(row['missing'])}")
            passing = sum(1 for r in rows if in_cascade(r))
            click.echo(f"Summary: {passing}/{len(rows)} models in cascade")
    if errors:
        sys.exit(PARSE_ERROR_EXIT)
    if any(is_gating(f, fleet) for f in findings):
        sys.exit(1)


def _print_findings(fleet, findings, errors, model_filter, kind_filter):
    decisions = [f for f in findings if f.kind == "decision"]
    findings = [f for f in findings if f.kind != "decision"]
    for d in decisions:
        click.echo(f"[decision] {d.model}: {d.data['kind']} {d.ref} — "
                   f"{d.detail}")
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
@click.option("--kind", "kinds", multiple=True,
              help="Only findings of this kind (dotted prefix: `measure` "
                   "covers every measure.* kind). Repeatable; intersects "
                   "allowlist.kinds.")
@click.option("--dry-run", is_flag=True,
              help="Compute every edit and print unified diffs; write nothing "
                   "(not even the ledger).")
@click.option("--sync", is_flag=True,
              help="Also offer retire.* and purge items (deletions of provably "
                   "ex-template or uncustomised objects). Never on by default.")
@click.option("--converge", is_flag=True,
              help="Re-detect and re-apply until a pass changes nothing "
                   "(at most 4 passes).")
@click.option("--model", "model_filter", default=None,
              help="Only this derived model.")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Apply without asking, but only batch-safe kinds, and only "
                   "with at least one --kind. Never deletes anything.")
@click.option("--interactive", is_flag=True,
              help="Ask before every fix (the default on a terminal).")
@click.option("--confirm-deletions", is_flag=True,
              help="With --sync: the explicit second confirmation that lets "
                   "deletions run without a terminal (for scheduled jobs).")
@click.option("--ref", "refs", multiple=True, metavar="NAME",
              help="Push only this template measure (bare name or "
                   "Table[Measure]; repeatable) to every model, where each "
                   "model hosts it. Other drift is left alone.")
@click.option("--add-missing", is_flag=True,
              help="With --ref: also insert the measure into models that "
                   "lack it (on the template's host table).")
@click.option("--digest-json", "digest_json", default=None, metavar="FILE",
              help="Write the run's decision digest (what was retained, "
                   "what needs a person, what owes a refresh) as JSON.")
@click.option("--backup", is_flag=True,
              help="Copy each file to <name>.bak_<timestamp> before its first "
                   "write in this run; the copies are removed when the run "
                   "succeeds and kept when anything failed.")
@click.option("--allow-stale", is_flag=True,
              help="Proceed on a stale baseline (misclassification risk).")
def remediate(fleet_path, kinds, dry_run, sync, converge, model_filter,
              assume_yes, interactive, confirm_deletions, refs, add_missing,
              digest_json, backup, allow_stale) -> None:
    """Cascade template truth into the derived models (allowlist-gated).

    On a terminal every fix is confirmed (y/n, a = yes to the rest, q =
    stop). Without a terminal nothing is applied unless --yes is given with
    at least one --kind, and even then deletions are never applied: they
    need an interactive yes, or --sync with --confirm-deletions."""
    from .interactive import Quit, ask, stdin_is_interactive
    if refs:
        _remediate_refs(fleet_path, refs, add_missing, model_filter, dry_run,
                        assume_yes, allow_stale)
        return
    if assume_yes and not kinds:
        click.echo("ERROR: --yes requires an explicit --kind (it never "
                   "applies every kind at once)", err=True)
        sys.exit(USAGE_EXIT)
    if confirm_deletions and not sync:
        click.echo("ERROR: --confirm-deletions only makes sense with --sync",
                   err=True)
        sys.exit(USAGE_EXIT)
    fleet = _load(fleet_path)
    baseline = _fresh_baseline(fleet, allow_stale)
    def detect_once():
        return _detect(fleet, baseline, model_filter=model_filter, sync=sync)

    terminal = stdin_is_interactive()
    deletions_ok = sync and (confirm_deletions or (terminal and not assume_yes))
    kw = dict(kind=list(kinds), dry_run=dry_run, sync=sync, baseline=baseline,
              allow_destructive=deletions_ok or dry_run,
              batch_only=assume_yes, backup=backup,
              offer_extra_delete=terminal and not assume_yes)
    if not dry_run and not assume_yes:
        if terminal:
            answers = {"all": False, "quit": False}

            def confirm(f, label, changes, position=None):
                if answers["quit"]:
                    return False, "stopped (q)"
                if answers["all"] and not is_destructive(f.kind):
                    return True, ""
                where = "[%d/%d] " % position if position else ""
                warn = " DESTRUCTIVE (deletes derived-model content)." \
                    if is_destructive(f.kind) else ""
                try:
                    answer = ask(f"{where}{f.model}: {f.kind} {f.ref} — "
                                 f"{label}.{warn} Apply?", allow_all=True)
                except Quit:
                    answers["quit"] = True
                    return False, "stopped (q)"
                if answer == "all":
                    answers["all"] = True
                return bool(answer), "declined"
        else:
            click.echo("note: stdin is not a terminal, so questions are "
                       "disabled and every item is treated as 'no' "
                       "(non-interactive). Pass --yes with --kind to apply "
                       "batch-safe kinds.")

            def confirm(f, label, changes, position=None):
                if is_destructive(f.kind) and deletions_ok:
                    return True, ""
                return (False, "non-interactive: not applied without --yes",
                        "skipped_noninteractive")
        kw["confirm"] = confirm
    if converge:
        results = remediate_converge(fleet, detect_once, **kw)
    else:
        results = [run_remediate(fleet, detect_once(), **kw)]
    if converge and dry_run:
        click.echo("note: --converge has no effect with --dry-run (nothing "
                   "changes on disk between passes); showing one pass.")
    if digest_json:
        entries = [d for r in results for d in r.digest]
        Path(digest_json).write_text(json.dumps(
            {"entries": entries}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    for d in (results[-1].digest if results else []):
        click.echo(f"[decision] {d['model']}: {d['kind']} {d['name']} — "
                   f"{d['note']}")
    if not dry_run:
        from .enrichment import aggregate, notify, write_report
        entries = aggregate([n for r in results for n in r.notices])
        if entries:
            path = write_report(fleet.state_dir, entries)
            click.echo(f"view enrichment: {len(entries)} table(s) need their "
                       f"source view extended -> {path}")
            warning = notify(fleet, entries)
            if warning:
                click.echo(f"warning: {warning}", err=True)
    failed = 0
    for n, result in enumerate(results, 1):
        if len(results) > 1:
            click.echo(f"\n══ pass {n}")
        _print_outcomes(result, dry_run)
        failed += result.count("failed") + len(result.errors)
    if converge and not dry_run:
        last = results[-1]
        state = ("converged" if last.count("applied") == 0
                 else f"stopped after {len(results)} pass(es) without "
                      f"converging; run again")
        click.echo(f"── {state}")
    if failed:
        sys.exit(1)


def _remediate_refs(fleet_path, refs, add_missing, model_filter, dry_run,
                    assume_yes, allow_stale):
    from .interactive import ask, stdin_is_interactive
    from .named import cascade_named
    fleet = _load(fleet_path)
    _fresh_baseline(fleet, allow_stale)
    if model_filter and model_filter not in fleet.models:
        raise ConfigError(f"unknown model '{model_filter}'; fleet.yml "
                          f"defines: {', '.join(fleet.models)}")
    try:
        plan = cascade_named(fleet, refs, add_missing=add_missing,
                             model_filter=model_filter, dry_run=True)
    except DriftDoctorError as e:
        raise ConfigError(str(e))
    todo = [o for o in plan.outcomes if o.status == "would-apply"]
    go = False
    if todo and not dry_run:
        if assume_yes:
            go = True
        elif stdin_is_interactive():
            go = bool(ask(f"Apply {len(todo)} change(s) for "
                          f"{', '.join(refs)}?"))
        else:
            click.echo("note: stdin is not a terminal and --yes was not "
                       "given; nothing is applied (non-interactive).")
    result = cascade_named(fleet, refs, add_missing=add_missing,
                           model_filter=model_filter, dry_run=not go)
    for o in result.outcomes:
        tag = {"applied": "applied", "would-apply": "would apply",
               "failed": "FAILED"}.get(o.status, "skipped")
        click.echo(f"[{tag}] {o.model}: {o.measure} — {o.reason}")
        if o.status == "would-apply" and dry_run and o.path is not None:
            import difflib
            click.echo("".join(difflib.unified_diff(
                o.old.splitlines(True), o.new.splitlines(True),
                fromfile=f"a/{o.path}", tofile=f"b/{o.path}")))
    click.echo(f"\n── summary: applied={result.count('applied')} "
               f"would-apply={result.count('would-apply')} "
               f"skipped={result.count('skipped')} "
               f"failed={result.count('failed')}")
    if result.count("failed"):
        sys.exit(1)


def _print_outcomes(result, dry_run):
    for o in result.outcomes:
        f = o.finding
        if o.status in ("applied", "would-apply"):
            verb = "would apply" if o.status == "would-apply" else "applied"
            mode = ""
            if o.status == "would-apply":
                from .remediate import is_batch_safe
                mode = (" [destructive: asked, never under --yes]"
                        if is_destructive(f.kind) else
                        " [asked; batch-safe under --yes]"
                        if is_batch_safe(f.kind) else " [asked]")
            click.echo(f"[{verb}] {f.model}: {f.kind} {f.ref} — "
                       f"{o.reason}{mode}")
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
        elif o.status in ("overridden", "skipped_override"):
            click.echo(f"[ACK] {f.model}: {f.kind} {f.ref} — {o.reason}")
        elif o.status == "manual":
            click.echo(f"[manual] {f.model}: {f.kind} {f.ref} — {o.reason}")
        else:
            click.echo(f"[skipped] {f.model}: {f.kind} {f.ref} — {o.reason}")
    for notice in result.notices:
        click.echo(f"[notice] {notice.get('message') if isinstance(notice, dict) else notice}")
    for err in result.errors:
        click.echo(f"[FAILED] {err}", err=True)
    if result.backups_removed:
        click.echo(f"backups: removed {result.backups_removed} safety "
                   f"cop{'y' if result.backups_removed == 1 else 'ies'}")
    for path in result.backups_kept:
        click.echo(f"backups: kept {path} (the run had failures)")
    click.echo(f"\n── summary: applied={result.count('applied')} "
               f"would-apply={result.count('would-apply')} "
               f"skipped={result.count('skipped') + result.count('skipped_noninteractive')} "
               f"manual={result.count('manual')} "
               f"blocked={result.count('blocked')} "
               f"auto_skipped_precondition="
               f"{result.count('auto_skipped_precondition')} "
               f"overridden={result.count('overridden')} "
               f"skipped_override={result.count('skipped_override')} "
               f"skipped_divergent={result.count('skipped_divergent')} "
               f"skipped_destructive={result.count('skipped_destructive')} "
               f"skipped_noninteractive="
               f"{result.count('skipped_noninteractive')} "
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
    return build_report(fleet, findings, errors, baseline=baseline)


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


# ───────────────────────────── renames ─────────────────────────────

@main.group("rename")
def rename_group() -> None:
    """Rename an object everywhere it is used."""


@rename_group.command("measure")
@_fleet_option
@click.argument("old")
@click.argument("new")
@click.option("--old-label", default=None,
              help="Picker label to replace (default: OLD).")
@click.option("--new-label", default=None,
              help="Picker label to write (default: NEW).")
@click.option("--format-arm", "format_arms", multiple=True,
              metavar="MODEL=FORMAT",
              help="Insert a format arm for NEW in MODEL's format selector "
                   "when NEW would show fewer decimals (repeatable).")
@click.option("--model", "model_filters", multiple=True,
              help="Only models whose path contains this text (repeatable).")
@click.option("--include-unlisted", is_flag=True, hidden=True,
              help="Accepted for compatibility; every model is in fleet.yml.")
@click.option("--dry-run", is_flag=True, help="Print the plan; write nothing.")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Apply without the one confirmation.")
def rename_measure_cmd(fleet_path, old, new, old_label, new_label,
                       format_arms, model_filters, include_unlisted, dry_run,
                       assume_yes) -> None:
    """Rename measure OLD to NEW in the template and every model: picker
    labels, the declaration, DAX references (text comparisons included) and
    report bindings. Exit 1 when no model declares OLD, 2 when a precision
    loss would remain (pass --format-arm)."""
    from .interactive import ask
    from .renames import plan_measure_rename, write_plan
    fleet = _load(fleet_path)
    arms = {}
    for spec in format_arms:
        if "=" not in spec:
            raise ConfigError(f"--format-arm needs MODEL=FORMAT (got {spec!r})")
        k, v = spec.split("=", 1)
        arms[k] = v
    plans = plan_measure_rename(fleet, old, new, old_label=old_label,
                                new_label=new_label, format_arms=arms,
                                model_filters=model_filters)
    if not plans:
        click.echo(f"no model declares measure '{old}'; nothing to rename.")
        sys.exit(1)
    totals = {"labels": 0, "decl": 0, "dax": 0, "reports": 0, "arm": 0}
    for p in plans:
        reports = "n/a" if p.reports is None else p.reports
        flag = "  PRECISION LOSS" if p.risks else ""
        arm = f" arm {p.format_arm}" if p.format_arm else ""
        click.echo(f"{p.name}: labels {p.labels} decl {p.decl} dax {p.dax} "
                   f"reports {reports}{arm}{flag}")
        for e in p.errors:
            click.echo(f"  ERROR {e}", err=True)
        totals["labels"] += p.labels
        totals["decl"] += p.decl
        totals["dax"] += p.dax
        totals["reports"] += p.reports or 0
        totals["arm"] += p.format_arm
    click.echo("total: " + " ".join(f"{k} {v}" for k, v in totals.items()))
    if any(p.reports is None for p in plans):
        click.echo("note: pbi-plot-styler is not installed, so report "
                   "references are neither counted nor rewritten.")
    risky = [p for p in plans if p.risks]
    if risky:
        for p in risky:
            for path, before, after in p.risks:
                click.echo(f"precision loss in {p.name} ({path}): '{old}' "
                           f"shows {before}, '{new}' would show {after}")
        click.echo("pass --format-arm MODEL=FORMAT for each model listed")
        sys.exit(USAGE_EXIT)
    if any(p.errors for p in plans):
        sys.exit(1)
    if dry_run:
        return
    if not assume_yes and not ask(f"Rename '{old}' to '{new}' in "
                                  f"{len(plans)} model(s)?"):
        click.echo("declined; nothing was written.")
        sys.exit(1)
    for p in plans:
        write_plan(p)
    click.echo(f"renamed in {len(plans)} model(s).")


@main.group("renames")
def renames_group() -> None:
    """The reviewed rename map (`renames:` in fleet.yml)."""


@renames_group.command("check")
@_fleet_option
@click.option("--model", "model_filter", default=None, help="Only this model.")
def renames_check(fleet_path, model_filter) -> None:
    """Every non-empty measure rename must point at a measure the model
    declares. Exit 1 when one does not."""
    from .renames import fleet_rename_map, model_profile
    fleet = _load(fleet_path)
    try:
        rmap = fleet_rename_map(fleet)
    except DriftDoctorError as e:
        raise ConfigError(str(e))
    names = list(fleet.models)
    if model_filter:
        if model_filter not in fleet.models:
            click.echo(f"warning: unknown model '{model_filter}', skipped",
                       err=True)
            names = []
        else:
            names = [model_filter]
    bad = False
    for name in names:
        valid, invalid = rmap.for_profile(model_profile(fleet, name)).check(
            fleet.model_definition(name))
        click.echo(f"{name}: {valid} valid mapping(s)")
        for o, n in invalid:
            click.echo(f"  {o} -> {n} (target doesn't exist)")
            bad = True
    if bad:
        sys.exit(1)


@renames_group.command("apply")
@_fleet_option
@click.option("--model", "model_filter", default=None, help="Only this model.")
@click.option("--dry-run", is_flag=True, help="Count only; write nothing.")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Apply without the one confirmation.")
def renames_apply(fleet_path, model_filter, dry_run, assume_yes) -> None:
    """Rewrite references to renamed measures and columns in every model
    (declarations are not renamed; see `rename measure`)."""
    from .interactive import ask
    from .renames import fleet_rename_map, model_profile, rewrite_model
    fleet = _load(fleet_path)
    try:
        rmap = fleet_rename_map(fleet)
    except DriftDoctorError as e:
        raise ConfigError(str(e))
    names = [model_filter] if model_filter else list(fleet.models)
    if model_filter and model_filter not in fleet.models:
        raise ConfigError(f"unknown model '{model_filter}'")
    if not dry_run and not assume_yes and not ask(
            f"Rewrite rename references in {len(names)} model(s)?"):
        click.echo("declined; nothing was written.")
        sys.exit(1)
    failed = False
    for name in names:
        m = rmap.for_profile(model_profile(fleet, name))
        res = rewrite_model(fleet.model_definition(name),
                            m.active_measures(), m.columns, dry_run=dry_run)
        for e in res.errors:
            click.echo(f"{name}: ERROR {e}", err=True)
            failed = True
        click.echo(f"{name}: files {len(res.files_modified)} measures "
                   f"{res.measures_modified} replacements {res.replacements} "
                   f"relationships {res.relationships}"
                   + (" (dry run)" if dry_run else ""))
    if failed:
        sys.exit(1)


# ───────────────────────────── analysis groups ─────────────────────────────

@main.group("groups")
def groups_group() -> None:
    """Plan and drop analysis groups (subject areas) safely."""


def _group_targets(fleet, model, all_models, path):
    """[(name, model dir)] for --model / --all / --path."""
    if path:
        p = Path(path)
        if not (p / "definition").is_dir():
            raise ConfigError(f"{path} is not a *.SemanticModel folder")
        return [(p.name, p)]
    if all_models:
        return list(fleet.models.items())
    if not model:
        raise ConfigError("name a target: --model NAME, --all or --path DIR")
    if model == "template":
        return [("template", fleet.template)]
    if model not in fleet.models:
        click.echo(f"unknown model '{model}'; fleet.yml defines: "
                   f"{', '.join(fleet.models)}", err=True)
        sys.exit(USAGE_EXIT)
    return [(model, fleet.models[model])]


def _target_options(fn):
    fn = click.option("--path", "path", default=None,
                      help="An explicit *.SemanticModel folder.")(fn)
    fn = click.option("--all", "all_models", is_flag=True,
                      help="Every model in fleet.yml.")(fn)
    fn = click.option("--model", "model", default=None,
                      help="A fleet model, or `template`.")(fn)
    return fn


@groups_group.command("plan")
@_fleet_option
@_target_options
@click.option("--drop", "drop", multiple=True, required=True,
              help="A group to drop (repeatable).")
@click.option("--json", "json_out", default=None, metavar="FILE",
              help="Also write the plans as JSON.")
def groups_plan(fleet_path, model, all_models, path, drop, json_out) -> None:
    """Read-only: which field groups and fact tables are exclusive to the
    dropped groups (safe to remove) and which are shared (kept)."""
    from .groups import build_graph, make_plan
    fleet = _load(fleet_path)
    plans = {}
    for name, mdir in _group_targets(fleet, model, all_models, path):
        click.echo(f"== {name} ({mdir})")
        graph = build_graph(fleet, mdir / "definition")
        if graph is None:
            click.echo("  SKIP: no mapping table")
            continue
        plan = make_plan(graph, drop)
        plans[name] = dict(plan, path=str(mdir))
        _echo_plan(plan)
    if json_out:
        Path(json_out).write_text(json.dumps(plans, indent=2, sort_keys=True,
                                             ensure_ascii=False) + "\n",
                                  encoding="utf-8")


def _echo_plan(plan):
    if plan["absent"]:
        click.echo(f"  not in this model: {', '.join(plan['absent'])}")
    dead = plan["dead_fact_tables"]
    if dead:
        click.echo("  dead fact tables (sensor references): " + ", ".join(
            f"{t} ({n})" for t, n in dead.items()))
    if not plan["present"]:
        click.echo("  none of the requested groups exists in this model")
        return
    ex, sh = plan["exclusive"], plan["shared"]
    click.echo(f"  exclusive field groups ({len(ex['field_groups'])}): "
               f"{', '.join(ex['field_groups']) or '-'}")
    for fg, keep in sh["field_groups"].items():
        click.echo(f"  shared field group {fg}: kept by {', '.join(keep)}")
    click.echo(f"  exclusive fact tables ({len(ex['fact_tables'])}): "
               f"{', '.join(ex['fact_tables']) or '-'}")
    for t, keep in sh["fact_tables"].items():
        click.echo(f"  shared fact table {t}: kept by {', '.join(keep)}")


LAYERS = ("selector-branches", "measures", "selectors", "annotation",
          "gates", "bookmarks", "tables", "mapping")


@groups_group.command("drop")
@_fleet_option
@_target_options
@click.option("--drop", "drop", multiple=True, required=True,
              help="A group to drop (repeatable).")
@click.option("--layer", "layers", multiple=True,
              type=click.Choice(LAYERS),
              help="Run only these layers (default: all, in dependency "
                   "order).")
@click.option("--apply", "apply", is_flag=True,
              help="Write the changes (default: dry run).")
@click.option("--verify", is_flag=True,
              help="After --apply, run the verification command per model.")
@click.option("--verify-cmd", default="tmdl-preflight check {model}",
              show_default=True, help="The verification command.")
def groups_drop(fleet_path, model, all_models, path, drop, layers, apply,
                verify, verify_cmd) -> None:
    """Drop groups end to end: selector branches, measures, selector rows,
    annotation, gates, bookmarks, dead tables, then mapping rows LAST."""
    from . import groups as gp
    fleet = _load(fleet_path)
    chosen = [l for l in LAYERS if not layers or l in layers]
    for name, mdir in _group_targets(fleet, model, all_models, path):
        defn = mdir / "definition"
        click.echo(f"== {name} ({mdir})")
        graph = gp.build_graph(fleet, defn)
        if graph is None:
            click.echo("  SKIP: no mapping table")
            continue
        plan = gp.make_plan(graph, drop)
        if plan["absent"]:
            click.echo(f"  not in this model: {', '.join(plan['absent'])}")
        c = gp.conf(fleet)
        try:
            summary = _run_layers(fleet, gp, c, mdir, graph, plan, drop,
                                  chosen, apply)
        except (DriftDoctorError, TmdlEditError) as e:
            click.echo(f"  ERROR: {e}", err=True)
            sys.exit(1)
        for line in summary:
            click.echo("  " + line)
        if apply:
            click.echo("  applied; commit this model's changes on their own")
            if verify:
                _verify(verify_cmd, mdir)
        else:
            click.echo("  dry run: nothing was written (use --apply)")


def _run_layers(fleet, gp, c, mdir, graph, plan, drop, chosen, apply):
    defn = mdir / "definition"
    present = bool(plan["present"])
    out = []

    def run(layer_fn):
        changes = []
        res = layer_fn(changes)
        if apply and changes:
            gp.write_changes(changes)
        return res

    if not present:
        out.append("nothing to drop in this model")
    exclusive = plan["exclusive"]["field_groups"]
    purge_preview = []
    if present and "selector-branches" in chosen:
        ms = gp.dropped_axis_measures(defn, c, exclusive)
        r = run(lambda ch: gp.layer_selector_branches(defn, c, ms, ch))
        out.append(f"selector branch lines removed: {r['lines_removed']}")
    if present and "measures" in chosen:
        r = run(lambda ch: gp.layer_measures(fleet, defn, graph, drop, ch))
        purge_preview = r["purge"]
        out.append(f"measures purged: {', '.join(r['purge']) or '-'}; kept "
                   f"live: {', '.join(r['kept']) or '-'}")
    if present and "selectors" in chosen:
        r = run(lambda ch: gp.layer_selector_rows(defn, c, exclusive, ch))
        out.append(f"selector rows removed: {r['rows_removed']} (exclusive "
                   f"field groups: {', '.join(r['exclusive']) or '-'})")
    if present and "annotation" in chosen:
        r = run(lambda ch: gp.layer_annotation(defn, c, drop, ch))
        out.append("annotation keys removed: " + (", ".join(r.get("keys")
                                                            or []) or
               "none (a template-wide group has no annotation)"))
    if present and "gates" in chosen:
        r = run(lambda ch: gp.layer_gates(defn, fleet, drop, ch))
        out.append(f"gate measures removed: "
                   f"{', '.join(r['gates_removed']) or '-'}; aggregate lines: "
                   f"{sum(r['aggregate_lines'].values())}")
    if present and "bookmarks" in chosen:
        r = run(lambda ch: gp.layer_bookmarks(mdir, fleet, c, drop, ch))
        out.append(f"bookmarks deleted: "
                   f"{', '.join(b['display_name'] for b in r['bookmarks']) or '-'}")
    if "tables" in chosen:
        ignore = () if apply else purge_preview
        g2 = gp.build_graph(fleet, defn, ignore_measures=ignore)
        dead = sorted(g2.dead) if g2 else []
        targets = [t for t in dead if t in plan["exclusive"]["fact_tables"]]
        out.append(f"dead fact tables: {', '.join(dead) or '-'}")
        for t in targets:
            rep = run(lambda ch, t=t: gp.drop_table(fleet, mdir, t, ch))
            out.append(f"table {t}: relationships "
                       f"{len(rep['relationships'])}, sensors "
                       f"{sum(rep['sensors'].values())}, Q&A entities "
                       f"{rep['qa_entities']}, report edits "
                       f"{sum(rep['reports'].values())}, deregistered "
                       f"{'yes' if rep['registration'] else 'no'}")
            for line in rep["manual_review"]:
                out.append(f"  manual review: {line}")
    if present and "mapping" in chosen:
        r = run(lambda ch: gp.layer_mapping(defn, c, drop, ch))
        out.append(f"mapping rows removed: {r.get('removed', 0)}")
    return out


def _verify(cmd, mdir):
    import shlex
    import subprocess
    try:
        r = subprocess.run(shlex.split(cmd.format(model=str(mdir))),
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        click.echo(f"  verify: could not run ({e})")
        return
    click.echo(f"  verify: exit {r.returncode}")
    for line in (r.stdout + r.stderr).strip().split("\n")[-8:]:
        click.echo(f"    {line}")


@groups_group.command("annotate")
@_fleet_option
@_target_options
@click.option("--dry-run", is_flag=True, help="Report counts; write nothing.")
def groups_annotate(fleet_path, model, all_models, path, dry_run) -> None:
    """Mirror the mapping table's groups as model.tmdl annotations (one per
    group; human-written descriptions are kept)."""
    from . import groups as gp
    fleet = _load(fleet_path)
    c = gp.conf(fleet)
    descriptions = c.get("descriptions") or {}
    bad = False
    for name, mdir in _group_targets(fleet, model, all_models, path):
        defn = mdir / "definition"
        groups = gp.mapping_groups(defn, c)
        if groups is None:
            click.echo(f"{name}: warning: the mapping table does not decode",
                       err=True)
            groups = []
        if not groups:
            click.echo(f"{name}: no groups found; model.tmdl untouched")
            bad = True
            continue
        mf = defn / "model.tmdl"
        old = mf.read_text(encoding="utf-8")
        new, n = gp.annotate(old, groups, c["annotation_prefix"],
                             descriptions)
        click.echo(f"{name}: kept {n['kept']} added {n['added']} removed "
                   f"{n['removed']} total {n['total']}"
                   + (" (dry run)" if dry_run else ""))
        if not dry_run and new != old:
            from . import tmdl_edit as te
            te.assert_tmdl_valid(new)
            mf.write_text(new, encoding="utf-8")
    if bad:
        sys.exit(1)


@groups_group.command("drop-table")
@_fleet_option
@_target_options
@click.option("--tables", "tables", multiple=True, required=True,
              help="A table to drop (repeatable).")
@click.option("--apply", "apply", is_flag=True,
              help="Write the changes (default: dry run).")
def groups_drop_table(fleet_path, model, all_models, path, tables,
                      apply) -> None:
    """Drop tables and every surface they occupy: boolean sensors in other
    measures, relationships, model registration, the Q&A schema, report
    bindings, then the file."""
    from . import groups as gp
    fleet = _load(fleet_path)
    for name, mdir in _group_targets(fleet, model, all_models, path):
        click.echo(f"== {name} ({mdir})")
        for t in tables:
            changes = []
            try:
                rep = gp.drop_table(fleet, mdir, t, changes)
                if apply:
                    gp.write_changes(changes)
            except (DriftDoctorError, TmdlEditError) as e:
                click.echo(f"  ERROR {t}: {e}", err=True)
                sys.exit(1)
            click.echo(f"  {t}: file {rep['file'] or 'absent'}; sensors "
                       f"{rep['sensors'] or 0}; relationships "
                       f"{', '.join(rep['relationships']) or '-'}; "
                       f"deregistered {'yes' if rep['registration'] else 'no'}"
                       f"; Q&A {rep['qa_entities']} entities / "
                       f"{rep['qa_relationships']} relationships; reports "
                       f"{rep['reports'] or 0}")
            for line in rep["manual_review"]:
                click.echo(f"    manual review: {line}")
        click.echo("  applied" if apply else
                   "  dry run: nothing was written (use --apply)")


# ───────────────────────────── selectors and named measures ─────────────────

@main.command("add-branch")
@_fleet_option
@click.option("--measure", required=True, help="The new measure.")
@click.option("--sibling", required=True,
              help="The measure whose SWITCH arms are copied.")
@click.option("--model", "models", multiple=True,
              help="A fleet model (repeatable; default: every model with "
                   "the helper file).")
@click.option("--yes", "assume_yes", is_flag=True, help="Write the changes.")
def add_branch_cmd(fleet_path, measure, sibling, models, assume_yes) -> None:
    """Clone each SWITCH arm of --sibling for --measure in the selector
    helper table, one new line right below each sibling arm."""
    from .selectors import add_branch
    fleet = _load(fleet_path)
    helper = (fleet.setting("selector_branch") or {}).get(
        "helper_file", "Helper Measures.tmdl")
    names = list(models) or list(fleet.models)
    inserted = refused = done = 0
    for name in names:
        if name not in fleet.models:
            raise ConfigError(f"unknown model '{name}'")
        path = fleet.model_definition(name) / "tables" / helper
        if not path.is_file():
            if models:
                click.echo(f"{name}: no helper file {helper}")
            continue
        status, n = add_branch(path, measure, sibling, assume_yes)
        click.echo(f"{name}: {status}")
        done += 1
        inserted += n
        refused += status.startswith("REFUSED")
    verb = "inserted" if assume_yes else "to insert"
    click.echo(f"── {inserted} branch(es) {verb} in {done} model(s), "
               f"{refused} refused")
    if not assume_yes and inserted:
        click.echo("dry run: run again with --yes to write")
    if refused:
        sys.exit(1)


@main.command("purge")
@_fleet_option
@click.option("--measure", "measures", multiple=True, required=True,
              help="A measure to remove (repeatable).")
@click.option("--model", "model", default=None,
              help="Comma-separated fleet models (or `template`).")
@click.option("--all", "all_models", is_flag=True,
              help="Every fleet model (never the template).")
@click.option("--table", default=None,
              help="The host table when a measure sits on several.")
@click.option("--apply", "apply", is_flag=True, help="Remove (default: dry run).")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Apply without a terminal (the names authorize it).")
def purge_cmd(fleet_path, measures, model, all_models, table, apply,
              assume_yes) -> None:
    """Remove named measures from the chosen models, on whatever table
    hosts them. Blocked while something else still calls the measure."""
    from .interactive import ask, stdin_is_interactive
    from .measure_ops import plan_purge
    fleet = _load(fleet_path)
    if all_models:
        targets = list(fleet.models.items())
    elif model:
        targets = []
        for n in model.split(","):
            n = n.strip()
            if n == "template":
                targets.append(("template", fleet.template))
            elif n in fleet.models:
                targets.append((n, fleet.models[n]))
            else:
                raise ConfigError(f"unknown model '{n}'")
    else:
        raise ConfigError("name the models: --model NAME[,NAME] or --all")
    bad = 0
    removed = 0
    for name, mdir in targets:
        for m in measures:
            item = plan_purge(name, mdir, m, table)
            if item.status == "blocked":
                bad += 1
                click.echo(f"[BLOCKED] {name}: {m} — {item.reason}")
                continue
            if item.status == "skipped":
                click.echo(f"[skipped] {name}: {m} — {item.reason}")
                continue
            if not apply:
                click.echo(f"[would remove] {name}: {m} — {item.reason}")
                click.echo(item.diff())
                continue
            ok = assume_yes or (stdin_is_interactive() and ask(
                f"{name}: remove measure {m} ({item.reason})?"))
            if not ok:
                click.echo(f"[skipped] {name}: {m} — not confirmed")
                continue
            item.path.write_bytes(item.new.encode("utf-8"))
            removed += 1
            click.echo(f"[removed] {name}: {m} — {item.reason}")
    click.echo(f"── removed={removed} blocked={bad}")
    if bad:
        sys.exit(1)


@main.command("retire")
@_fleet_option
@click.argument("old")
@click.option("--new", "new", default=None,
              help="The measure that supersedes OLD (report bindings move "
                   "to it).")
@click.option("--drop", "drop", is_flag=True,
              help="Delete OLD's declaration; leave report bindings.")
@click.option("--skip-dir", "skip_dirs", multiple=True,
              help="Top-level folders to leave out.")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Apply (default: dry run).")
def retire_cmd(fleet_path, old, new, drop, skip_dirs, assume_yes) -> None:
    """Retire a superseded measure in the template and every model:
    bindings move first, the declaration goes last; refused while DAX still
    calls it."""
    from .measure_ops import plan_retire
    if bool(new) == bool(drop):
        raise ConfigError("give exactly one of --new NEW or --drop")
    fleet = _load(fleet_path)
    affected = 0
    for name, mdir in [("template", fleet.template)] + list(
            fleet.models.items()):
        try:
            rel = Path(mdir).resolve().relative_to(fleet.root)
            if rel.parts and rel.parts[0] in skip_dirs:
                continue
        except ValueError:
            pass
        res = plan_retire(name, mdir, old, None if drop else new, assume_yes)
        if res is None:
            continue
        affected += 1
        click.echo(f"{res.model}: {res.status} — {res.detail}")
    click.echo(f"── {affected} model(s) affected")


# ───────────────────────────── view enrichment ─────────────────────────────

@main.group("enrichment")
def enrichment_group() -> None:
    """Source views that must gain columns spliced onto divergent tables."""


@enrichment_group.command("rescan")
@_fleet_option
@click.option("--model", "models", default=None,
              help="Comma-separated model names to check (default: all).")
@click.option("--registry", default=None, metavar="FILE",
              help="Registry file (default: <state_dir>/"
                   "view_enrichment_outstanding.json).")
def enrichment_rescan(fleet_path, models, registry) -> None:
    """Warn about registry entries whose recorded columns are gone from the
    model. Exit 5 when any warning is printed (advisory); never writes."""
    from .enrichment import REGISTRY, load_registry, rescan
    fleet = _load(fleet_path)
    path = Path(registry) if registry else fleet.state_dir / REGISTRY
    entries = load_registry(path)
    wanted = set(models.split(",")) if models else None
    scoped = [e for e in entries if not wanted or e.get("model") in wanted]
    if not scoped:
        click.echo("no registry entries in scope.")
        return
    warnings = rescan(fleet, scoped)
    for w in warnings:
        click.echo(f"warning: {w}")
    if warnings:
        sys.exit(5)
    click.echo(f"{len(scoped)} registry entr"
               f"{'y' if len(scoped) == 1 else 'ies'} still match the models.")


@enrichment_group.command("report")
@_fleet_option
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]),
              default="markdown", show_default=True)
@click.option("--registry", is_flag=True,
              help="Render the whole registry instead of the last run's "
                   "entries.")
def enrichment_report(fleet_path, fmt, registry) -> None:
    """Render the last run's (or the registry's) enrichment entries."""
    from .enrichment import REGISTRY, REPORT, load_registry, markdown
    fleet = _load(fleet_path)
    entries = load_registry(fleet.state_dir / (REGISTRY if registry
                                               else REPORT))
    if fmt == "json":
        click.echo(json.dumps({"entries": entries}, indent=2,
                              ensure_ascii=False))
    else:
        click.echo(markdown(entries), nl=False)


# ───────────────────────────── encoded mapping tables ─────────────────────────────

@main.group("mapping")
def mapping_group() -> None:
    """Read and merge encoded entered-data mapping tables."""


def _read_encoded(path):
    from .mapping_codec import CodecError, extract_rows
    try:
        return extract_rows(Path(path).read_text(encoding="utf-8"))
    except (OSError, CodecError) as e:
        raise ConfigError(f"{path}: {e}")


@mapping_group.command("decode")
@click.argument("table", type=click.Path(exists=True, dir_okay=False))
def mapping_decode(table) -> None:
    """Print the row and category counts of an encoded table."""
    from .mapping_codec import categories
    rows = _read_encoded(table)
    cats = categories(rows)
    click.echo(f"{len(rows)} row(s), {len(cats)} categor"
               f"{'y' if len(cats) == 1 else 'ies'}")
    for c in cats:
        click.echo(f"  {sum(1 for r in rows if r and r[0] == c):>5}  {c}")


@mapping_group.command("diff")
@click.argument("derived", type=click.Path(exists=True, dir_okay=False))
@click.argument("template", type=click.Path(exists=True, dir_okay=False))
def mapping_diff(derived, template) -> None:
    """Categories the template has and DERIVED lacks (-), and categories
    only DERIVED has (+)."""
    from .mapping_codec import categories
    mine = categories(_read_encoded(derived))
    theirs = categories(_read_encoded(template))
    lines = [f"- {c}" for c in theirs if c not in mine] + \
        [f"+ {c}" for c in mine if c not in theirs]
    for line in lines:
        click.echo(line)
    if not lines:
        click.echo("both tables carry the same categories")


@mapping_group.command("union")
@click.option("--template", "template", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--derived", "derived", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="The table file to write (it must already hold exactly "
                   "one encoded string; everything else in it is kept).")
def mapping_union(template, derived, out) -> None:
    """Write every template row followed by each derived-only row into
    --out, then read it back."""
    from .mapping_codec import CodecError, extract_rows, replace_rows, \
        union_rows, verify_rows
    t_rows = _read_encoded(template)
    d_rows = _read_encoded(derived)
    rows, added = union_rows(t_rows, d_rows)
    text = Path(out).read_text(encoding="utf-8")
    try:
        new = replace_rows(text, rows)
        verify_rows(new, rows)
    except CodecError as e:
        raise ConfigError(f"{out}: {e}; nothing was written")
    Path(out).write_text(new, encoding="utf-8")
    back = extract_rows(Path(out).read_text(encoding="utf-8"))
    click.echo(f"template rows {len(t_rows)}, derived-only rows added "
               f"{added}, total {len(back)}")


# ───────────────────────────── field parameters ─────────────────────────────

@main.group("fieldparam")
def fieldparam_group() -> None:
    """Edit field-parameter tables."""


@fieldparam_group.command("retag")
@click.option("--from", "source", required=True, help="Current category.")
@click.option("--to", "dest", required=True, help="New category.")
@click.option("--display", "displays", multiple=True, required=True,
              help="Display name of a row to move (repeatable).")
@click.argument("files", nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False))
def fieldparam_retag(source, dest, displays, files) -> None:
    """Move the named live rows from category --from to --to in every FILE
    (only the category literal changes). Nothing is written unless every
    requested row is found under --from in every file."""
    from . import tmdl_edit as te
    from .fieldparams import retag
    planned = []
    for path in files:
        text = Path(path).read_text(encoding="utf-8")
        try:
            new, done = retag(text, source, dest, displays)
            te.assert_tmdl_valid(new)
        except (DriftDoctorError, TmdlEditError) as e:
            click.echo(f"{path}: {e}")
            sys.exit(1)
        planned.append((path, new, done))
    for path, new, done in planned:
        Path(path).write_text(new, encoding="utf-8")
        click.echo(f"{path}: retagged {', '.join(done)} -> {dest}")


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
