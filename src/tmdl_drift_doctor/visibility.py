"""Measure visibility: a user-facing measure must be visible, a helper must
be hidden.

The class of a measure is decided by its NAME alone — the way tmdl-preflight's
O009 and O011 rules decide it — so a measure that was hidden by accident
still reads as user-facing and the visibility rule can catch it. Visibility
itself (``isHidden``) is a separate thing, and the two never mask each other.

* ``visibility.hidden_user_facing``: a user-facing-named measure that is
  hidden. Either it was hidden by mistake (show it) or it is really a helper
  (rename it with a helper prefix). The repair removes the hidden flag;
* ``visibility.helper_visible``: a helper-named measure that is visible and is
  not on the always-visible allowlist. The repair hides it.

Both repairs change what a report author sees, so neither is batch-safe: they
need an interactive answer, like every other repair that changes meaning.

A fleet may add names to the always-visible allowlist
(``visibility.always_visible``) and replace the built-in helper test with its
own prefixes (``visibility.helper_prefixes``).
"""

from __future__ import annotations

import re
from typing import Optional

from . import DriftDoctorError
from . import tmdl_edit as te

#: helper-named measures that are canonically visible: the infrastructure
#: set a report binds to directly.
ALWAYS_VISIBLE = (
    "f-All",
    "f-Slicer",
    "f-Matrix Title",
    "i-On-Plot Title",
    "i-On-Plot Subtitle",
    "i-HTML x/y Plot Specific",
    "i-HTML x/y Selector-2",
    "i-Applied Filters",
    "i-Selected Timeframe",
    "i-Selected Analysis Category",
    "i-Diagnostic Measures",
    "i-Tooltip",
    "i-Enhanced Timeframe",
    "i-Enhanced Timeframe HTML",
    "i-Selected Timeframe HTML",
)

#: the letters whose ``<letter>-`` names are user-facing, not helpers
USER_FACING_LETTERS = ("z",)

#: scaffolding names that look user-facing but are never "hidden by mistake"
NOISE_EXACT = ("max", "min", "value")
NOISE_PREFIXES = ("max ", "min ")
NOISE_SUBSTRINGS = ("blank", "date", "day", "countrows", "filter", "calendar",
                    "facebook")

_TEST_WORD = re.compile(r"\btest", re.IGNORECASE)


def is_helper_name(name: str, prefixes=None) -> bool:
    """Is the name a helper's name?

    A name is a helper when it starts with ``-`` or ``_``; when its second
    character is a dash after a letter that is not a user-facing one (``z``);
    or when it carries the word ``test`` at a word boundary. Every other name
    is user-facing, the sigils ``!``, ``#`` and ``^`` included. When the fleet
    declares its own helper prefixes, they replace the built-in test.
    """
    if not name:
        return False
    if prefixes:
        return name.startswith(tuple(prefixes))
    if name[0] in "-_":
        return True
    if (len(name) >= 2 and name[1] == "-" and name[0].isalpha()
            and name[0] not in USER_FACING_LETTERS):
        return True
    return bool(_TEST_WORD.search(name))


def is_noise_name(name: str) -> bool:
    """A user-facing name that is scaffolding: a measure a developer hides on
    purpose, never one hidden by mistake."""
    if not name:
        return False
    low = name.lower()
    if low in NOISE_EXACT:
        return True
    if low.startswith(NOISE_PREFIXES):
        return True
    if "_" in name:
        return True
    return any(token in low for token in NOISE_SUBSTRINGS)


def is_hidden(measure) -> bool:
    """Is the measure hidden? ``isHidden`` is written as a bare flag or with
    a value; both mean the same here."""
    value = (measure.properties or {}).get("isHidden")
    if value is None:
        return False
    return str(value).strip().lower() == "true"


def options(fleet) -> dict:
    """The visibility options a fleet declares."""
    conf = fleet.setting("visibility") or {}
    if not isinstance(conf, dict):
        conf = {}
    always = set(ALWAYS_VISIBLE) | {str(n) for n in conf.get("always_visible")
                                     or ()}
    return {"always_visible": always,
            "helper_prefixes": tuple(conf.get("helper_prefixes") or ())}


def findings(name: str, fleet, model, claimed: Optional[set] = None) -> list:
    """The visibility findings for one model.

    `claimed` holds the measure names a rename explains: a measure that is
    about to be renamed is not also reported as badly visible."""
    from .detect import Finding
    conf = options(fleet)
    always = conf["always_visible"]
    prefixes = conf["helper_prefixes"]
    out = []
    for tname, table in sorted(model.tables.items()):
        for mname, measure in sorted(table.measures.items()):
            if claimed and mname in claimed:
                continue
            ref = f"{tname}[{mname}]"
            hidden = is_hidden(measure)
            if is_helper_name(mname, prefixes):
                if hidden or mname in always:
                    continue
                out.append(Finding(
                    kind="visibility.helper_visible", model=name, ref=ref,
                    detail=(f"{ref} has a helper NAME but is visible and is "
                            f"not on the always-visible allowlist — "
                            f"auto-hiding."),
                    data={"table": tname, "measure": mname}))
            elif hidden and not is_noise_name(mname):
                out.append(Finding(
                    kind="visibility.hidden_user_facing", model=name, ref=ref,
                    detail=(f"{ref} has a user-facing NAME but is hidden. "
                            f"Either it was hidden by mistake (show it) or it "
                            f"is really a helper (rename with a helper "
                            f"prefix — a cascade rename)."),
                    data={"table": tname, "measure": mname}))
    return out


def apply_visibility(fleet, f) -> tuple:
    """Hide a helper-named measure, or show a user-facing one.

    The file's own bytes are kept: its line endings and its byte-order mark
    survive, so a fleet committed from Windows does not come back as a
    whole-file diff."""
    from .remediate import FileChange
    from .detect import split_ref
    table, obj = split_ref(f.ref)
    path = fleet.model_definition(f.model) / "tables" / f"{table}.tmdl"
    if not path.is_file():
        raise DriftDoctorError(
            f"the table file {path} is gone; re-run `drift-doctor detect`")
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    old = te.read_text(path)
    eol = te.line_ending(old)
    text = old.replace("\r\n", "\n")
    if f.kind == "visibility.helper_visible":
        new = te.insert_property_line(text, "measure", obj, "\t\tisHidden: true")
        label = f"hide helper measure {f.ref} (isHidden: true)"
    else:
        new = te.remove_property_line(text, "measure", obj, "isHidden")
        label = f"show user-facing measure {f.ref} (the hidden flag goes)"
    if new is None:
        raise DriftDoctorError(
            f"measure {f.ref} is no longer in {path.name}; re-run "
            f"`drift-doctor detect`")
    te.assert_tmdl_valid(new)
    if new == text:
        return (f"{f.ref} already reads as it should — no change", [])
    return (label, [FileChange(path, raw, te.encode(new, eol, bom))])
