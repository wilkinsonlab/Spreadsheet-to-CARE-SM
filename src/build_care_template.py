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

Usage (run from the repo root):
  python3 src/build_care_template.py mockdata/dump.mock -o out_dir/ --model Phenotype
  python3 src/build_care_template.py mockdata/dump.mock -o out_dir/ --model Sex --offline
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from types import SimpleNamespace

import profile_columns as pc

# ---- domain-knowledge lookup tables -------------------------------------------
# CURIE prefixes, sex/status value vocab, affirmative/negative tokens: all live
# in care_template_mappings.json, NOT here — see that file's _readme. Only the
# v2 Toolkit's enforced column contract (TOOLKIT_COLUMNS/MODEL_COLUMNS, below)
# stays in code: it's copied from the Toolkit's own source/example CSVs, not a
# curation decision.
_MAPPINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "care_template_mappings.json")
with open(_MAPPINGS_PATH) as _f:
    _M = json.load(_f)

CURIE_EXPAND = {k: v for k, v in _M["curie_expand"].items() if not k.startswith("_")}
SEX_MAP = {k: v for k, v in _M["sex_map"].items() if not k.startswith("_")}
_STATUS_LABEL_IRI = _M["status_label_iri"]
SIO_ALIVE = _STATUS_LABEL_IRI["alive"]
SIO_DEAD = _STATUS_LABEL_IRI["dead"]
STATUS_MAP = {v: _STATUS_LABEL_IRI[label] for v, label in _M["status_map"].items() if not v.startswith("_")}
STATUS_HEADER_RE = re.compile(_M["status_header_pattern"], re.I)
AFFIRMATIVE = set(_M["affirmative_tokens"])
NEGATIVE = set(_M["negative_tokens"])
NEG_RE = re.compile(_M["negation_pattern"], re.I)

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

