# Column triage — the decision workflow, in full

This is the textual, no-diagrams companion to the slide deck. Slide 4 ("cheap,
deterministic checks first") and slide 5 ("a column, not a value, is judged")
compress this into pictures; this document is the ground truth they're drawn
from — every branch, every threshold, every place a search actually happens.

Source of truth: [`profile_columns.py`](../profile_columns.py), function
`classify()` (line ~212). It runs **once per column**, top to bottom, and
returns on the **first** rule that matches — so order is itself a decision:
cheap, unambiguous checks are tried before anything that costs a network call
or carries any doubt.

---

## 1. What "search" actually means here

Every "search" in this document is the same single operation, so it's worth
defining once instead of re-explaining it nine times.

- **What it searches against:** a hosted semantic (embedding) index built
  over **NMDO**, the Neuromuscular Disease Ontology — about **3,942 terms**:
  MONDO (~2,748, diseases), HGNC (~915, genes), UBERON (848, anatomy), HP
  (329, phenotypes), NCIT (43, misc. clinical terms). It is *not* a general
  biomedical search — if a concept isn't in NMDO's import list, no query
  wording will find it.
- **The call:** `HttpSearcher.top(query)` hits
  `https://simpathic.services/llm_search/search?q=<query>&top_k=3` and keeps
  only the **top-1** result (score, ontology prefix, label, IRI). Identical
  queries are cached in-process, so re-running a query string is free.
- **Offline mode:** `--offline` swaps in `StubSearcher`, a transparent
  keyword-overlap heuristic over an 11-term lexicon, used only for demos/CI.
  It is explicitly *not* a stand-in for validation — every quality number in
  the results slide came from the live embedder.
- **Two very different ways it gets used**, and this is the part worth being
  precise about:
  - **A single, unguarded lookup** — ask the search *one* question (usually
    "what does this header word mean?"), take the top hit if its score clears
    a bar, done. No statistics, because there's only one thing to resolve.
  - **A guarded, multi-sample lookup** — ask the search the same question
    *many times* (once per sampled cell value) and only trust the column if
    a *fraction* of those queries clear the bar. This is the only place the
    hit-fraction guard from slide 5 applies, because it's the only place a
    single answer can't be trusted (free text is ambiguous per-cell in a way
    a header word is not).

  Only step 9 (SEARCH, free text) uses the guarded form. Steps 6, 8, and 10
  make single lookups. Steps 1–5 and 7 never call the search at all. That
  distinction is the honest version of "only the last branch reaches
  semantic search" — the slide's shorthand for it.

- **Thresholds** (all CLI-overridable, calibrated against this embedder's
  low absolute scores — good matches sit around 0.5–0.8, not textbook ~0.9):

  | Name | Default | Meaning |
  |---|---|---|
  | `--score` | 0.50 | a single value/header lookup counts as a "hit" if its top score clears this |
  | `--hit-fraction` | 0.60 | a free-text column is accepted only if this share of its sampled values hit |
  | `--small-vocab` | 12 | at most this many distinct values to treat a column as a controlled vocabulary |
  | `--sample` | 20 | distinct values sampled per column before scoring (free-text lane only) |

---

## 2. The decision sequence

Each column goes through this in order. The first rule that matches wins —
later rules never see a column that an earlier rule already claimed.

### Step 0 — is the column empty?
**Rule:** no non-null values at all (post-load; an all-empty column is
already dropped at file-load time as an export artifact).
**Search:** none.
**Result:** `DROP`, confidence *high*.

### Step 1 — is the header a direct identifier (PII)?
**Rule:** header matches `PII_PATTERNS` — a regex for `first/last/middle/
maiden/sur name`, `full name`, `address`, `postcode`/`zip`, `phone`, `email`,
`NHS number`, `SSN`, `initials`.
**Search:** none — this is a lexical trap-door specifically so PII can never
reach the network call.
**Result:** `PII`, confidence *high* — dropped, never mapped, never even
searched.

### Step 2 — is this a patient/record key?
**Rule:** header matches `KEY_PATTERNS` (`patient id`, `pid`, `record id`,
`subject id`, or bare `id`) **OR** — a narrower fallback — the column is
`idx == 0` (the first column) *and* every value is numeric and distinct
(`n_distinct == n_values`, >90% numeric) *and* the header carries no unit
(no `(...)`/`[...]` suffix).
*Why the fallback is narrow:* an all-unique numeric column alone isn't
enough evidence — a lab result column can look exactly like that too. Position
(first column) plus absence of a unit is what keeps a CK-level column from
being mis-called a key.
**Search:** none.
**Result:** `KEY` → `pid`, confidence *high*.

