# PDF Tagging Ruleset — Formalized Specification

Derived from the internal "PDF Accessibility" tagging guide. Rules are split into two tiers:

- **[STRUCTURAL]** — deterministic, no ambiguity. Belongs in a rule engine / validator, not a model.
- **[SEMANTIC]** — requires visual/contextual judgment. This is what a fine-tuned classifier should learn.

Anywhere the source doc said "based on client requirements," it's flagged **[CONFIGURABLE]** — these need a per-client parameter, not a hardcoded value.

---

## 1. Root Structure

- **[CONFIGURABLE]** All tags live directly under `Document`, **or** under `Document > Section`, depending on client requirement.
- **[STRUCTURAL]** Whichever mode is selected, all content tags must nest inside that root — no orphan top-level tags outside it.

## 2. Document Properties (not part of tag tree, but required)

- Title = document's H1 / main title
- Author = per client requirement **[CONFIGURABLE]**
- Language = document language (default English) **[CONFIGURABLE]**
- Initial view: Page Only navigation, Single Page layout, Fit Page magnification, show document title

## 3. Headings (H1–H6)

- **[SEMANTIC]** Assign level by font size, visual hierarchy, and document structure.
- **[STRUCTURAL]** Largest/main title = H1. Subsequent levels strictly descend (H2, H3, H4...) — no skipping levels arbitrarily unless the client's hierarchy explicitly allows it.
- **[CONFIGURABLE]** Heading structure may vary by client — treat level-assignment thresholds as a per-client parameter.

## 4. Paragraph (P)

- **[SEMANTIC]** Use for any normal text that is not heading/list/table/figure/other structural element.
- **[STRUCTURAL]** One paragraph = one P tag (no splitting a single paragraph across multiple P tags). Avoid unnecessary nested tags inside P unless required.

## 5. Artifacts

- **[SEMANTIC]** Judgment call: repeated headers/footers, page numbers, chapter names, decorative images/lines/borders, background graphics/watermarks, repeated logos (unless conveying info), decorative shapes, crop/registration marks, blank formatting spaces → Artifact.
- **[STRUCTURAL] Exception rule:** if the PDF has no title/heading on page 1, the page-1 header must be tagged H1 (not Artifact); from page 2 onward the repeated header reverts to Artifact.
- **[CONFIGURABLE]** If header/footer content is unique/required by client, do not artifact — follow client instruction instead.

## 6. Figure (Image)

- **[STRUCTURAL]**
  - Figure tag wraps the image only — never the caption.
  - Caption tagged separately (typically P), sibling to Figure, not child.
  - One image per Figure tag (no bundling multiple images in one Figure).
  - Required attribute: **Alt Text** on every Figure (mandatory, not optional).
- **[SEMANTIC]** Alt text content: concise description of information/purpose, not exhaustive visual detail; include embedded important text. Decorative images → Artifact instead of Figure+Alt.

## 7. Lists

**[STRUCTURAL] Canonical structure:**
```
L
└── LI
    ├── Lbl
    │   └── Span      (unordered lists only)
    └── LBody
        └── P
```
- Ordered list: `Lbl` contains the actual numbering/lettering (e.g. "1.", "A.", "i.").
- Unordered list: `Lbl > Span`, Span's Actual Text is the literal string `"Bullet "` (note mandatory trailing space — affects screen-reader pronunciation).
- **[STRUCTURAL] List-break rule:** if any non-list content (paragraph, heading, figure, table, etc.) interrupts a sequence of list items, the list (`L`) must be closed before that content and a new `L` opened after. Lists may never "wrap around" non-list content.

## 8. Tables

