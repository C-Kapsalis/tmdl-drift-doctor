"""The interactive walk's `[n/total]` counter counts the questions asked.

The total was every admitted finding, including the `[manual]` items (a
kind with no automated fix), the advisories and the items a gate skips:
none of them is ever asked, so a walk of 13 questions ended at `[13/17]`.
"""

from __future__ import annotations

from conftest import detect_all

from tmdl_drift_doctor.detect import Finding
from tmdl_drift_doctor.remediate import remediate


def test_the_counter_total_is_the_number_of_questions(captured):
    findings = detect_all(captured)
    findings.append(Finding(kind="nofix.example", model="alpha",
                            ref="Visits[Manual Item]",
                            detail="a kind with no automated fix"))
    asked = []

    def confirm(f, label, changes, position=None):
        asked.append(position)
        return True, ""

    result = remediate(captured, findings, confirm=confirm)
    assert result.count("manual") == 1
    assert asked, "nothing was asked"
    total = len(asked)
    assert asked == [(n, total) for n in range(1, total + 1)], asked
    assert result.count("applied") == total
