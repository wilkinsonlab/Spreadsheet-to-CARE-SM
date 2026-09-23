# Changelog

Notable changes to the spreadsheet-to-CARE-SM pipeline. Dates are when the
work landed on this branch, not when it was authored.

## 2026-09-23 — Gene/variant/zygosity: a fourth column-routing lane, and Genetic emission

- **GENE lane**: gene-symbol/gene-ID columns (`GENE_PATTERNS` header keyword) are routed
  around semantic search entirely and resolved via `mygene.info` instead
  (`resolve_gene_column`) — short precise codes like `SCN4A` carry too little semantic
  content for an embedder to discriminate reliably, regardless of index coverage.
- **VARIANT lane**: HGVS/dbSNP notation is detected by value shape (`VARIANT_VALUE_RE`),
  not header — partner header naming for a variant column is expected to be
  inconsistent, so the header keyword (`var`/`variant`/`allele`) is confirmatory only.
  When the two signals disagree, the column goes to human review rather than being
  auto-classified either way. Added gene-from-variant resolution
  (`resolve_variant_column`): a transcript accession (`NM_`/`NR_`/`XM_`/`XR_`, version
  suffix stripped — mygene.info's batch endpoint is unreliable with it on at least one
  real record) resolves via `mygene.info?scopes=refseq`; a dbSNP rsID resolves via
  `myvariant.info` then `mygene.info` (symbol→HGNC). Bare coding-DNA/protein notation
  with no accession, and chromosome-level `NC_`/`NG_` accessions, are confirmed
  unresolvable by any lookup service (not a tooling gap — the notation itself doesn't
  carry enough information) and correctly fall to human review.
- **ZYGOSITY lane**: header-first (`zygosity`/`allele state`/...), resolved against a
  GENO-ontology vocabulary (`resolve_zygosity_value`) built from expected clinical
  shorthand rather than GENO's own near-empty synonym set. Deliberately conservative:
  exact-match-after-normalization only (never substring, to avoid e.g. "heterogeneous"
  matching "het"), and several ambiguous terms (`biallelic`/`monoallelic`, "chet") are
  deliberately excluded rather than guessed at.
- **`build_genetic()`** added to `build_care_template.py`: emits CARE-SM v2 Genetic rows
  per the gene/variant/zygosity presence-or-absence truth table (gene alone → default
  unspecified zygosity; gene+variant; gene+zygosity; all three; variant alone → gene
  derived from its own accession/rsID; zygosity alone → nonsense, skipped). A row with no
  resolvable gene is skipped, never emitted with `target` blank — matches a corresponding
  fix on the CARE-SM v2 model side making `Genetic.target` Mandatory (see the
  CARE-Semantic-Model-Version-2 changelog).
- **Mapping tables extracted to JSON** (`column_mappings.json`, `care_template_mappings.json`):
  header-trigger patterns and value vocabularies used to live hardcoded in
  `profile_columns.py`/`build_care_template.py`; now a curator can see/edit what maps to
  what without reading Python. Toolkit-contract tables (`TOOLKIT_COLUMNS`/`MODEL_COLUMNS`)
  deliberately stay in code — they mirror an external system's enforced contract, not a
  curation decision.
- **Bug fix**: `build_care_template.py`'s `main()` never passed `gene_resolver`/
  `myvariant_resolver` into `classify()`, so a live (non-`--offline`) run silently
  resolved GENE/VARIANT columns against the tiny offline stub lexicons instead of
  mygene.info/myvariant.info — `--offline` and a live run behaved identically by
  accident. Fixed via a new `build_resolvers()` helper; regression test added
  (`test_build_care_template.py`).

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