# Per-model column list = exactly the v2 example CSV header for that model,
# plus `comments` — confirmed Optional (not Unused) for all 7 of these models
# in the CARE-SM v2 glossary (docs/glossary.md in the CARE-Semantic-Model-
# Version-2 repo; checked 2026-09 rather than assumed, per the lesson in
# PIPELINE.md §5.3 about guessing which fields belong). Used to carry the
# synthetic-PID note (see with_comments()) and, in future, real free-text
# comment content once a builder has any to put there.
MODEL_COLUMNS = {
    "Phenotype": ["model", "pid", "startdate", "enddate", "event_id", "target",
                  "value", "value_datatype", "duration_value",
                  "duration_startdate", "duration_enddate", "comments"],
    "Diagnosis": ["model", "pid", "startdate", "enddate", "event_id", "target",
                  "value", "value_datatype", "comments"],
    "Sex": ["model", "pid", "startdate", "enddate", "event_id", "attribute_type", "comments"],
    "Status": ["model", "pid", "value_datatype", "startdate", "enddate", "event_id",
               "attribute_type", "comments"],
    "Birthdate": ["model", "pid", "value_datatype", "startdate", "enddate",
                  "event_id", "value", "comments"],
    "Deathdate": ["model", "pid", "value_datatype", "startdate", "enddate",
                  "event_id", "value", "cause_id", "comments"],
    "Symptoms_onset": ["model", "pid", "value_datatype", "startdate", "enddate",
                       "event_id", "value", "target", "comments"],
    "Genetic": ["model", "pid", "event_id", "target", "attribute_type",
                "identifier_value", "comments"],
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


def split_parts(cell):
    return [p.strip() for p in re.split(r"\s*[;,]\s*|\s+and\s+", cell) if p.strip()]


def pick_pid(results):
    return next((r["column"] for r in results if r["lane"] == "KEY"), None)


SYNTHETIC_PID_COMMENT = "synthetic patient ID — no ID column found in source data"


def with_comments(row, args, extra_comment=None):
    """Attach a `comments` field to an emitted row dict: a synthetic-PID note
    when this run used one (see pc.ensure_pid_column), concatenated with any
    real free-text comment content a builder supplies via `extra_comment` --
    none do yet, but this keeps that path from silently overwriting the
    synthetic-PID note once one does."""
    parts = []
    if getattr(args, "synthetic_pid", False):
        parts.append(SYNTHETIC_PID_COMMENT)
    if extra_comment:
        parts.append(extra_comment)
    if parts:
        row["comments"] = "; ".join(parts)
    return row


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
        # filter to same-ontology candidates BEFORE rerank: an exact-label
        # match in a different ontology (e.g. "ophthalmoplegia" also exists
        # as a MONDO disease term) must not steal the slot from a same-
        # ontology candidate at rank 2/3 — it would just return None instead.
        candidates = [c for c in searcher.top_k(term, k=3) if c and c.get("prefix") == "hp"]
        hit, reranked = pc.pick_exact_match(term, candidates, args.score)
        if reranked:
            stats["reranked_exact_match"] += 1
        if hit and "_error" not in hit and hit.get("score", 0) >= args.score:
            return hit["iri"]
        return None

    def emit(pid, iri, value, date):
        return with_comments(
            {"model": "Phenotype", "pid": pid, "target": iri, "value": value,
             "value_datatype": "xsd:boolean", "startdate": date, "enddate": date},
            args)

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
        candidates = [c for c in searcher.top_k(term, k=3) if c and c.get("prefix") == "mondo"]
        hit, reranked = pc.pick_exact_match(term, candidates, args.score)
        if reranked:
            stats["reranked_exact_match"] += 1
        if hit and "_error" not in hit and hit.get("score", 0) >= args.score:
            return hit["iri"]
        return None

    def emit(pid, iri, date):
        return with_comments(
            {"model": "Diagnosis", "pid": pid, "target": iri, "value": "true",
             "value_datatype": "xsd:boolean", "startdate": date, "enddate": date},
            args)

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
# SEX_MAP/SIO_ALIVE/SIO_DEAD/STATUS_MAP/STATUS_HEADER_RE are loaded from
# care_template_mappings.json above.


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
            rows.append(with_comments(
                {"model": "Sex", "pid": pid, "attribute_type": iri,
                 "startdate": date, "enddate": date}, args))
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
            rows.append(with_comments(
                {"model": "Status", "pid": pid, "value_datatype": "xsd:string",
                 "attribute_type": iri, "startdate": d, "enddate": d}, args))
            stats["emitted"] += 1
    return rows, stats


# ============================================================ date models ====
def _simple_date_model(model, headers, body, results, args):
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
        rows.append(with_comments(
            {"model": model, "pid": pid, "value": d,
             "value_datatype": "xsd:date", "startdate": d, "enddate": d}, args))
        stats["emitted"] += 1
    return rows, stats


def build_birthdate(headers, body, results, searcher, args):
    return _simple_date_model("Birthdate", headers, body, results, args)


def build_deathdate(headers, body, results, searcher, args):
    return _simple_date_model("Deathdate", headers, body, results, args)


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
        rows.append(with_comments(
            {"model": "Symptoms_onset", "pid": pid, "value": onset,
             "value_datatype": "xsd:date", "startdate": rec or onset,
             "enddate": rec or onset}, args))
        stats["emitted"] += 1
    return rows, stats


# ============================================================== Genetic ======
# Unspecified-zygosity IRI (GENO_0000137) -- the intended default whenever a
# gene is resolved but no separate zygosity column/value exists, or the
# zygosity column's value isn't in the GENO vocabulary (see column_mappings.
# json's zygosity_values._readme and the gene/variant/zygosity triad design
# notes). This mirrors pc.GENO_BASE + "GENO_0000137" but is spelled out here
# rather than imported, since it's a fixed constant this builder owns.
UNSPECIFIED_ZYGOSITY_IRI = "http://purl.obolibrary.org/obo/GENO_0000137"


def build_genetic(headers, body, results, searcher, args):
    """Emits Genetic.csv rows per the gene/variant/zygosity truth table
    (project design notes, 2026-09-23):

      gene alone            -> target=gene, attribute_type=unspecified, identifier_value=blank
      gene + variant         -> target=gene, attribute_type=unspecified, identifier_value=variant
      gene + zygosity         -> target=gene, attribute_type=zygosity,    identifier_value=blank
      gene + variant + zygosity -> target=gene, attribute_type=zygosity, identifier_value=variant
      variant alone (+/- zygosity) -> target derived FROM the variant (see
          pc.resolve_variant_column); attribute_type=zygosity if present,
          else unspecified; identifier_value=variant
      zygosity alone (no gene, no variant) -> nonsense, no record possible
      no gene AND no resolvable gene-from-variant -> SKIPPED, not emitted
          with target blank -- CARE-SM v2's Genetic.target is Mandatory
          (docs/glossary.md in CARE-Semantic-Model-Version-2): a variant/
          zygosity report that isn't about any resolvable gene isn't a
          meaningful Genetic record at all.

    Reuses the per-column resolution profile_columns.py's classify() already
    did (each GENE/VARIANT/ZYGOSITY column's `proposed_mappings`, a list of
    {source: <raw value>, iri: <resolved IRI>} per distinct value) rather
    than re-resolving anything live -- this builder is pure row-walking
    business logic on top of already-classified/resolved columns, same
    pattern as build_diagnosis's CURIE/freetext handling.

    Multiple GENE, VARIANT, or ZYGOSITY columns in one file is not a case
    this builder guesses at: it's flagged and NO rows are emitted, since
    which column is authoritative is a human judgment call.
    """
    colidx = {h: i for i, h in enumerate(headers)}
    pid_col = pick_pid(results)

    gene_cols = [r for r in results if r["lane"] == "GENE"]
    variant_cols = [r for r in results if r["lane"] == "VARIANT"]
    zygosity_cols = [r for r in results if r["lane"] == "ZYGOSITY"]

    stats = Counter()
    stats["_pid"] = pid_col
    stats["_gene_col"] = gene_cols[0]["column"] if len(gene_cols) == 1 else "(none)"
    stats["_variant_col"] = variant_cols[0]["column"] if len(variant_cols) == 1 else "(none)"
    stats["_zygosity_col"] = zygosity_cols[0]["column"] if len(zygosity_cols) == 1 else "(none)"

    if len(gene_cols) > 1 or len(variant_cols) > 1 or len(zygosity_cols) > 1:
        stats["skipped_ambiguous_columns"] = (
            f"multiple columns in one lane (GENE={len(gene_cols)}, "
            f"VARIANT={len(variant_cols)}, ZYGOSITY={len(zygosity_cols)}) -- "
            "which is authoritative is a human judgment call, no rows emitted")
        return [], stats

    gene_col = gene_cols[0] if gene_cols else None
    variant_col = variant_cols[0] if variant_cols else None
    zygosity_col = zygosity_cols[0] if zygosity_cols else None

    if not gene_col and not variant_col:
        if zygosity_col:
            stats["skipped_zygosity_alone"] = (
                "zygosity column present with no gene or variant column -- "
                "not a meaningful Genetic record on its own, skipped")
        return [], stats

    gene_map = {m["source"]: m["iri"] for m in (gene_col["proposed_mappings"] if gene_col else [])}
    variant_gene_map = {m["source"]: m["iri"] for m in (variant_col["proposed_mappings"] if variant_col else [])}
    zygosity_map = {m["source"]: m["iri"] for m in (zygosity_col["proposed_mappings"] if zygosity_col else [])}

    rows = []
    for row in body:
        pid = row[colidx[pid_col]].strip() if pid_col else ""
        if not pid:
            continue

        gene_val = row[colidx[gene_col["column"]]].strip() if gene_col else ""
        variant_val = row[colidx[variant_col["column"]]].strip() if variant_col else ""
        zygosity_val = row[colidx[zygosity_col["column"]]].strip() if zygosity_col else ""

        # target: a direct Gene column is the more reliable source (see
        # resolve_gene_column) -- only fall back to deriving the gene from
        # the variant's own accession/rsID when there's no Gene column value.
        target_iri = gene_map.get(gene_val) if gene_val else None
        if not target_iri and variant_val:
            target_iri = variant_gene_map.get(variant_val)

        if not target_iri:
            stats["skipped_no_resolvable_gene"] += 1
            continue

        attribute_type = zygosity_map.get(zygosity_val) if zygosity_val else None
        if not attribute_type:
            attribute_type = UNSPECIFIED_ZYGOSITY_IRI
            stats["defaulted_unspecified_zygosity"] += 1

        rows.append(with_comments(
            {"model": "Genetic", "pid": pid, "target": target_iri,
             "attribute_type": attribute_type, "identifier_value": variant_val}, args))
        stats["emitted"] += 1
    return rows, stats


# ==================================================== query prewarming =======
def collect_builder_queries(headers, body, results):
    """Full (uncapped) query set build_phenotype/build_diagnosis will need,
    at per-ROW granularity. profile_columns.py's own dry-run (collect_literal_
    queries) only samples up to args.sample distinct values per column, which
    is fine for triage — but these builders walk every row of every patient,
    so on a dataset with more distinct values in a column than the triage
    sample cap, relying on the triage set alone would still leak one-at-a-
    time network calls during the actual build. Only Phenotype/Diagnosis
    SEARCH-lane columns call the searcher in these builders — Sex, Status,
    Birthdate, Deathdate and Symptoms_onset use no live search at all."""
    colidx = {h: i for i, h in enumerate(headers)}
    queries = set()

    for r in results:
        if r["lane"] != "SEARCH":
            continue
        model = r.get("care_sm_model")
        if model not in ("Phenotype", "Diagnosis"):
            continue
        ci = colidx[r["column"]]
        for row in body:
            cell = row[ci].strip()
            if not cell:
                continue
            for part in split_parts(cell):
                if model == "Phenotype" and NEG_RE.match(part):
                    part = NEG_RE.sub("", part, count=1).strip()
                if part:
                    queries.add(part)
    return queries


BUILDERS = {
    "Phenotype": build_phenotype, "Diagnosis": build_diagnosis, "Sex": build_sex,
    "Status": build_status, "Birthdate": build_birthdate,
    "Deathdate": build_deathdate, "Symptoms_onset": build_symptoms_onset,
    "Genetic": build_genetic,
}


def build_resolvers(offline):
    """Bug fix (2026-09-23): main() used to call pc.classify() with no
    gene_resolver/myvariant_resolver at all, so classify() fell through to
    its own defaults -- StubGeneResolver()/StubMyVariantResolver() -- on
    EVERY run, live or not. A live (non---offline) run's GENE/VARIANT lanes
    were silently resolved against the tiny hardcoded stub lexicons instead
    of mygene.info/myvariant.info, while --offline behaved identically by
    accident. profile_columns.py's own main() always got this right;
    build_care_template.py's main() didn't. Extracted to its own function so
    the offline/live choice is unit-testable without a full CLI run."""
    if offline:
        return pc.StubGeneResolver(), pc.StubMyVariantResolver()
    return pc.GeneResolver(), pc.MyVariantResolver()


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
    ap.add_argument("--batch-size", type=int, default=256,
                    help="queries per prewarm round trip against a live search service")
    ap.add_argument("--no-prewarm", action="store_true",
                    help="skip batch prewarming and query the network one at a time")
    a = ap.parse_args()

    args = SimpleNamespace(score=a.score, hit_fraction=a.hit_fraction,
                           small_vocab=a.small_vocab, sample=a.sample,
                           offline=a.offline, search_url=a.search_url)
    searcher = pc.StubSearcher() if a.offline else pc.HttpSearcher(a.search_url)
    gene_resolver, myvariant_resolver = build_resolvers(a.offline)
    headers, body, _ = pc.load_table(a.file)
    if not headers:
        raise SystemExit(f"No data found in {a.file}")

    t0 = time.time()
    if isinstance(searcher, pc.HttpSearcher) and not a.no_prewarm:
        _, triage_queries = pc.collect_literal_queries(headers, body, args)
        print(f"Offline triage: {time.time() - t0:.1f}s (free, no network) "
              f"-> {len(triage_queries)} unique live queries needed", file=sys.stderr)
        searcher.prewarm(triage_queries, batch_size=a.batch_size)

    results = [pc.classify(h, pc.col_values(body, i), searcher, args, idx=i,
                           gene_resolver=gene_resolver, myvariant_resolver=myvariant_resolver)
               for i, h in enumerate(headers)]

    headers, body, results, args.synthetic_pid = pc.ensure_pid_column(headers, body, results)
    if args.synthetic_pid:
        print("WARNING: no patient/record identifier column found in the source file — "
              "synthesized one from row position (column 'synthetic_row_id'). This ID is "
              "NOT a real registry identifier: it is only stable across the CARE-SM model "
              "CSVs produced by THIS run, not across re-exports or re-runs. Every emitted "
              f"row's `comments` field notes this ({SYNTHETIC_PID_COMMENT!r}). See "
              "ensure_pid_column()'s docstring in profile_columns.py.", file=sys.stderr)

    if isinstance(searcher, pc.HttpSearcher) and not a.no_prewarm:
        # second pass: the builders below walk every ROW (uncapped), which can
        # need queries beyond profile_columns.py's own triage sample cap.
        builder_queries = collect_builder_queries(headers, body, results)
        searcher.prewarm(builder_queries, batch_size=a.batch_size)

    rows, stats = BUILDERS[a.model](headers, body, results, searcher, args)
    if isinstance(searcher, pc.HttpSearcher):
        print(f"Total build time: {time.time() - t0:.1f}s", file=sys.stderr)
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
