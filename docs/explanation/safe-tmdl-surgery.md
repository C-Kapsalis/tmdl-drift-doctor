# Safe TMDL surgery

**The rule: edit TMDL as raw text blocks, validate before every write, and
never, ever insert a property line into a DAX expression body.**

## Why raw-block splicing (not parse → re-serialize)

A diff-oriented parser keeps what it needs to compare and drops the rest —
lineage tags, annotations, format hints, description lines, the exact
formatting a desktop tool wrote. Rebuilding TMDL from such parsed objects
silently destroys everything the parser ignored. So the remediation engine
never serializes parsed dataclasses; it locates an object's **raw text
block** in the template file (declaration line, `///` descriptions,
properties, annotations — byte-faithful) and splices it into the derived
file. Parsing is for *deciding*; text surgery is for *doing*.

Two identity rules ride along:

- a block **inserted** into a model gets a fresh lineage tag (a copied tag
  can collide with an existing one, and the engine refuses to load a model
  with duplicate lineage tags);
- a block **overwriting** an existing object keeps the derived object's tag
  (stable identity; idempotent re-runs).

## The DAX-injection hazard

The one TMDL-editing mistake that earns its own invariant. TMDL expressions
come in three shapes:

```tmdl
	measure 'Simple' = COUNTROWS(T)          ← single-line
		formatString: #,0

	measure 'Fenced' = ```
			CALCULATE( ... )
			```                              ← fenced
		formatString: #,0

	measure 'Layered' =
			VAR _x = ...
			RETURN _x + 1                    ← bare multi-line
		formatString: #,0
```

Single-line and fenced bodies have an obvious "after the expression"
insertion point for a property. The trap is the **bare multi-line** shape:
a naive "insert the property right after the declaration line" puts it
between the `=` and the DAX —

```tmdl
	measure 'Layered' =
		isHidden                              ← now the FIRST LINE OF THE EXPRESSION
			VAR _x = ...
```

— and the engine reads `isHidden` as DAX. The measure fails to compile with
a bare syntax error. Worse, the edit *looks* plausible in a diff, and a
fleet cascade multiplies it: this exact bug once landed dozens of broken
measures across every model in a production fleet in a single automated
pass, discovered only when reports started erroring.

## How the invariant is enforced (twice)

1. **The writer does it right.** `insert_property_line` classifies the
   expression shape and inserts *after the whole body*: after the closing
   fence for fenced bodies, after the last body line for bare multi-line
   ones. It is also idempotent — an already-present property is never
   duplicated.
2. **The guard assumes the writer is wrong.** `assert_no_property_injection`
   independently scans content before *any* write: for every bare
   multi-line declaration, a property-keyword line appearing before the
   expression body (with body lines still following) raises, and the write
   is refused. The guard runs inside `assert_tmdl_valid`, so every code
   path that persists TMDL passes it — including block replacements that
   should be safe by construction.

Belt and suspenders is deliberate: the incident taught that "the writer
handles all the shapes" is exactly the claim that fails on the shape nobody
listed.

## The rest of the write guards

Every candidate file content must also have: tab indentation only (TMDL
rejects space-indented lines), balanced ``` fences, and no duplicated
single-value property within one object (a second `isHidden` is as fatal as
a misplaced one). Any violation raises before the file is touched — a
guarded edit either persists valid TMDL or persists nothing.
