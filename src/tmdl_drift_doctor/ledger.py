"""The remediation ledger — append-only JSONL, with revival semantics.

Every consequential act leaves a line::

    {"ts": "...", "event": "retired",    "kind": "measure", "ref": "Visits[Peak Hour Visits]", ...}
    {"ts": "...", "event": "revived",    "kind": "measure", "ref": "...", "model": "alpha", ...}
    {"ts": "...", "event": "remediated", "kind": "retire.measure", "model": "alpha", "ref": "...", ...}

Why the ledger exists — the template is the canonical CORE, not a superset:
derived models legitimately extend beyond it, so an object's absence from the
template is NOT proof it should be deleted from a derived model. The only
population for which deletion is automatable is objects that PROVABLY used to
be in the template and were retired from it. ``retired`` events record exactly
that population; they are appended automatically at baseline recapture (see
``baseline.update_retirements``) and never removed.

Revival: a ``revived`` event cancels a retirement instead of deleting its
history. Two paths produce one:

* **automatic** — at recapture, an object that returned to the template is
  stamped revived (global);
* **manual** — ``drift-doctor ledger --revive`` marks an object a user
  deliberately re-added to a derived model, so the retirement channel will
  not re-remove it. A manual revival may be scoped to one model (``--model``)
  or apply fleet-wide.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import DriftDoctorError

RETIREMENT_KINDS = ("table", "column", "measure", "mapping_row")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """Append-only JSONL ledger. Reads fold the event stream into state."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ── writing ──────────────────────────────────────────────

    def append(self, event: dict) -> dict:
        event = {"ts": _now(), **event}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def record_retirement(self, kind: str, ref: str, **extra) -> dict:
        if kind not in RETIREMENT_KINDS:
            raise DriftDoctorError(
                f"invalid retirement kind: {kind!r}. Valid kinds: "
                f"{', '.join(RETIREMENT_KINDS)}.")
        return self.append({"event": "retired", "kind": kind, "ref": ref, **extra})

    def record_revival(self, kind: str, ref: str, model: Optional[str] = None,
                       source: str = "manual", note: str = "") -> dict:
        event = {"event": "revived", "kind": kind, "ref": ref, "source": source}
        if model:
            event["model"] = model
        if note:
            event["note"] = note
        return self.append(event)

    def record_remediation(self, kind: str, model: str, ref: str,
                           action: str, files: Optional[list] = None) -> dict:
        return self.append({"event": "remediated", "kind": kind, "model": model,
                            "ref": ref, "action": action,
                            "files": files or []})

    # ── reading ──────────────────────────────────────────────

    def entries(self) -> list:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise DriftDoctorError(
                    f"{self.path}:{i + 1}: malformed ledger line ({e}). The "
                    f"ledger authorizes removals, so a corrupt line stops "
                    f"every read rather than risk mis-targeting one. Repair "
                    f"that line or restore the file from version control, "
                    f"then retry.") from e
        return out

    def active_retirements(self, model: Optional[str] = None) -> dict:
        """{(kind, ref): retired_event} still in force for `model`.

        Folds the stream in order: a ``retired`` event opens a retirement; a
        later ``revived`` event closes it — globally when the revival carries
        no model, or for that one model only when it does. A retirement
        re-recorded after a revival is in force again.
        """
        state: dict = {}
        for e in self.entries():
            key = (e.get("kind"), e.get("ref"))
            if e.get("event") == "retired":
                state[key] = e
            elif e.get("event") == "revived":
                scope = e.get("model")
                if scope is None or scope == model:
                    state.pop(key, None)
        return state

    def is_retired(self, kind: str, ref: str, model: Optional[str] = None) -> bool:
        return (kind, ref) in self.active_retirements(model)

    def retired_refs(self) -> set:
        """(kind, ref) pairs ever retired and not globally revived — used by
        recapture to avoid duplicate `retired` events."""
        return set(self.active_retirements(model=None))
