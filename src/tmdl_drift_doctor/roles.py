"""Table roles and measure classes: the ONE definition every check uses.

Configuration (``table_roles`` in fleet.yml) is authoritative; the naming
and partition conventions below are the fallback when a table or measure
is not listed:

* a **fact table** reads a Power Query (``m``) partition and its name does
  not start with ``_`` (dimensions), ``#`` (measure containers) or a
  report-scaffolding prefix (``x-``, ``y-``, ``z-``, ``f-``, ``i-``);
* a **filter measure** (``f-`` measure) is named ``f-...``;
* a **helper measure** starts with one of the helper prefixes (``-``,
  ``_``, ``!``, ``#``, ``^``, ``f-``, ``i-`` by default);
* every other measure is **user-facing**.
"""

from __future__ import annotations

from .tmdl import Model

HELPER_PREFIXES = ("-", "_", "!", "#", "^", "f-", "i-")
SCAFFOLD_PREFIXES = ("_", "#", "x-", "y-", "z-", "f-", "i-")


class Roles:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        roles = cfg.get("table_roles") or {}
        self.facts = set(roles.get("fact") or [])
        self.not_facts = set(roles.get("dimension") or []) | set(
            roles.get("other") or [])
        vis = cfg.get("visibility") or {}
        self.helper_prefixes = tuple(vis.get("helper_prefixes")
                                     or HELPER_PREFIXES)

    def is_fact(self, table) -> bool:
        if table.name in self.facts:
            return True
        if table.name in self.not_facts:
            return False
        if table.name.startswith(SCAFFOLD_PREFIXES):
            return False
        return any(p.mode == "m" for p in table.partitions.values())

    def fact_tables(self, model: Model) -> list:
        return sorted(t.name for t in model.tables.values() if self.is_fact(t))

    def is_helper(self, name: str) -> bool:
        return name.startswith(self.helper_prefixes)

    @staticmethod
    def is_filter(name: str) -> bool:
        return name.startswith("f-")

    def measure_classes(self, model: Model) -> dict:
        names = [m for t in model.tables.values() for m in t.measures]
        helpers = [n for n in names if self.is_helper(n)]
        return {"all": names, "filter": [n for n in names if self.is_filter(n)],
                "helper": helpers,
                "user_facing": [n for n in names if not self.is_helper(n)]}
