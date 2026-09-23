# Handoff: GENE lane added to profile_columns.py (2026-09-23)

Written from a session that took place in the `neuromuscular-disease-ontology`
repo's Claude Code context (its memory system does not cover this repo), while
editing files here. If you're picking this up as a fresh session scoped to
this project, paste this file's contents in, or just point Claude at it.

## What changed and why

`profile_columns.py`'s column-triage pipeline had a known gap (see
`project_spreadsheet2care` / `project_nmdo_search` memory in the other repo,
or just `git log`/`git blame` here going forward): a "Gene" column (real
symbols like SCN4A, PMP22, COL6A3...) was being sent through the semantic
SEARCH lane against nmdo-search and correctly REJECTED by the hit-fraction
guard (0.10 hit-fraction on the real partner file) — but for the wrong
underlying reason. It wasn't a search-quality problem: gene symbols are
short, precise codes with too little semantic content for a sentence
embedder to reliably discriminate, even after a separate real bug (HGNC
classes living under `identifiers.org/hgnc/` instead of the OBO PURL
namespace `OwlParser` expected) was fixed on the nmdo-search side. Semantic
search is structurally the wrong tool for this data type regardless of
indexing coverage.

**Fix: route gene columns around semantic search entirely**, using
mygene.info as an external identifier-resolution service instead. Full
design reasoning and empirical tests behind every decision below are in the
conversation transcript this file was written from (not reproduced here,
just the load-bearing facts).

## What's now in profile_columns.py

- `GENE_PATTERNS` — header regex (`gene`, `genes`, `hgnc`, `locus`, `loci`).
  Deliberately does NOT match "Genetic confirmation" (word-boundary catches
  "gene" but not "genetic"); that column is a Yes/No flag, correctly still
  routed to BOOLEAN.
- `GeneResolver` / `StubGeneResolver` — batched mygene.info client (POST
  `/v3/query`, up to its documented 5000-term cap per request — in practice
  always ONE round trip per gene column, since distinct gene values in any
  real dataset are bounded by the disease-gene panel size, nowhere near
  5000) + an offline stub (19-gene lexicon captured live 2026-09-23) for
  `--offline`/dry-run use.
- `resolve_gene_column()` — the actual resolution logic:
  1. Header hint first (HGNC / Entrez / NCBI Gene ID keywords) — free, most
     reliable.
  2. Symbol-shaped values (contain a letter) → resolved directly via
     `scopes=symbol,alias,retired,ensembl.gene`. Validated safe: only bare
     NUMBERS collide across ID spaces, not symbols.
  3. Purely numeric values with NO header hint → sample ~20 distinct values,
     probe `scope=HGNC` and `scope=entrezgene` SEPARATELY (never combined —
     see gotcha below), require ≥95% hit-fraction AND a ≥20-point margin
     over the other scope to trust a guess. **This branch is ALWAYS forced
     to low confidence / human review**, even when the guess looks clean —
     a live test showed the WRONG scope alone still "hits" ~79% of the time
     on real HGNC numbers, each one a different, confidently single-matched
     WRONG gene. Too close to the 95% bar to trust unsupervised.
- Per-value `unresolved` / `ambiguous` tracking, surfaced in the report and
  in `proposed_mappings` (feeds the curation workbook same as other lanes).
  `species=human` is hardcoded everywhere, never configurable — this
  pipeline is human-patient data only.

## Real gotchas discovered empirically (all reproducible, see live curl tests)

1. **mygene.info's default scopes do NOT include `symbol`** (`entrezgene,
   ensemblgene,retired` only) — a bare-symbol query with no explicit
   `scopes` param silently returns `notfound`. Never rely on the API
   default; always pass an explicit scopes list.
2. **Combining `HGNC` and `entrezgene` scopes in one query is dangerous.**
   Querying the bare number `10591` under both scopes at once returns TWO
   results tagged with the identical query string — one correct (HGNC 10591
   = SCN4A) and one a completely different, confidently single-matched gene
   (Entrez 10591 = DNPH1). No multi-match count or notfound marks the wrong
   one; a naive "take the result" parser would silently pick whichever
   comes first. Always query one scope at a time for numeric IDs.
3. **identifiers.org SPARQL and Bioregistry are NOT identifier resolvers**
   — both were seriously considered and rejected after live testing.
   identifiers.org's graph has zero literals (only `owl:sameAs` between URI
   spellings of an ID you already have); Bioregistry's `/api/reference/`
   just echoes a symbol into provider URL templates without ever returning
   the numeric HGNC ID. Neither has a path from a raw string like "SCN4A"
   to anything. mygene.info is the only one of the three that's an actual
   cross-reference database.
4. **A single scope can itself be ambiguous even for a real, correctly-
   typed symbol.** `PMP22` resolved to TWO hits under
   `symbol,alias,retired,ensembl.gene` — the real PMP22 gene (HGNC 9118,
   Charcot-Marie-Tooth-associated) AND PXMP2 (HGNC 9716), because "PMP22"
   is also a legacy alias for PXMP2. The wrong one (PXMP2) scores HIGHER
   (18.78 vs 18.18) — top-1-by-score would have picked wrong. This is why
   `resolve_gene_column` treats >1 hit as `ambiguous` and never just takes
   the top score.

## Verified against real data

Live end-to-end run against `mockdata/FakeData_sNMD1(Singular items).mock`'s
real `Gene` column (19→21 distinct real gene symbols including TARDBP,
VCP): 20/21 resolved cleanly, 1 correctly flagged `ambiguous` (PMP22, see
above). `Genetic confirmation` column unaffected (still BOOLEAN). Synthetic
stress-test file (no gene column) byte-identical to before the change — no
regressions.

## Still open / not done this session

- **Not wired into `build_care_template.py`** — this only adds the GENE
  lane to the read-only `profile_columns.py` triage step (classification +
  proposed mappings for the curation workbook). No CARE-SM template rows
  are emitted for gene data yet. Note the CARE-SM v2 `Genetic` model's
  actual schema (`implementation/CSV/Genetic.csv` in
  CARE-Semantic-Model-Version-2) is built around variant-level HGVS
  records + zygosity (GENO ontology terms) + a sequencing-method
  `activity` field — NOT a simple gene-symbol-to-IRI mapping. Real partner
  data (just a bare gene symbol per patient, no HGVS/zygosity/method) does
  not fit that schema directly. Whether/how to emit anything into the
  `Genetic` CARE-SM model — or whether the resolved HGNC IRI is meant to
  feed something else entirely (e.g. just the curation workbook / data
  dictionary, or attached as evidence on another model) — is an open
  design question, not decided this session.
- mygene.info vs HGNC REST API (`rest.genenames.org`) as the resolver
  choice was never finally settled (per the other repo's `project_nmdo_
  search` memory) — mygene.info was used here because it was already
  validated format-agnostic; no comparative test against the HGNC REST API
  was run in this session.
- No CLI flag to override the mygene.info URL (unlike `--search-url` for
  nmdo-search) — hardcoded `MYGENE_URL` constant. Add one if testing
  against a mock/local mygene-compatible service ever becomes necessary.

## Files touched

- `profile_columns.py` only (this directory). No other files changed.
