#!/usr/bin/env python3
"""
build_care_template.py — emit a CARE-SM (v2) per-type template CSV from a dump.

Targets CARE-SM v2 (repo CARE-Semantic-Model-Version-2, w3id CARE-SM-2). The
column contract is the one the v2 Toolkit ENFORCES: it rejects any column
outside its allowed set, so each model emits exactly the columns its v2 example
CSV uses. Notably v2 differs from v1:

  * the ontology code being tested lives in `target` (Phenotype, Diagnosis,
    Symptoms_onset) or in `attribute_type` (Sex, Status) — NOT `valueIRI`;
  * `value` is a typed literal routed by `value_datatype`
    (xsd:boolean / xsd:date / …);
  * there is no `specification` / `valueIRI` column.

NEGATION (new in v2, for Phenotype and Diagnosis): a record supplies `target`
(what was tested) plus `value` = true/false. A false result is a first-class,
queryable row — the Toolkit builds the Attribute node only when value == true.
So negatives ("denies dysphagia", flag = No) are now EMITTED as value=false
rather than dropped.

`event_id` stays blank: it groups same-visit observations for the quad context
URI, and the source dumps don't carry visit grouping.

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

# ---- v2 Toolkit column contract ---------------------------------------------
# The full set the v2 Toolkit accepts (toolkit/main.py `self.columns`). Any
# column we emit MUST be in here or the Toolkit raises "Unexpected columns".
TOOLKIT_COLUMNS = {
    "model", "pid", "event_id", "value", "age", "value_datatype", "activity",
    "unit", "input", "target", "protocol_id", "frequency_type",
    "frequency_value", "startdate", "enddate", "comments", "organisation",
    "duration_value", "duration_startdate", "duration_enddate",
    "identifier_value", "input_value", "attribute_type", "output_type",
    "output_id", "cause_id",
}

# Per-model column list = exactly the v2 example CSV header for that model.
MODEL_COLUMNS = {
    "Phenotype": ["model", "pid", "startdate", "enddate", "event_id", "target",
                  "value", "value_datatype", "duration_value",
                  "duration_startdate", "duration_enddate"],
    "Diagnosis": ["model", "pid", "startdate", "enddate", "event_id", "target",
                  "value", "value_datatype"],
    "Sex": ["model", "pid", "startdate", "enddate", "event_id", "attribute_type"],
    "Status": ["model", "pid", "value_datatype", "startdate", "enddate", "event_id", "attribute_type"],
    "Birthdate": ["model", "pid", "value_datatype", "startdate", "enddate",
                  "event_id", "value"],
    "Deathdate": ["model", "pid", "value_datatype", "startdate", "enddate",
                  "event_id", "value", "cause_id"],
    "Symptoms_onset": ["model", "pid", "value_datatype", "startdate", "enddate",
                       "event_id", "value", "target"],
}
# fail fast if a model list ever drifts outside the enforced set
for _m, _cols in MODEL_COLUMNS.items():
    _bad = set(_cols) - TOOLKIT_COLUMNS
    assert not _bad, f"{_m} uses non-Toolkit columns {_bad}"


def write_model_csv(model, rowdicts, path):
    header = MODEL_COLUMNS[model]
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for rd in rowdicts:
            w.writerow([rd.get(col, "") for col in header])
    return header


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
    cands = [r for r in results if r["lane"] == "DATE" and r.get("care_sm_model") == model]
    if prefer_startswith:
        for r in cands:
            if r["column"].lower().startswith(prefer_startswith):
                return r["column"]
    if cands:
        return cands[0]["column"]
    return generic_date_col(results)


def date_cols_for_model(model, results):
    """(event_date_col, datestamp_col): event date lacks 'datestamp' in its
    header; the record datestamp contains it."""
    cols = [r["column"] for r in results if r["lane"] == "DATE" and r.get("care_sm_model") == model]
    event = next((c for c in cols if "datestamp" not in c.lower()), None)
    stamp = next((c for c in cols if "datestamp" in c.lower()), None)
    return event, stamp


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
    """v2: target = HPO code, value = true/false (xsd:boolean). Negatives —
    free-text 'denies X' and boolean flag = No — are emitted as value=false."""
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
            return hit["iri"]
        return None

    def emit(pid, iri, value, date):
        return {"model": "Phenotype", "pid": pid, "target": iri, "value": value,
                "value_datatype": "xsd:boolean", "startdate": date, "enddate": date}

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
                negated = bool(NEG_RE.match(part))
                term = NEG_RE.sub("", part, count=1).strip() if negated else part
                iri = hpo(term)
                if not iri:
                    stats["unmapped_skipped"] += 1
                    continue
                rows.append(emit(pid, iri, "false" if negated else "true", date))
                stats["emitted_negative" if negated else "emitted_positive"] += 1
        for r in flags:
            cell = row[colidx[r["column"]]].strip().lower()
            polarity = "true" if cell in AFFIRMATIVE else "false" if cell in NEGATIVE else None
            if polarity is None:
                continue
            maps = r.get("proposed_mappings") or []
            if not maps:
                stats["flag_no_map"] += 1
                continue
            date = resolve_flag_date(r["column"], row, colidx, results, gdate)
            rows.append(emit(pid, maps[0].get("iri"), polarity, date))
            stats["emitted_flag_positive" if polarity == "true" else "emitted_flag_negative"] += 1
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
    """v2: target = disease code, value = true/false (xsd:boolean). CURIE cols
    expand deterministically; free-text cols resolve to MONDO via search. Our
    data carries only affirmed diagnoses, so value is 'true' here — but the
    false path is structurally identical if a source ever records a ruled-out
    diagnosis."""
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
            return hit["iri"]
        return None

    def emit(pid, iri, date):
        return {"model": "Diagnosis", "pid": pid, "target": iri, "value": "true",
                "value_datatype": "xsd:boolean", "startdate": date, "enddate": date}

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
            rows.append(emit(pid, iri, date))
            stats["emitted_curie"] += 1
        for r in freetext_cols:
            v = row[colidx[r["column"]]].strip()
            if not v:
                continue
            for part in split_parts(v):
                iri = mondo(part)
                if not iri:
                    stats["unmapped_skipped"] += 1
                    continue
                rows.append(emit(pid, iri, date))
                stats["emitted_freetext"] += 1
    return rows, stats


# ============================================================ Sex / Status ===
# v2: the categorical concept goes in `attribute_type` (was v1 valueIRI).
SEX_MAP = {
    "male": "http://purl.obolibrary.org/obo/NCIT_C20197",
    "m": "http://purl.obolibrary.org/obo/NCIT_C20197",
    "female": "http://purl.obolibrary.org/obo/NCIT_C16576",
    "f": "http://purl.obolibrary.org/obo/NCIT_C16576",
}
SIO_ALIVE = "http://semanticscience.org/resource/SIO_010058"  # "alive"
SIO_DEAD = "http://semanticscience.org/resource/SIO_010059"    # "dead"
STATUS_MAP = {
    "yes": SIO_ALIVE, "alive": SIO_ALIVE, "living": SIO_ALIVE,
    "no": SIO_DEAD, "dead": SIO_DEAD, "deceased": SIO_DEAD,
}
STATUS_HEADER_RE = re.compile(r"\b(alive|vital\s*status|life\s*status)\b", re.I)


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
            iri = SEX_MAP.get(row[colidx[sex_col]].strip().lower())
            if not iri:
                stats["unmapped_skipped"] += 1
                continue
            date = row[colidx[bdate]].strip() if bdate else ""
            rows.append({"model": "Sex", "pid": pid, "attribute_type": iri,
                         "startdate": date, "enddate": date})
            seen.add(pid)
            stats["emitted"] += 1
    return rows, stats


def build_status(headers, body, results, searcher, args):
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
            iri = STATUS_MAP.get(row[colidx[status_col]].strip().lower())
            if not iri:
                stats["unmapped_skipped"] += 1
                continue
            d = row[colidx[sdate]].strip() if sdate else ""
            rows.append({"model": "Status", "pid": pid, "value_datatype": "xsd:string",
                         "attribute_type": iri, "startdate": d, "enddate": d})
            stats["emitted"] += 1
    return rows, stats


# ============================================================ date models ====
def _simple_date_model(model, headers, body, results):
    """Birthdate / Deathdate: value = the date (xsd:date); startdate=enddate =
    the date. Skips patients with no date."""
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
        rows.append({"model": model, "pid": pid, "value": d,
                     "value_datatype": "xsd:date", "startdate": d, "enddate": d})
        stats["emitted"] += 1
    return rows, stats


def build_birthdate(headers, body, results, searcher, args):
    return _simple_date_model("Birthdate", headers, body, results)


def build_deathdate(headers, body, results, searcher, args):
    return _simple_date_model("Deathdate", headers, body, results)


def build_symptoms_onset(headers, body, results, searcher, args):
    """value = onset date (xsd:date); startdate/enddate = record datestamp if
    present, else the onset date. `target` (specific symptom HPO) left blank —
    the dumps carry disease-level onset, not per-symptom onset."""
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


BUILDERS = {
    "Phenotype": build_phenotype, "Diagnosis": build_diagnosis, "Sex": build_sex,
    "Status": build_status, "Birthdate": build_birthdate,
    "Deathdate": build_deathdate, "Symptoms_onset": build_symptoms_onset,
}


def main():
    ap = argparse.ArgumentParser(description="Emit a CARE-SM v2 per-type template from a raw dump.")
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
    header = write_model_csv(a.model, rows, outpath)

    print(f"Wrote {len(rows)} {a.model} rows -> {outpath}")
    print(f"  columns: {','.join(header)}")
    for k, v in stats.items():
        if k.startswith("_"):
            print(f"  {k[1:]+' source':16} {v}")
    for k, v in stats.items():
        if not k.startswith("_"):
            print(f"  {k:24} {v}")


if __name__ == "__main__":
    main()
