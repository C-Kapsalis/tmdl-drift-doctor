# Marketing / launch material — tmdl-drift-doctor

Launch copy for X/Twitter and LinkedIn. All claims are grounded in the repo:
the concrete demo is the bundled gym-chain example fleet
(`examples/fleet/`), where `detect` finds **9 drifts** across two franchises
and a default `remediate` applies **8** and skips the **1** advisory. No
invented metrics — everything below is reproducible from the example.

---

## Positioning tagline

**Git for the gap between your template model and its client copies.**
Drift detection and allowlist-gated auto-remediation for fleets of TMDL Power
BI semantic models.

Alternates:
- *One template, N client models, zero folder-diffing.*
- *Cascade your golden model's truth back to the fleet — safely, auditably.*

---

## Problem → solution hook

**The problem.** You maintain one template ("golden") Power BI model and ship
a copy to every client. Then reality sets in: last month you fixed the
`Total Visits` measure in the template — but fourteen client models still
carry the old DAX. Someone hand-edited client 7's format strings and nobody
remembers why. A table you retired in March still haunts three tenants. And
"are the clients up to date?" is an afternoon of eyeballing folder diffs,
every time.

**The solution.** `tmdl-drift-doctor` turns that afternoon into three
commands. **`capture`** a baseline of the template. **`detect`** typed drift
findings in every derived model. **`remediate`** — an auditable, allowlist-
gated, `--dry-run`-able cascade of template truth back out to the fleet.
Deletions are double-gated behind `--sync`, and every applied fix lands in an
append-only ledger. It reads and writes plain TMDL files, so it drops into any
git-based deployment pipeline.

---

## X / Twitter launch thread (7 posts)

**1/**
If you maintain one "golden" Power BI model and ship a copy to every client,
you know the dread: *are the clients actually up to date?*

Today it's an afternoon of diffing folders. Meet tmdl-drift-doctor — drift
detection + safe auto-remediation for fleets of TMDL semantic models. 🧵

**2/**
The pain is specific. You fix a measure in the template. Fourteen client
copies still carry the old DAX. Someone hand-edited a format string months
ago. A retired table still haunts three tenants.

One template, N copies, and they quietly drift apart.

**3/**
tmdl-drift-doctor is three commands:

• `capture` — snapshot the template into a committed baseline
• `detect` — typed drift findings for every client model (read-only, CI-ready)
• `remediate` — cascade the template's truth back out to the fleet

**4/**
The demo ships in the repo: a fictional gym chain, one template + two
franchises, drift pre-seeded.

`drift-doctor detect` finds 9 drifts — a missing measure, DAX that diverged,
a format string someone changed, a dropped column, a missing lookup row, and
more — grouped per model.

**5/**
Then `remediate --dry-run` shows you the exact unified diffs. Zero writes.

Run it for real and it applies 8 fixes and *skips* the 1 that's a legitimate
franchise-only extension — extras are advisory, never auto-deleted. Every
applied fix is written to an append-only ledger.

**6/**
Safety is the whole point:

• Nothing cascades unless you allowlist the drift kind
• Deletions need a second gate: `--sync` + proof-of-retirement in the ledger
• Stale baseline? It refuses to run rather than resurrect a retired object
• Raw-block TMDL surgery preserves lineage tags & formatting

**7/**
It's files-only — no workspace/XMLA integration — so it slots into any
git-based Power BI deployment flow. `detect` exits non-zero on drift, so it
wires straight into CI.

MIT-licensed. Python. Try the gym-fleet demo in 60 seconds. 👇
[repo link]

---

## LinkedIn post

**Shipping one Power BI template to many clients? Your copies are drifting
apart right now — and you can't see it.**

Here's the pattern I kept hitting: you maintain one "golden" TMDL semantic
model and derive a copy per client. Then you fix a measure in the template.
Fourteen client models still carry the old DAX. Someone hand-edited a client's
format strings months ago. A table you retired never got removed downstream.
Answering "are the clients up to date?" becomes an afternoon of manually
diffing folders — and you're never quite sure you caught everything.

So I built **tmdl-drift-doctor**, an open-source Python CLI that turns that
into three commands:

• **capture** a baseline of your template model
• **detect** typed drift findings in every derived model (read-only, and
  CI-ready — it exits non-zero when there's remediable drift)
• **remediate** — an auditable cascade of the template's truth back out to the
  whole fleet

The safety model is the part I'm proud of. Nothing cascades unless you
explicitly allowlist that kind of drift. Deletions are double-gated — they
require a `--sync` flag *and* proof that the object was genuinely retired from
the template, recorded in an append-only ledger. `--dry-run` shows you the
exact diffs before anything is written. And if your baseline is stale because
the template changed, it refuses to run rather than silently resurrect a
retired object.

The repo ships a runnable demo — a fictional gym chain with two franchises and
drift pre-seeded. `detect` finds 9 drifts; a default `remediate` fixes 8 and
correctly skips the one franchise-only extension, logging every change to the
ledger.

It works on plain TMDL files, so it drops into any git-based deployment
pipeline. MIT-licensed. Link in the comments — I'd love feedback from anyone
running Power BI at fleet scale.

#PowerBI #DataEngineering #Analytics

---

## Five one-liner hooks

1. You fixed the measure in the template. Did all 14 client copies get the
   fix? tmdl-drift-doctor knows.
2. "Are the clients up to date?" shouldn't be an afternoon of folder-diffing.
   Make it one command.
3. Cascade your golden Power BI model to the whole fleet — and never
   accidentally delete a client's own work.
4. Drift detection for Power BI semantic models, with a dry-run and an
   append-only audit trail. Deletions double-gated by design.
5. One template, N client models. tmdl-drift-doctor tells you exactly where
   they diverged — then fixes it, on your terms.

---

## Hashtags

Primary: `#PowerBI` `#TMDL` `#DataEngineering` `#Analytics` `#SemanticModel`

Secondary / thread: `#MicrosoftFabric` `#BusinessIntelligence` `#DataOps`
`#OpenSource` `#Python` `#DAX` `#PowerBIDeveloper`