### Step 3 — are the values already ontology codes (CURIE)?
**Rule:** ≥80% of values match `PREFIX:CODE` shape (letters, then `:`, then
alphanumerics — and the part before `:` must not start with a digit, which
excludes things like `10:30` timestamps).
**Model assignment:** take the most common prefix (e.g. `ORPHA`). Model is
set to `Diagnosis` if that prefix is `ORPHA`/`MONDO`/`OMIM`, **or** — and
this is a documented sharp edge — if the *header* matches the `diagnos`
keyword regex (see step 4's date-subtype list, reused here). That reuse
means a column literally named `Genetic confirmation diagnosis code` would
tag itself `Diagnosis` by header wording alone, independent of what the
codes actually are; it's flagged for review, not asserted blind.
**Search:** none — CURIEs are already codes, so there's nothing to look up.
**Result:** `CURIE`, confidence *high* (structure is unambiguous; only the
model guess is soft, hence the review flag on anything not ORPHA/MONDO/OMIM).

### Step 4 — do the values parse as dates?
**Rule:** ≥70% of values parse under one of `%Y-%m-%d`, `%d/%m/%Y`,
`%m/%d/%Y`, `%Y/%m/%d`, `%Y`.
**Subtype (which CARE-SM date model):** tried **by header keyword only**, in
this fixed order — first match wins:
1. `death|deceased|died` → `Deathdate`
2. `birth|dob|d\.o\.b` → `Birthdate`
3. `onset` → `Symptoms_onset`
4. `first\s*visit|enrol|baseline` → `First_visit`
5. `diagnos` → `Diagnosis`

If none match, the column is still `DATE` but with **no model** and
confidence *low* — "date column — meaning not resolvable by search" (the
note is explicit that search wouldn't help here even if we tried it: a bare
date string like `2019-03-04` carries no lexical content to embed).
**Search:** **none, ever.** This is the one lane that is genuinely,
completely search-free — it's pure regex-over-header, which is exactly what
the deck's shorthand claims for the *whole* tree but is only strictly true
for this one branch.
**A known trap:** rule 5 (`diagnos`) is greedy — a column named
`Cardiomyopathy diagnosis date` matches it even though the column is really
about *when cardiomyopathy was diagnosed*, not a diagnosis code. Caught by
the confidence flag, not prevented structurally.

### Step 5 — is this a Yes/No flag (boolean, header-as-concept)?
**Rule:** the column's *entire* value set is a subset of `BOOLEAN_TOKENS`
(`yes/no/y/n/true/false/positive/negative/present/absent/unknown/n/a/
carrier/confirmed/not confirmed`, case-insensitive) **and** there are ≤4
distinct values.
**Search:** **one lookup**, on the *header text* (cleaned: unit suffixes and
underscores stripped) — e.g. header `Cardiomyopathy` → one query.
**Deciding the model from the hit:**
- top hit prefix `hp` and score ≥ 0.50 → `Phenotype`
- top hit prefix `mondo` and score ≥ 0.50 → `Diagnosis`
- otherwise → no model, confidence *low*, still flagged `BOOLEAN` (the lane
  is certain; only the ontology mapping is unresolved)
**A known mis-fire:** a header like `Genetic confirmation` can hit an
unrelated NMDO term (observed: matched on the word "acquired" at 0.57) and
get mis-typed. This is exactly why BOOLEAN mappings still go through the
curation workbook rather than being asserted directly.
**Result:** `BOOLEAN`, confidence *medium* if a model was assigned, else
*low* — always flagged for review since it's a single, unguarded query.

### Step 6 — is this the sex column (special-cased small vocabulary)?
**Rule:** value set ⊆ `{male, female, m, f, intersex, other, unknown}` and
≤4 distinct values.
**Search:** **deliberately none.** The note in the code is explicit: *"small
controlled vocab → curated lookup (search is unreliable here)"* — single
letters/words like "M" or "F" embed too poorly to trust, so this is a
hard-coded lookup table instead (`male`→NCIT_C20197, `female`→NCIT_C16576,
in the downstream transform script) rather than anything probabilistic.
**Result:** `DICTIONARY` → `Sex`, confidence *high*.

### Step 7 — is this a numeric measurement (header-as-measurement)?
**Rule:** ≥60% of values are numeric (tolerating things like `"1200 U/L"` by
splitting on whitespace before parsing the leading number).
**Search:** **one lookup**, on the cleaned header — e.g. `10MWT` or `CK
(U/L)` → one query (unit suffix stripped first).
**Deciding the model from the hit** (only if score ≥ 0.50):
- hit label contains "test", "scale", or "walk" → `Examination`
- hit prefix is `hp` or `ncit` → `Laboratory`, but confidence only *low* —
  the code flags this explicitly as a known trap: **NMDO's analyte hits are
  the phenotype *form* of the concept** ("Extremely elevated creatine
  kinase" as an HP term), not a lab-test concept, so a genuine lab column
  routinely surfaces a Phenotype-shaped hit. The lane is still called
  `Laboratory`, but low confidence flags that the mapping itself is
  suspect, not just optional.
- otherwise → no model
**Also recorded:** the value datatype (`xsd:float` vs `xsd:integer`, from
whether any sampled value has a decimal point) and any "sentinel" values
seen (`unable`, `not done`, `n/a`, `unknown`, `missing`) that will need
special handling downstream rather than being coerced to numbers.
**Result:** `NUMERIC`, confidence *medium* (Examination) or *low*
(Laboratory/unresolved) — always flagged.

### Step 8 — is this a small controlled vocabulary (general case)?
**Rule:** (only reached if nothing above matched) ≤12 distinct values *and*
`n_distinct / n_values < 0.5` (i.e., values repeat — it isn't just an
accident of a small sample).
**Search:** **one lookup per distinct value**, capped at 12 — e.g. a column
with values `{"Neonatal onset", "Childhood onset", "Adult onset"}` fires
three independent queries, each scored on its own; there is no fraction
guard here, because each accepted value becomes its own mapping row rather
than a verdict about the whole column.
**Result:** `DICTIONARY`, confidence *low* — explicitly "search only
suggests"; every proposed value→IRI pairing here goes to a human, none are
auto-accepted regardless of score.

### Step 9 — free text (the guarded SEARCH lane)
**Rule:** everything that survived steps 0–8 — i.e. genuinely unstructured
text.
**Search — the guarded, multi-sample form:**
1. Take up to 20 distinct values (`--sample`), in first-seen order.
2. Split each value on `;`, `,`, or literal `" and "` — so a cell like
   `"scoliosis; proximal weakness"` becomes two independently-scored parts.
3. Query every part (each ≥3 characters) against the search, keep the top
   hit for each.
4. **Score each part** against 0.50; **hit_fraction** = the share of parts
   that cleared it.
5. **Accept the column** only if `hit_fraction ≥ 0.60`. This is the
   guard from slide 5 — judging the column's aggregate behaviour, not
   trusting any single value's score, because a single free-text value can
   mislead (a comment scoring higher than a real symptom) in a way an
   aggregate share can't.
**Deciding the model:** among parts that cleared the score bar, take the
most common ontology prefix (`dom_prefix`); map `hp`→`Phenotype`,
`mondo`/`orpha`→`Diagnosis`; `uberon`/`ncit` map to no model (anatomy/misc
terms aren't a CARE-SM type on their own in this pipeline yet).
**Confidence:** *high* if `hit_fraction ≥ 0.80` and a dominant prefix
exists; *medium* if it merely cleared 0.60; if it didn't clear 0.60 at all,
the column is **rejected** — logged as `"REJECTED as distractor: only NN% of
values clear score 0.50 (median X) — scattered hits"`, model set to `None`,
confidence *low*. Rejection is the intended outcome here, not a failure of
the classifier — see the `comments` and `Gene` cases on slide 5.
**Result:** `SEARCH`, model per above, confidence as above.

---

## 3. Reading order and evidence

Every classification carries: the lane, the proposed CARE-SM model (or
`None`), a confidence (`high`/`medium`/`low`), a human-readable note
explaining *why*, and — for anything not `high` confidence — a `review`
flag. `high` confidence is reserved for the lanes with no semantic
ambiguity at all (DROP, PII, KEY, CURIE structure, Sex). Everything that
passed through a search — guarded or not — tops out at `medium`. Nothing
the search touches is ever asserted at `high` confidence; that's a
structural guarantee of the code, not a policy note.

The full per-column evidence (including the actual hit returned, its score,
and — for the SEARCH lane — every scored part) is available via
`--json report.json` on `profile_columns.py`, and is what the curation
workbook (`build_curation_workbook.py`) reads to build the human-review
spreadsheet.