- **[STRUCTURAL]**
  - Every table requires at least one header (row and/or column) — a headerless table is non-compliant, full stop.
  - `TH` for header cells, `TD` for data cells.
  - Caption tagged outside the `Table` tag (sibling, not child).
  - Single header row → flat structure:
    ```
    Table
    ├── TR (TH, TH, TH...)
    ├── TR (TD, TD, TD...)
    └── ...
    ```
  - Multiple header rows → use `THead`/`TBody` split:
    ```
    Table
    ├── THead (TR, TR...)
    └── TBody (TR, TR...)
    ```
    Never use THead/TBody for single-header-row tables.
  - Cell attributes: `Scope` = Column or Row on every header cell; `ColSpan`/`RowSpan` set to the exact spanned count for merged cells.
- **[SEMANTIC]** Reading order within the table must match visual/logical structure (matters for irregular merged-cell layouts).

## 9. Links

**[STRUCTURAL] External link:**
```
P
└── Link
    ├── Content
    └── OBJR
```
**[STRUCTURAL] Internal link (references, footnotes, TOC entries):**
```
P
└── Reference
    └── Link
        ├── Content
        └── OBJR
```
Rules (both types):
- Link tag must always have a parent (never root-level).
- Exactly one OBJR per Link tag — never more.
- If a Reference is already nested inside another structural tag (e.g. `TOCI`), do not add a redundant P wrapper.
- Every Link requires Alt Text.

## 10. Footnotes / References / Endnotes

**[STRUCTURAL] Reference marker (in main content):**
```
P
└── Reference
    └── Link
        ├── Content
        └── OBJR
```
**[STRUCTURAL] Note (the actual footnote/endnote text):**
```
P
└── Note
    ├── Lbl
    └── P
```
- Note tag must have a P parent; never root-level.
- Every Note requires a unique Note ID linking it to its Reference.
- **[CONFIGURABLE]** Two accepted placement methods — Note relocated adjacent to its Reference in the tag tree, OR Note left at its natural document location (footer/endnote section). Client/project decides which.

## 11. Formulas

- **[STRUCTURAL]** `P > Formula`. Never root-level. Requires **Actual Text** representing the mathematical expression (verbatim, use client-provided Actual Text if given — don't modify it).
- **[SEMANTIC]** Caption/equation number, if present, tagged separately per document structure (not inside Formula).

## 12. Span

- **[SEMANTIC]** Use for: bullet symbols (see Lists), fill-in-the-blank content, underlined text needing special handling, words incorrectly joined without a space, missing/duplicated/misread text.
- **[STRUCTURAL]** Every Span requires Actual Text. Trailing space appended where it affects pronunciation (e.g. "Bullet "). Use client-provided Actual Text verbatim when available.

## 13. Forms

- **[STRUCTURAL]** Each form field needs a meaningful Name/Tooltip (screen-reader label). Checkboxes/radios must expose checked/unchecked state and be keyboard-navigable.

## 14. Table of Contents (TOC)

**[STRUCTURAL]**
```
TOC
└── TOCI
    └── Reference
        └── Link
            ├── Content
            └── OBJR
```
- Every TOC entry = one TOCI.
- Each Link contains exactly one OBJR.
- Every TOC link must resolve to the correct page/heading target (this is a functional check, verifiable programmatically by validating the internal destination).

## 15. Reading Order

- **[SEMANTIC]** Default: top-to-bottom, left-to-right. Override where visual/logical layout genuinely requires a different sequence (multi-column layouts, sidebars, pull-quotes, etc.) — this is the single hardest judgment call in the whole pipeline and where most model error will concentrate.

---

## Implication for your pipeline

- Everything marked **[STRUCTURAL]** → implement as hard validation/construction logic (see accompanying `tagging_rules_schema.json`). Your model never needs to "learn" these — it just needs to emit the right semantic label and position, and the rule engine assembles the compliant tree deterministically.
- Everything marked **[SEMANTIC]** → this is your actual fine-tuning target. Notably smaller and more tractable than "learn the whole standard" — it's really: heading-level assignment, artifact-vs-content classification, alt-text generation, list-boundary detection, and reading-order sequencing.
- Everything marked **[CONFIGURABLE]** → needs a per-client settings object (root structure mode, heading thresholds, footnote placement method, header/footer exceptions) consumed by both the model prompt/context and the rule engine — not hardcoded either place.
