# Cookbook: Building Synthetic Test Data for Spreadsheet-to-Ontology Pipelines

*A reusable method for anyone building a tool that ingests a registry/clinical
spreadsheet and heuristically maps its columns and cells onto ontology terms and
a target data model (here: CARE-SM). The example file this cookbook documents is
`mockdata/synthetic_stresstest.mock`.*

---

## 1. Why synthetic data, when you have real data?

Real clinical dumps are the *acceptance* test. They are a poor *development*
test, for one counter-intuitive reason:

> **Real data is usually too clean, too consistent, and too polite to break your code.**

A well-run registry exports a single controlled vocabulary per column, no
free text, no surprises. Your heuristics will pass on it and then fall over the
first time a different site sends a messier export. The job of a synthetic file
is the opposite of realism: it is **adversarial coverage**. You deliberately
pack, into a small file you can read at a glance, one clean example of every
failure mode you can imagine — so that every branch of your mapping logic is
exercised on day one.

The ideal test set is therefore **two files, used together**:

1. A **real (or realistic) clean file** — proves you handle the common case and
   the site's actual conventions. *(Ours: `FakeData_sNMD1(Singular items).mock`.)*
2. A **synthetic adversarial file** — proves you handle everything else.
   *(Ours: `synthetic_stresstest.mock`.)*

If your profiler/transformer behaves sensibly on both, you have real confidence.

---

## 2. The hazard catalogue

Each hazard below is a distinct thing that can go wrong when you try to
auto-map a spreadsheet. Bake **at least one clean instance of each** into the
synthetic file. The right-hand notes say what the pipeline must *do* about it.

### A. Column-role ambiguity — *which* mapping applies?
A mappable column is not one thing. There are (at least) three roles, and the
correct transform differs for each:

| Role | Header maps? | Cell maps? | Example | What the value means |
|------|:---:|:---:|---|---|
| **Header-as-measurement** | ✅ | ✗ (numeric) | `10MWT` = `8.4` | header→term, cell→numeric value |
| **Cell-as-concept** | ✗ | ✅ (text) | `pheno` = `"scoliosis"` | cell→term |
| **Header-as-concept, cell-as-presence** | ✅ | boolean | `cardiomyopathy` = `Yes` | header→term, cell = include/exclude |

> **Test:** include all three. A profiler that only knows the first two will
> silently mishandle boolean flag columns (very common in registries).

### B. Pre-coded columns that bypass search entirely
Some columns already contain ontology identifiers (CURIEs). No semantic search
is needed — only CURIE→IRI expansion — and blindly running search here is
wasted effort *and* a source of error.

> **Test:** include a column of pre-coded values (e.g. `ORPHA:70`). The pipeline
> must detect "this is already an identifier" and route it to an expansion path,
> not the search path.

### C. Free-text hazards (the semantic-search lane)
This is where embedding search earns its keep — and where it is most fragile:

- **Multi-value cells:** `"proximal weakness; scoliosis"` → *multiple* output
  rows from one cell. Vary the **delimiter** (`;`, `,`, and the word "and") so
  you don't hard-code one splitter.
- **Negation / polarity:** `"denies dysphagia"`, `"no ptosis"`. An embedding
  model happily returns the *dysphagia* term for "denies dysphagia" — the hit is
  correct, the **meaning is inverted**. The pipeline needs a negation check, or
  it will assert the opposite of the truth.

### D. Numeric-measurement hazards (units are a minefield)
- **Unit in the header:** `CK (U/L)` — the unit must be parsed out of the header
  string, not the cell.
- **Unit entirely absent:** a bare `sodium` column — the unit exists only in the
  clinician's head. Your model still needs one; you can't invent it from the data.
- **Unit inline in a cell:** one stray `"1200 U/L"` in an otherwise-numeric
  column — breaks naive `to_float`.

> These three coexist in real data. Include all three so unit handling can't
> quietly assume one layout.

### E. Composite instruments
Some "columns" are actually several variables fused. `EQ-5D-5L` is five ordinal
dimensions (each 1–5) *plus* a 0–100 VAS. One header ≠ one value.

> **Test:** include a composite column (e.g. a 5-digit `EQ-5D-5L` profile). The
> pipeline must either decompose it or flag it as un-mappable-as-is, never treat
> it as a single scalar.

### F. Type-inference breakers
A numeric column that is *mostly* numeric but contains sentinel text —
`10MWT` = `"unable"`, or blanks. Naive datatype detection (`xsd:float`) must
survive the outliers instead of crashing or coercing them to 0.

### G. Date ambiguity and format drift
- **Many models key on a bare date** — birth, death, onset, first-visit all look
  identical to a value-mapper. Only the **header** disambiguates them, and
  semantic search is useless on dates. Include several date columns with
  different meanings (`dob`, `date_of_death`, `onset`).
- **Format drift:** slip one `DD/MM/YYYY` into an otherwise-ISO column to force
  robust date parsing.
- **Two dates per event:** real registries often carry a *record/entry* date AND
  a *clinical event* date. Include both so the pipeline has to choose the right
  one for `startdate`/`enddate` (hint: the always-populated one is usually the
  wrong, data-entry one).

### H. Semantic distractors ⭐ (the highest-value trick)
Add columns that are **not** clinical concepts but that an embedding search will
nonetheless score highly:

