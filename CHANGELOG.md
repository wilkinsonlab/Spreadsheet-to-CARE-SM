# Changelog

Notable changes to the spreadsheet-to-CARE-SM pipeline. Dates are when the
work landed on this branch, not when it was authored.

## 2026-08-25 — Presentation materials
- Added `talk/spreadsheet-to-care-deck.pptx`: a native, editable slide deck
  covering the CARE-SM v1→v2 model change, the pipeline's original
  speculations vs. what actually held up, the column-triage decision tree,
  the search guard, and results across both datasets.
- Added `talk/DECISION-WORKFLOW.md`: a textual, step-by-step account of every
  triage decision in `profile_columns.py::classify()` — what each rule
  checks, whether and how it calls the ontology search (none / single
  unguarded lookup / guarded multi-sample hit-fraction), and the exact
  thresholds involved. Companion to the deck, not a replacement for reading
  the code.

## 2026-07-27 — CARE-SM v2 migration
- Migrated `build_care_template.py` to target CARE-SM v2's enforced column
  set (`target`/`attribute_type` carry the ontology code; `value` is a typed
  literal; no `valueIRI`/`specification` columns).
- Implemented negative-observation capture: a Yes/No flag now emits
  `value=false` instead of being silently dropped. Phenotype rows went from
  40→68 (synthetic) and 18→120 (partner) — 102 previously-discarded
  confirmed-absent findings recovered.

## 2026-07-21 — Pipeline documented end to end
- Added `PIPELINE.md`: workflow diagram, column-routing decision tree,
  resource inventory, stage-by-stage detail, and validation measurements.
- Captured the presentation-materials link in `README.md`.

## 2026-07-16 — Initial pipeline
- Added `profile_columns.py` (column triage), `build_curation_workbook.py`
  (human-review `.xlsx` with locked verdict drop-down), and
  `build_care_template.py` (CARE-SM v1 template emission).
- Added `SYNTHETIC-TEST-DATA-COOKBOOK.md`, the methodology for building
  adversarial test data.
