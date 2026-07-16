#!/usr/bin/env python3
"""
build_care_template.py — emit a CARE-SM per-type template CSV from a raw dump.

The transform is not a column projection: it explodes multi-value cells into
rows, sources start/end dates from a different column than the value, and maps
every cell (not a sample) to an ontology IRI.

STRUCTURE IS DRIVEN BY THE AUTHORITATIVE M/O/U SPEC (from the CARE-SM CSV
glossary, https://care-sm.readthedocs.io/en/latest/glossary.html), NOT by the
example CSVs (which are known to be incorrect). For each model, every field is
Mandatory (M), Optional (O) or Unused (U):
  * a model's CSV header = its M and O fields, in canonical order; U fields are
    NEVER emitted;
  * `value` (O in most models) = the human-readable label of `valueIRI`;
  * `value_datatype` is per-model (Unused for Sex/Phenotype/Diagnosis, Mandatory
    for Symptoms_onset/Medication/…);
  * `event_id` (O) groups elements from one clinical visit for the quad context
    URI. We have no visit-grouping information, so the column is present but left
    BLANK — never fabricated.

Negation: the Phenotype template has no negation slot, so negatives ("denies
dysphagia" / flag = No) are skipped and counted pending a CARE-SM model decision
(lab meeting scheduled).

Usage:
  python3 build_care_template.py mockdata/dump.mock -o out_dir/ --model Phenotype
  python3 build_care_template.py mockdata/dump.mock -o out_dir/ --model Sex --offline
"""

import argparse
import csv
import os
import re
from collections import Counter
from types import SimpleNamespace

import profile_columns as pc

# ---- authoritative field spec ------------------------------------------------
# Canonical field order for the per-type CSV template.
CANONICAL_ORDER = [
    "model", "pid", "value", "value_datatype", "valueIRI", "activity", "unit",
    "input", "target", "specification", "frequency_type", "frequency_value",
    "agent", "startdate", "enddate", "age", "comments", "event_id",
    "organisation", "duration_value", "duration_startdate", "duration_enddate",
]

