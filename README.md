# Spreadsheet to CARE-SM

> **Before you start: this is currently tuned for neuromuscular disease
> registries.** Most of the "which standard code does this diagnosis/symptom
> match?" work in this tool is done by sending a short query to a lookup
> service that has been loaded with a specific medical reference vocabulary
> — one built for neuromuscular disease, called
> [**NMDO**](https://github.com/NeuromuscularDisease/neuromuscular-disease-ontology).
> Out of the box,
> this tool will therefore be much better at recognising neuromuscular
> diagnoses and symptoms than, say, cardiology or oncology ones. (Gene, gene
> variant, and gene-zygosity lookups are the exception — those always use
> public, general-purpose biomedical reference services regardless of
> disease area, so they work the same for any registry.)
>
> If your registry covers a different area of medicine, this tool can still
> work for you — it just needs an equivalent lookup service loaded with a
> vocabulary suited to your area, pointed at via the `--search-url` option on
> each command (see [`docs/PIPELINE.md`](docs/PIPELINE.md) for how that
> service works). **[Open an issue](../../issues)** on this repo if you'd
> like help setting one up.

## What this is for

Most patient registries don't actually live in a spreadsheet — they live in a
database, structured however that particular registry's software was built:
patients, visits, repeated questionnaires, linked forms, and so on. That
structure is usually nothing like a flat spreadsheet, and **this tool has no
way to understand it directly** — it doesn't connect to databases, and it
knows nothing about your particular system's underlying data model. The core
mismatch is a real one: a database's entities and relationships and a
spreadsheet's flat rows-and-columns are simply not the same kind of thing, and
no tool can safely guess how to collapse one into the other on your behalf.

What this tool *does* understand is a flat spreadsheet export — one sheet,
one row per observation (for example, one row per patient, or one row per
patient per visit), one column per piece of information. Almost every
registry system, however it's built, can produce an export roughly this
shape — even though doing so means flattening or simplifying some of the
richer structure held in the real database. **That export step is on you (or
your registry's IT/data-management team)** — this tool picks up only once
that flat file exists, and trusts that whoever produced it made sensible
decisions about how to flatten it.

**[CARE-SM](https://github.com/wilkinsonlab/CARE-Semantic-Model-Version-2)**
(the Clinical And Registry Entries Semantic Model) is a shared,
standard way of describing patient information — diagnoses, symptoms, lab
results, genetic findings, and more — so that data collected by different
clinics, in different countries, using completely different systems, can
still be compared, combined, and understood correctly by everyone.

This tool takes that flat spreadsheet export and does the tedious,
error-prone translation work for you: it looks at each column, works out what
kind of clinical information it holds, and converts it into the standard
CARE-SM format — ready to be shared, combined, or analysed alongside data from
anywhere else that also uses CARE-SM.

## What it actually does

Give it a spreadsheet export, and it will:

1. **Work out what each column means.** Is this a patient identifier? A
   diagnosis? A yes/no clinical flag such as "has cardiomyopathy"? A gene? A
   date? It uses a mix of straightforward rules (a column literally called
   "Diagnosis" probably contains diagnoses) and biomedical lookups (a value
   like `SCN4A` is recognised as a gene symbol, not just a random piece of
   text) to work this out — the same kind of judgement a data manager would
   apply by eye, just applied consistently to every column, every time.
2. **Find the correct standard code for each value**, wherever one exists —
   for example, turning "Duchenne muscular dystrophy" into its proper entry
   in a recognised disease vocabulary, or a gene symbol into its official
   gene identifier, so that "Duchenne" typed one way in your spreadsheet
   means exactly the same thing as "DMD" typed another way in someone else's.
3. **Never guess when it isn't confident.** If a column's meaning is unclear,
   or a value can't be matched to a standard code with real confidence, it is
   set aside for a person to look at — never silently forced into an answer
   that might be wrong.
4. **Write out the finished CARE-SM files**, ready to be loaded into any
   CARE-SM-compatible system.

## What it will never do

- **It will never connect to your registry's database, or attempt to work out
  its structure on your behalf.** It only reads the flat spreadsheet export
  you give it — see "What this is for" above.
- **It will never silently guess.** Anything the tool isn't genuinely
  confident about is flagged for a human to check, not quietly turned into an
  answer that looks plausible but might be wrong.
- **It never modifies your original spreadsheet.** It only ever reads it, and
  writes its output to new files elsewhere.
- **It automatically finds and removes directly identifying information** —
  patient names, addresses, and similar columns — before anything is written
  out. That information is never copied into the CARE-SM output.

## Is my data safe?

This tool runs entirely on your own computer or server — none of your patient
data is uploaded anywhere by default. The one exception is short, individual
lookup queries — a diagnosis name, a gene symbol, a lab-test description — sent
to public biomedical reference services (the same kind of service a doctor
might use to look up a disease code) purely to find the correct standard code
for that one term. No patient names, dates of birth, or other identifying
information are ever included in those lookups.

## What comes out the other end isn't automatically "done"

This tool proposes a translation of your data — it does not make final
clinical or coding decisions on its own. The goal is to remove the tedious,
repetitive 95% of the work, not to remove the person who understands the
data.

### Where the "needs a human to check this" items end up

Anything the tool wasn't genuinely confident about is **not** included in the
finished CARE-SM files at all — it's left out of that output entirely, rather
than included with a "maybe" flag attached. Where to actually find it:

- **The curation workbook** — the `.xlsx` file you name with `-o` when you run
  `build_curation_workbook.py` (`review.xlsx` in the example above). This is
  the main "for a human to check" artifact: one row per uncertain column or
  value, with a locked drop-down for a reviewer's verdict.
- **The column-triage report** — printed to your screen by `profile_columns.py`
  by default, or saved as a file if you add `--json report.json`. Useful for
  understanding *why* the tool made a particular call, column by column.
- **The run's own summary counts** — every time `build_care_template.py` runs,
  it prints how many rows it emitted and how many it skipped, and why
  (e.g. "skipped: gene could not be resolved"). Worth reading after every run,
  not just once.

### There is currently no automated way to feed corrections back in

Once a human has reviewed the curation workbook, **there is no "apply my
corrections" step today.** The current, entirely manual workflow is:

1. A person reviews the flagged items and works out what should actually
   happen for each one.
2. They fix the *source spreadsheet* by hand (correcting a value, renaming a
   column, filling in something that was missing, etc.) — or, if the same
   kind of thing is likely to recur, someone technical updates the tool's own
   mapping files (`column_mappings.json`/`care_template_mappings.json`) so
   the fix applies automatically next time.
3. The whole transformation is **run again from scratch** on the corrected
   spreadsheet.

There's no partial re-run and no way to hand the reviewed workbook back to the
tool yet — every fix currently has to be made upstream of it. A "patching"
workflow that reads the reviewed workbook and reapplies corrections
automatically is planned, but doesn't exist yet.

**Why that's harder than it sounds:** not every entry in the review workbook
is something that can be "corrected" at all. Some of them mean "this value has
no matching concept in NMDO" — and the fix for *that* isn't a data correction
on your end, it's a request to add a new term to the ontology itself, which
only NMDO's own maintainers can do. There is, as of now, no defined process
for routing those specific findings to the NMDO team, and until a term
actually gets added to the ontology, no amount of re-running this tool will
resolve that particular item. Building that piece — deciding which review
items are "fixable here" versus "needs the ontology itself extended
elsewhere," and how the second kind gets routed and tracked — is future work.

---

## Trying it out

You'll need Python 3 (no other software to install — see
[`requirements.txt`](requirements.txt)).

```bash
git clone https://github.com/wilkinsonlab/Spreadsheet-to-CARE-SM.git
cd Spreadsheet-to-CARE-SM

# 1. See how the tool understands your columns, without writing anything yet
python3 src/profile_columns.py path/to/your_export.csv

# 2. Produce a review spreadsheet a human can check and correct
python3 src/build_curation_workbook.py path/to/your_export.csv -o review.xlsx

# 3. Emit the actual CARE-SM file for one clinical model, e.g. Diagnosis
python3 src/build_care_template.py path/to/your_export.csv -o out/ --model Diagnosis
```

Add `--offline` to any command to run without contacting any external lookup
service (useful for a first look, or for a machine with no internet access) —
it will simply flag more things for human review instead of resolving them
automatically.

## Wanting to know more

- **[`docs/PIPELINE.md`](docs/PIPELINE.md)** is the full technical write-up:
  the exact decision-making the tool goes through for every column, why it's
  built the way it is, and the evidence behind those decisions.
- **[`docs/talk/`](docs/talk/)** has a presentation deck and a step-by-step
  walkthrough of the same decision-making, if a slide-and-narrative format is
  more useful than a technical document.
- **[`CHANGELOG.md`](CHANGELOG.md)** lists what changed and when.
- **[CARE-SM](https://github.com/wilkinsonlab/CARE-Semantic-Model-Version-2)**
  is the data model this tool targets (v2, specifically), maintained
  separately.

## For developers

- `src/` — the three command-line tools (`profile_columns.py`,
  `build_curation_workbook.py`, `build_care_template.py`) plus the JSON
  mapping tables (`column_mappings.json`, `care_template_mappings.json`) they
  read their header-keyword and value-vocabulary rules from — edit those
  files, not the Python, to change what maps to what.
- `tests/` — run with `python3 -m pytest` from the repo root (needs
  `requirements-dev.txt`: `pip install -r requirements-dev.txt`).
- `docs/` — the technical write-up and presentation materials referenced
  above.
- Stdlib-only Python 3 for the tools themselves; nothing to install to run
  them (see `requirements.txt`).

## License

[MIT](LICENSE) — see the LICENSE file.