- a `comments` free-text column containing an ontology-loaded phrase like
  *"reports difficulty walking long distances"* — "difficulty walking" **will**
  return a strong HPO hit;
- an administrative column like `hospital`.

Because embedding search *always* returns a ranked top-k with a non-trivial
score, a naive "did it get a hit? then map it" rule will confidently map your
free-text notes and your hospital names. Distractors are what force you to build
the two guards you actually need:

1. a **score threshold**, and
2. a **hit-fraction threshold** (what proportion of the column's sampled values
   clear the bar) — a real phenotype column hits on most rows; a comments column
   hits on a scattered few.

> This is the single most important thing a clean real file will *not* test for
> you, and the most common way these pipelines embarrass themselves in production.

### I. Privacy / must-drop columns
Include direct identifiers (`first_name`, `last_name`). The pipeline must
**recognise and drop** them — never map them, never emit them, never let them
reach a shared artifact. (See the `.mock` rule in §5.)

### J. Redundant / duplicate columns
Real exports carry near-duplicates (`Full date of birth` == `Date of birth`;
`Sex at birth` == `Sex`). The pipeline should de-duplicate rather than emit the
same fact twice.

### K. Structural mess
- non-comma **delimiters** (`;` is common from European/Excel exports);
- **quoting** around fields that contain the delimiter;
- **export artifacts** — e.g. a trailing empty `""""""` column Excel loves to add.

### L. Required-but-absent target fields
The target model needs things the source simply does not contain:
- a per-event grouping id (CARE-SM `event_id`) — must be **synthesised**
  (e.g. hash of `patientid` + `date`);
- specimen type, assay method, administration route — **clinical background
  knowledge**, not data. The pipeline must leave them blank, look them up
  externally, or ask — but it cannot mine them from the dump.

> Design the synthetic file with **no** `event_id` column, so the synthesis path
> is always exercised.

---

## 3. Design principles for the file itself

1. **Small enough to read by eye.** ~30 rows. You want to be able to hand-verify
   the expected output for every row. This is a *test fixture*, not a benchmark.
2. **One clean instance of each hazard**, not a random mush. If a test fails you
   want to know exactly which hazard broke it. Keep hazards mostly separated by
   column.
3. **A few deliberate crossings.** Real data mixes hazards (a multi-value cell
   *and* a negation in the same field). Include one or two, on purpose, once the
   isolated cases pass.
4. **Plausible clinical content.** Use real disease/phenotype vocabulary so that
   the search step is genuinely exercised (a fake term hits nothing and tests
   nothing). Values are fake; *vocabulary* is real.
5. **Document the intent** — a mapping from each column to the hazard(s) it
   encodes (next section), so the file is self-explaining to a new contributor.

---

## 4. Hazard → column map for `synthetic_stresstest.mock`

| Column | Hazard(s) exercised |
|---|---|
| `patientid` | key; must not be mapped (§I-adjacent) |
| `visit_date` | the clinical event date (§G two-dates) |
| `dob` | date-role ambiguity + one `DD/MM/YYYY` format-drift row (§G) |
| `date_of_death` | date-role ambiguity; mostly-blank column (§G, §F) |
| `onset` | date-role ambiguity — a third bare-date meaning (§G) |
| `sex` | small controlled vocab; `Male/Female/M/F/f` casing & abbrev drift |
| `pheno` | free-text: multi-value, mixed delimiters, **negation** (§C) |
| `diagnosis` | free-text disease → MONDO; vocab drift (`DMD`/`Duchenne muscular dystrophy`) |
| `10MWT` | header-as-measurement (§A); `"unable"` type-breaker (§F) |
| `MRC_grade` | header-as-measurement; ordinal scale |
| `CK (U/L)` | numeric lab, **unit-in-header**; one inline `1200 U/L` cell (§D) |
| `sodium` | numeric lab, **unit absent** (§D) |
| `EQ-5D-5L` | **composite instrument** (§E) |
| `cardiomyopathy` | **header-as-concept, cell-as-presence** boolean flag (§A) |
| `comments` | **semantic distractor** — contains "difficulty walking" (§H) |
| `hospital` | **administrative distractor** (§H) |

*(No `event_id` column — forces the synthesis path, §L. Pre-coded-CURIE and
PII hazards, §B/§I, are demonstrated by the companion real file
`FakeData_sNMD1`, which carries `ORPHA:` diagnosis codes and name columns; a
future revision of the synthetic file can fold those in too.)*

---

## 5. Privacy rule (non-negotiable)

**All sample data — real or synthetic — uses the `.mock` extension and is
git-ignored. It is NEVER committed.** Real registry exports obviously must not
leave the machine; but we hold synthetic data to the same rule so there is one
simple, unbreakable convention (`*.mock` in `.gitignore`) and no judgement call
at commit time. This cookbook (a `.md`) *is* shareable; the data it describes is
not.

---

## 6. Using the two files together

Run your column-profiler over **both** files and compare against a hand-written
expectation:

- On the **clean file**, the profiler should route most columns to the
  *dictionary / CURIE-expansion* lane (controlled vocabularies, pre-coded IDs)
  and correctly drop PII/duplicates.
- On the **synthetic file**, it should route free-text columns to the
  *semantic-search* lane, apply score + hit-fraction thresholds to **reject**
  the distractors, detect the three column-roles, and flag composites/unit-less
  labs for human review rather than guessing.

A profiler that is green on both has earned the right to attempt the full
transform.
