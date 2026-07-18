# Allowlist-only cascading

**The rule: nothing cascades by default. Every automated fix is authorized
twice — the kind must be named in the allowlist, and deletions additionally
require `--sync`.**

## Why default-deny

A fleet tool that "fixes everything it finds" is a fleet-wide blast radius
with a progress bar. Detection is cheap to be liberal with — a false
positive costs a minute of reading. Remediation is not: a wrong cascade
lands the same wrong edit in every model at machine speed, and the fleet's
own history includes exactly that class of incident (see
[Safe TMDL surgery](safe-tmdl-surgery.md) for one that shipped a syntax
error to every tenant at once).

Default-deny inverts the failure mode. A too-small allowlist means some
drift stays reported-but-unfixed — annoying, visible, safe. A too-large
"denylist" tool means an unanticipated kind cascades before anyone thought
about whether it should — silent, fleet-wide, and discovered by a tenant.

## The three gates

1. **`allowlist.kinds`** — the fleet-level policy of which finding kinds may
   be applied at all. Detection is unaffected: everything is still found and
   reported; the allowlist only opens the *write* path.
2. **`--sync` for `retire.*`** — deletions get a second, per-run gate. A
   default `remediate` is purely additive/overwriting; removing objects is
   always an explicit choice made *this run*.
3. **No gate can exist for advisories** — `extra.*` kinds have no applier.
   That is the strongest form of default-deny: for derived-model-owned work,
   there is nothing to switch on ([Extra objects](extra-objects.md)).

## The sharpest example: named shared expressions

`expressions.tmdl` shows why "cascade the whole surface" is untenable. The
file mixes:

- genuinely shared parameters — a reporting window start, feature toggles —
  that *should* track the template, and
- per-tenant connection parameters — warehouse name, project id, currency —
  that must **never** be touched, plus tenant-only expressions.

So the expression allowlist (`allowlist.expressions`) names the shared set
explicitly, and everything else is invisible to the tool — not compared,
not flagged, not cascadable. Gating detection itself (not just remediation)
is deliberate: a "finding" on a connection parameter would train someone to
allowlist it just to silence the noise. The fixture fleet demonstrates it:
`Data Source` differs in every franchise and is never reported.

## Choosing your allowlist

- Start additive-only: the `*.missing` kinds. Gaps are the safest cascades.
- Add the overwrite kinds (`*_drift`) once you trust your template hygiene —
  overwrites assume the template is right, which is a process claim, not a
  tool claim.
- Add `retire.*` last, after the ledger has history you've reviewed.
- Scope expression cascading to parameters you would be comfortable setting
  identically in every tenant *today*.
- When a kind keeps getting `--kind`-filtered out of runs by hand, that's
  the signal to remove it from the allowlist — make the policy live in the
  file, not in habits.
