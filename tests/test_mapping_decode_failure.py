"""DC-5: a mapping table that cannot be decoded fails loudly.

When a derived model's encoded mapping table cannot be decoded, the tool
swallowed the error and the whole mapping comparison quietly disappeared.
The run then reported no mapping drift at all, and the user read a clean
report about a table the tool could not read.

The command must refuse: report the failure, name the model and the table,
and exit with the parse-failure code, exactly as it does for any other
model it could not read.
"""

from __future__ import annotations

from pathlib import Path

from port_helpers_b6_change import (
    SOURCE_IDENTITY,
    ZMAP_TABLE,
    install_zmap,
    zmap_tmdl,
)
from test_pristine_purge import (
    MODEL,
    UNI_ZMAP_ROWS,
    _copy_pair,
    _detect,
    _fleet,
)

BROKEN_BLOB = "not-base64-at-all"


def _break_the_zmap(definition: Path) -> None:
    """An encoded mapping table whose blob does not decode."""
    path = definition / "tables" / f"{ZMAP_TABLE}.tmdl"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Binary.FromText", "Binary.FromText"),  # no-op
                    encoding="utf-8")
    # replace the encoded payload with something that cannot decode
    import re
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'(Binary\.FromText\(\s*")[^"]*(")',
                  lambda m: m.group(1) + BROKEN_BLOB + m.group(2), text)
    path.write_text(text, encoding="utf-8")


def test_a_model_with_an_undecodable_mapping_table_is_an_error(tmp_path):
    """DC-5: the run refuses and names the model and the table."""
    from conftest import drift
    uni_model, growth_model = _copy_pair(tmp_path)
    _break_the_zmap(growth_model / "definition")

    r = drift("detect", fleet=_fleet(tmp_path, uni_model, growth_model))
    assert r.exit_code == 3, r.output
    assert MODEL in r.stderr, r.stderr
    assert ZMAP_TABLE in r.stderr, r.stderr


def test_the_run_does_not_report_no_mapping_drift(tmp_path):
    """DC-5: an unreadable table is never reported as free of drift."""
    from conftest import drift
    uni_model, growth_model = _copy_pair(tmp_path)
    _break_the_zmap(growth_model / "definition")

    r = drift("detect", fleet=_fleet(tmp_path, uni_model, growth_model))
    assert "no mapping drift" not in r.output, r.output
    assert "[ERROR]" in r.output, r.output


def test_a_decodable_mapping_table_is_unchanged(tmp_path):
    """DC-5: the other side of the rule."""
    from conftest import drift
    uni_model, growth_model = _copy_pair(tmp_path)
    install_zmap(growth_model / "definition", UNI_ZMAP_ROWS)

    r = drift("detect", fleet=_fleet(tmp_path, uni_model, growth_model))
    assert r.exit_code != 3, r.output
    assert "[ERROR]" not in r.output, r.output
