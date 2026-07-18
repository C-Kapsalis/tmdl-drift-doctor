"""tmdl-drift-doctor — drift detection and remediation for TMDL model fleets.

One shared template semantic model, N derived per-tenant models. Derived
models drift: measures get edited, columns go missing, objects retired from
the template linger on. This package captures a baseline of the template,
detects typed drift findings in every derived model, and cascades template
truth back out — allowlist-driven, ledger-audited, and safe about TMDL text
surgery.
"""

__version__ = "0.1.0"


class DriftDoctorError(Exception):
    """Base error for all drift-doctor failures (config, parse, edit guard)."""


class StaleBaselineError(DriftDoctorError):
    """The live template no longer matches the committed baseline.

    Detection and remediation both read the baseline as template truth; acting
    on a stale one misclassifies drift (a retired object looks 'missing' and
    gets resurrected). Run `drift-doctor capture` after ANY template edit.
    """


class TmdlEditError(DriftDoctorError):
    """A guarded TMDL edit refused to persist (invalid or corrupting result)."""