# Per model: which fields are Mandatory and Optional (everything else Unused).
# Source: CARE-SM CSV glossary (M/O/N markers), verified against the rendered page.
MODEL_SPEC = {
    "Birthdate":      {"M": ["model", "pid", "value"], "O": ["specification", "startdate", "enddate", "comments", "event_id"]},
    "Birthyear":      {"M": ["model", "pid", "value"], "O": ["specification", "comments", "event_id"]},
    "Birthplace":     {"M": ["model", "pid", "valueIRI"], "O": ["value", "specification", "startdate", "enddate", "comments", "event_id"]},
    "Deathdate":      {"M": ["model", "pid", "value"], "O": ["valueIRI", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "First_visit":    {"M": ["model", "pid"], "O": ["value", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Sex":            {"M": ["model", "pid", "valueIRI"], "O": ["value", "specification", "startdate", "enddate", "comments", "event_id"]},
    "Status":         {"M": ["model", "pid", "valueIRI"], "O": ["value", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Symptoms_onset": {"M": ["model", "pid", "value", "value_datatype"], "O": ["target", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Phenotype":      {"M": ["model", "pid", "valueIRI"], "O": ["value", "target", "specification", "startdate", "enddate", "age", "comments", "event_id", "duration_value", "duration_startdate", "duration_enddate"]},
    "Diagnosis":      {"M": ["model", "pid", "valueIRI"], "O": ["value", "target", "startdate", "enddate", "age", "comments", "event_id"]},
    "Examination":    {"M": ["model", "pid", "value", "valueIRI"], "O": ["value_datatype", "activity", "unit", "target", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Laboratory":     {"M": ["model", "pid", "value", "target"], "O": ["value_datatype", "activity", "unit", "input", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Medication":     {"M": ["model", "pid", "value", "value_datatype", "valueIRI", "unit", "agent"], "O": ["activity", "specification", "frequency_type", "frequency_value", "startdate", "enddate", "age", "comments", "event_id"]},
    "Genetic":        {"M": ["model", "pid", "value", "valueIRI"], "O": ["activity", "input", "specification", "agent", "startdate", "enddate", "age", "comments", "event_id"]},
    "Surgery":        {"M": ["model", "pid", "activity"], "O": ["target", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Biobank":        {"M": ["model", "pid", "value", "organisation"], "O": ["input", "specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Hospitalization": {"M": ["model", "pid", "activity"], "O": ["specification", "startdate", "enddate", "age", "comments", "event_id"]},
    "Disability":     {"M": ["model", "pid", "value", "value_datatype", "specification"], "O": ["unit", "enddate", "age", "comments", "event_id", "duration_value", "duration_startdate", "duration_enddate"]},
}


def model_header(model):
    """CSV header for a model: its M and O fields in canonical order (U excluded)."""
    spec = MODEL_SPEC[model]
    allowed = set(spec["M"]) | set(spec["O"])
    return [f for f in CANONICAL_ORDER if f in allowed]


def write_model_csv(model, rowdicts, path):
    """Write rows (list of field->value dicts) using the model's M/O/U header.
    Unset fields (incl. the deliberately-blank event_id) are empty. Returns a
    Counter of any Mandatory fields left blank (should be zero)."""
    header = model_header(model)
    mandatory = MODEL_SPEC[model]["M"]
    violations = Counter()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for rd in rowdicts:
            for m in mandatory:
                if not rd.get(m):
                    violations[m] += 1
            w.writerow([rd.get(col, "") for col in header])
    return header, violations


# ---- shared helpers ----------------------------------------------------------
NEG_RE = re.compile(r"^(no|not|without|denies|denied|absent|negative for|no evidence of)\b", re.I)
AFFIRMATIVE = {"yes", "true", "present", "positive", "y"}
NEGATIVE = {"no", "false", "absent", "negative", "n"}


def split_parts(cell):
    return [p.strip() for p in re.split(r"\s*[;,]\s*|\s+and\s+", cell) if p.strip()]


def pick_pid(results):
    return next((r["column"] for r in results if r["lane"] == "KEY"), None)


def generic_date_col(results):
    dates = [r for r in results if r["lane"] == "DATE"]
    for r in dates:
        if r.get("care_sm_model") is None:
            return r["column"]
    return dates[0]["column"] if dates else None


def date_col_for_model(model, results, prefer_startswith=None):
    """The date column feeding a given model. Prefers a DATE column whose guessed
    model matches and (optionally) whose header starts with a keyword — so
    'Diagnosis date' wins over 'Cardiomyopathy diagnosis date'."""
    cands = [r for r in results if r["lane"] == "DATE" and r.get("care_sm_model") == model]
    if prefer_startswith:
        for r in cands:
            if r["column"].lower().startswith(prefer_startswith):
                return r["column"]
    if cands:
        return cands[0]["column"]
    return generic_date_col(results)


def resolve_flag_date(flag_header, row, colidx, results, gdate):
    pref = flag_header.lower()
    cand = [r["column"] for r in results if r["lane"] == "DATE" and r["column"].lower().startswith(pref)]
    cand.sort(key=lambda d: (0 if "diagnos" in d.lower() else 1 if "datestamp" in d.lower() else 2))
    for d in cand:
        v = row[colidx[d]].strip()
        if v:
            return v
    return row[colidx[gdate]].strip() if gdate else ""


# ============================================================ Phenotype ======
def build_phenotype(headers, body, results, searcher, args):
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    gdate = generic_date_col(results)
    freetext = [r for r in results if r["lane"] == "SEARCH" and r.get("care_sm_model") == "Phenotype"]
    flags = [r for r in results if r["lane"] == "BOOLEAN" and r.get("care_sm_model") == "Phenotype"]
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_date"] = gdate
    stats["_freetext"] = ", ".join(r["column"] for r in freetext) or "(none)"
    stats["_flags"] = ", ".join(r["column"] for r in flags) or "(none)"

    def hpo(term):
        hit = searcher.top(term)
        if hit and "_error" not in hit and hit.get("prefix") == "hp" and hit.get("score", 0) >= args.score:
            return hit["iri"], hit.get("label", "")
        return None

    rows = []
    for row in body:
        pid = row[colidx[pid_col]].strip() if pid_col else ""
        if not pid:
            continue
        for r in freetext:
            cell = row[colidx[r["column"]]].strip()
            if not cell:
                continue
            date = row[colidx[gdate]].strip() if gdate else ""
            for part in split_parts(cell):
                if NEG_RE.match(part):
                    stats["negated_skipped"] += 1
                    continue
                m = hpo(part)
                if not m:
                    stats["unmapped_skipped"] += 1
                    continue
                iri, label = m
                rows.append({"model": "Phenotype", "pid": pid, "valueIRI": iri,
                             "value": label, "startdate": date, "enddate": date})
                stats["emitted_freetext"] += 1
        for r in flags:
            cell = row[colidx[r["column"]]].strip().lower()
            if cell in AFFIRMATIVE:
                maps = r.get("proposed_mappings") or []
                if not maps:
                    stats["flag_no_map"] += 1
                    continue
                iri, label = maps[0].get("iri"), maps[0].get("label", "")
                date = resolve_flag_date(r["column"], row, colidx, results, gdate)
                rows.append({"model": "Phenotype", "pid": pid, "valueIRI": iri,
                             "value": label, "startdate": date, "enddate": date})
                stats["emitted_flags"] += 1
            elif cell in NEGATIVE:
                stats["flag_negative_skipped"] += 1
    return rows, stats


# ============================================================ Diagnosis ======
CURIE_EXPAND = {
    "ORPHA": "http://www.orpha.net/ORDO/Orphanet_{}",
    "ORPHANET": "http://www.orpha.net/ORDO/Orphanet_{}",
    "MONDO": "http://purl.obolibrary.org/obo/MONDO_{}",
    "OMIM": "https://www.omim.org/entry/{}",
    "ICD10": "http://purl.bioontology.org/ontology/ICD10/{}",
}


def expand_curie(value):
    if ":" not in value:
        return None
    prefix, local = value.split(":", 1)
    tmpl = CURIE_EXPAND.get(prefix.strip().upper())
    return tmpl.format(local.strip()) if tmpl else None


def build_diagnosis(headers, body, results, searcher, args):
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    ddate = date_col_for_model("Diagnosis", results, prefer_startswith="diagnos")
    curie_cols = [r for r in results if r["lane"] == "CURIE" and r.get("care_sm_model") == "Diagnosis"]
    freetext_cols = [r for r in results if r["lane"] == "SEARCH" and r.get("care_sm_model") == "Diagnosis"]
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_date"] = ddate
    stats["_curie"] = ", ".join(r["column"] for r in curie_cols) or "(none)"
    stats["_freetext"] = ", ".join(r["column"] for r in freetext_cols) or "(none)"

    def mondo(term):
        hit = searcher.top(term)
        if hit and "_error" not in hit and hit.get("prefix") == "mondo" and hit.get("score", 0) >= args.score:
            return hit["iri"], hit.get("label", "")
        return None

    rows = []
    for row in body:
        pid = row[colidx[pid_col]].strip() if pid_col else ""
        if not pid:
            continue
        date = row[colidx[ddate]].strip() if ddate else ""
        for r in curie_cols:
            v = row[colidx[r["column"]]].strip()
            if not v:
                continue
            iri = expand_curie(v)
            if not iri:
                stats["curie_unexpanded"] += 1
                continue
            # value (label) left blank for pre-coded CURIEs — no label fetched.
            rows.append({"model": "Diagnosis", "pid": pid, "valueIRI": iri,
                         "startdate": date, "enddate": date})
            stats["emitted_curie"] += 1
        for r in freetext_cols:
            v = row[colidx[r["column"]]].strip()
            if not v:
                continue
            for part in split_parts(v):
                m = mondo(part)
                if not m:
                    stats["unmapped_skipped"] += 1
                    continue
                iri, label = m
                rows.append({"model": "Diagnosis", "pid": pid, "valueIRI": iri,
                             "value": label, "startdate": date, "enddate": date})
                stats["emitted_freetext"] += 1
    return rows, stats


# ============================================================ Sex =============
# Curated lookup (NMDO search returns nonsense for "Male"). NCIT codes per CARE-SM.
SEX_MAP = {
    "male": ("http://purl.obolibrary.org/obo/NCIT_C20197", "male"),
    "m": ("http://purl.obolibrary.org/obo/NCIT_C20197", "male"),
    "female": ("http://purl.obolibrary.org/obo/NCIT_C16576", "female"),
    "f": ("http://purl.obolibrary.org/obo/NCIT_C16576", "female"),
}


def build_sex(headers, body, results, searcher, args):
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    bdate = date_col_for_model("Birthdate", results)
    sex_cols = [r for r in results if r.get("care_sm_model") == "Sex"]
    sex_col = next((r["column"] for r in sex_cols if "birth" in r["column"].lower()),
                   sex_cols[0]["column"] if sex_cols else None)
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_date"] = bdate
    stats["_sex"] = sex_col or "(none)"

    rows, seen = [], set()
    if sex_col:
        for row in body:
            pid = row[colidx[pid_col]].strip() if pid_col else ""
            if not pid or pid in seen:
                continue
            mapped = SEX_MAP.get(row[colidx[sex_col]].strip().lower())
            if not mapped:
                stats["unmapped_skipped"] += 1
                continue
            iri, label = mapped
            date = row[colidx[bdate]].strip() if bdate else ""
            rows.append({"model": "Sex", "pid": pid, "valueIRI": iri, "value": label,
                         "startdate": date, "enddate": date})
            seen.add(pid)
            stats["emitted"] += 1
    return rows, stats


# ============================================================ date models ====
def date_cols_for_model(model, results):
    """Return (event_date_col, datestamp_col) for a model: the event date is a
    DATE column of that model whose header lacks 'datestamp'; the datestamp (the
    record/registration date) is one whose header contains 'datestamp'."""
    cols = [r["column"] for r in results if r["lane"] == "DATE" and r.get("care_sm_model") == model]
    event = next((c for c in cols if "datestamp" not in c.lower()), None)
    stamp = next((c for c in cols if "datestamp" in c.lower()), None)
    return event, stamp


def _simple_date_model(model, headers, body, results):
    """Birthdate / Deathdate: value = the date; startdate=enddate = the date
    (per the CARE-SM RDF examples). Skips patients with no date."""
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    event, _ = date_cols_for_model(model, results)
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_date"] = event or "(none)"
    rows = []
    for row in body:
        pid = row[colidx[pid_col]].strip() if pid_col else ""
        if not pid:
            continue
        d = row[colidx[event]].strip() if event else ""
        if not d:
            stats["blank_skipped"] += 1
            continue
        rows.append({"model": model, "pid": pid, "value": d, "startdate": d, "enddate": d})
        stats["emitted"] += 1
    return rows, stats


def build_birthdate(headers, body, results, searcher, args):
    return _simple_date_model("Birthdate", headers, body, results)


def build_deathdate(headers, body, results, searcher, args):
    return _simple_date_model("Deathdate", headers, body, results)


def build_symptoms_onset(headers, body, results, searcher, args):
    """value = onset date (value_datatype xsd:date, Mandatory here); startdate/
    enddate = the record datestamp if present (else the onset date). `target`
    (the specific symptom HPO) is left blank — the dumps carry disease-level
    onset, not a per-symptom onset."""
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    event, stamp = date_cols_for_model("Symptoms_onset", results)
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_onset"] = event or "(none)"
    stats["_datestamp"] = stamp or "(none)"
    rows = []
    for row in body:
        pid = row[colidx[pid_col]].strip() if pid_col else ""
        if not pid:
            continue
        onset = row[colidx[event]].strip() if event else ""
        if not onset:
            stats["blank_skipped"] += 1
            continue
        rec = row[colidx[stamp]].strip() if stamp else ""
        rows.append({"model": "Symptoms_onset", "pid": pid, "value": onset,
                     "value_datatype": "xsd:date", "startdate": rec or onset,
                     "enddate": rec or onset})
        stats["emitted"] += 1
    return rows, stats


# ============================================================ Status =========
SIO_ALIVE = "http://semanticscience.org/resource/SIO_010058"  # "alive"
SIO_DEAD = "http://semanticscience.org/resource/SIO_010059"    # "dead"
STATUS_MAP = {
    "yes": (SIO_ALIVE, "alive"), "alive": (SIO_ALIVE, "alive"), "living": (SIO_ALIVE, "alive"),
    "no": (SIO_DEAD, "dead"), "dead": (SIO_DEAD, "dead"), "deceased": (SIO_DEAD, "dead"),
}
STATUS_HEADER_RE = re.compile(r"\b(alive|vital\s*status|life\s*status)\b", re.I)


def build_status(headers, body, results, searcher, args):
    """Participation/vital status from an 'Alive' (Yes/No) column -> SIO
    alive/dead. Dated by the matching '<col> datestamp'."""
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)
    status_col = next((r["column"] for r in results
                       if r["lane"] in ("BOOLEAN", "DICTIONARY") and STATUS_HEADER_RE.search(r["column"])), None)
    sdate = None
    if status_col:
        pref = status_col.lower()
        sdate = next((r["column"] for r in results
                      if r["lane"] == "DATE" and r["column"].lower().startswith(pref)), None)
    stats = Counter()
    stats["_pid"] = pid_col
    stats["_status"] = status_col or "(none)"
    stats["_date"] = sdate or "(none)"
    rows = []
    if status_col:
        for row in body:
            pid = row[colidx[pid_col]].strip() if pid_col else ""
            if not pid:
                continue
            mapped = STATUS_MAP.get(row[colidx[status_col]].strip().lower())
            if not mapped:
                stats["unmapped_skipped"] += 1
                continue
            iri, label = mapped
            d = row[colidx[sdate]].strip() if sdate else ""
            rows.append({"model": "Status", "pid": pid, "valueIRI": iri, "value": label,
                         "startdate": d, "enddate": d})
            stats["emitted"] += 1
    return rows, stats


BUILDERS = {
    "Phenotype": build_phenotype, "Diagnosis": build_diagnosis, "Sex": build_sex,
    "Birthdate": build_birthdate, "Deathdate": build_deathdate,
    "Symptoms_onset": build_symptoms_onset, "Status": build_status,
}


def main():
    ap = argparse.ArgumentParser(description="Emit a CARE-SM per-type template from a raw dump.")
    ap.add_argument("file")
    ap.add_argument("-o", "--out", required=True, help="output directory")
    ap.add_argument("--model", default="Phenotype", choices=sorted(BUILDERS))
    ap.add_argument("--search-url", default=pc.DEFAULT_SEARCH_URL)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--score", type=float, default=pc.SCORE_THRESHOLD)
    ap.add_argument("--hit-fraction", dest="hit_fraction", type=float, default=pc.HIT_FRACTION)
    ap.add_argument("--small-vocab", dest="small_vocab", type=int, default=pc.SMALL_VOCAB_MAX)
    ap.add_argument("--sample", dest="sample", type=int, default=pc.SAMPLE_VALUES)
    a = ap.parse_args()

    args = SimpleNamespace(score=a.score, hit_fraction=a.hit_fraction,
                           small_vocab=a.small_vocab, sample=a.sample,
                           offline=a.offline, search_url=a.search_url)
    searcher = pc.StubSearcher() if a.offline else pc.HttpSearcher(a.search_url)
    headers, body, _ = pc.load_table(a.file)
    if not headers:
        raise SystemExit(f"No data found in {a.file}")
    results = [pc.classify(h, pc.col_values(body, i), searcher, args, idx=i)
               for i, h in enumerate(headers)]

    rows, stats = BUILDERS[a.model](headers, body, results, searcher, args)
    os.makedirs(a.out, exist_ok=True)
    outpath = os.path.join(a.out, f"{a.model}.csv")
    header, violations = write_model_csv(a.model, rows, outpath)

    print(f"Wrote {len(rows)} {a.model} rows -> {outpath}")
    print(f"  header ({len(header)} cols): {','.join(header)}")
    for k, v in stats.items():
        if k.startswith("_"):
            print(f"  {k[1:]+' source':16} {v}")
    for k, v in stats.items():
        if not k.startswith("_"):
            print(f"  {k:24} {v}")
    if violations:
        print(f"  !! Mandatory-field blanks: {dict(violations)}")


if __name__ == "__main__":
    main()
