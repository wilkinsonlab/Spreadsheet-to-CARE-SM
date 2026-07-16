#!/usr/bin/env python3
"""
profile_columns.py — Column profiler for the spreadsheet-to-CARE-SM pipeline.

Given a raw registry dump, decide for EACH column which processing "lane" it
belongs to, and (heuristically) which CARE-SM data model it will feed. This is
a *read-only triage* step: it never writes a CARE-SM template. It produces a
report a human can eyeball before we commit to the full transform.

The three mapping lanes (plus non-mapping routes):

  CURIE      values are already ontology IDs (e.g. ORPHA:70) -> expand + xref
  DICTIONARY small controlled vocabulary -> curated lookup beats semantic search
  SEARCH     free-text -> nmdo-search, guarded by score + hit-FRACTION thresholds
  ---
  DATE       bare dates -> disambiguated by header keyword, not by search
  NUMERIC    header-as-measurement -> search the HEADER, cell is the value
  BOOLEAN    header-as-concept, cell = presence/absence flag
  KEY/PII/DROP  identifiers, direct identifiers, and export artifacts -> excluded

Thresholds default to values calibrated against the hosted NMDO embedder, whose
"good match" scores sit around ~0.5-0.8 (NOT the textbook ~0.9). See the
companion SYNTHETIC-TEST-DATA-COOKBOOK.md for the rationale.

Usage:
  python3 profile_columns.py path/to/dump.mock
  python3 profile_columns.py dump.mock --json report.json --sample 20
  python3 profile_columns.py dump.mock --offline      # no network; stub searcher

stdlib only — no pip installs required.
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from statistics import median

DEFAULT_SEARCH_URL = "https://simpathic.services/llm_search/search"

# ---- thresholds (calibrated to the hosted embedder; override via CLI) --------
SCORE_THRESHOLD = 0.50      # a per-value hit counts if top-1 score >= this
HIT_FRACTION = 0.60         # a column is SEARCH-mappable if >= this share hit
SMALL_VOCAB_MAX = 12        # <= this many distinct values => controlled vocab
SAMPLE_VALUES = 20          # distinct values sampled per column for searching

# ---- lexical signals ---------------------------------------------------------
PII_PATTERNS = re.compile(
    r"\b(?:(?:first|last|middle|maiden|sur)\s*name|full\s*name|address|postcode|"
    r"zip|phone|email|nhs\s*number|ssn|initials)\b", re.I)
KEY_PATTERNS = re.compile(r"\b(patient\s*id|pid|record\s*id|subject\s*id|^id$)\b", re.I)
DATE_SUBTYPE = [  # (regex on header, CARE-SM model)
    (re.compile(r"death|deceased|died", re.I), "Deathdate"),
    (re.compile(r"birth|dob|d\.o\.b", re.I), "Birthdate"),
    (re.compile(r"onset", re.I), "Symptoms_onset"),
    (re.compile(r"first\s*visit|enrol|baseline", re.I), "First_visit"),
    (re.compile(r"diagnos", re.I), "Diagnosis"),
]
BOOLEAN_TOKENS = {
    "yes", "no", "y", "n", "true", "false", "positive", "negative",
    "present", "absent", "unknown", "not applicable", "n/a", "na",
    "carrier", "confirmed", "not confirmed",
}
SEX_TOKENS = {"male", "female", "m", "f", "intersex", "other", "unknown"}
CURIE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[A-Za-z0-9_]+$")
NUMERIC_SENTINELS = {"unable", "not done", "nd", "n/a", "na", "unknown", "missing"}
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%Y"]


# ============================================================ searchers ======
class HttpSearcher:
    """Calls the live nmdo-search endpoint; caches identical queries."""

    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self.cache = {}

    def top(self, query):
        q = (query or "").strip()
        if not q:
            return None
        if q in self.cache:
            return self.cache[q]
        u = self.url + "?" + urllib.parse.urlencode({"q": q, "top_k": 3})
        try:
            with urllib.request.urlopen(u, timeout=self.timeout) as r:
                results = json.load(r).get("results", [])
            hit = results[0] if results else None
        except Exception as e:  # network hiccup: degrade to "no hit", note once
            hit = {"_error": str(e)}
        self.cache[q] = hit
        return hit


class StubSearcher:
    """Offline stand-in for demos/CI. NOT a real embedder — a transparent
    keyword-overlap heuristic over a tiny NMD lexicon. Clearly labelled so it is
    never mistaken for real validation."""

    LEX = {  # term -> (prefix, keywords)
        "Scoliosis": ("hp", {"scoliosis", "spine", "curvature"}),
        "Muscle weakness": ("hp", {"weakness", "weak", "muscle", "proximal", "distal"}),
        "Ptosis": ("hp", {"ptosis", "eyelid", "drooping"}),
        "Dysphagia": ("hp", {"dysphagia", "swallow", "swallowing"}),
        "Gait disturbance": ("hp", {"gait", "walking", "walk", "waddling"}),
        "Duchenne muscular dystrophy": ("mondo", {"duchenne", "dmd", "muscular", "dystrophy"}),
        "Becker muscular dystrophy": ("mondo", {"becker", "muscular", "dystrophy"}),
        "Spinal muscular atrophy": ("mondo", {"spinal", "muscular", "atrophy", "sma"}),
        "10-Meter Walk/Run Test": ("ncit", {"10mwt", "10", "meter", "walk", "run", "test"}),
        "Elevated creatine kinase": ("hp", {"ck", "creatine", "kinase"}),
        "Cardiomyopathy": ("hp", {"cardiomyopathy", "cardiac", "heart"}),
    }

    def top(self, query):
        q = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
        if not q:
            return None
        best, best_s = None, 0.0
        for label, (prefix, kws) in self.LEX.items():
            overlap = len(q & kws)
            if not overlap:
                continue
            score = overlap / (len(q | kws) ** 0.5)  # rough Jaccard-ish
            if score > best_s:
                best, best_s = (label, prefix), score
        if not best:
            return None
        return {"score": round(min(best_s, 0.95), 3), "prefix": best[1],
                "label": best[0], "iri": f"stub:{best[0]}", "_stub": True}


# ============================================================ loading ========
def load_table(path):
    """Read a delimited file: sniff , vs ;, strip BOM, drop all-empty
    (export-artifact) columns. Returns (headers, rows) with rows as lists."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.reader(f, delimiter=delim)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], [], delim
    headers = [h.strip().strip('"') for h in rows[0]]
    body = rows[1:]
    # normalise row length to header length
    width = len(headers)
    body = [(r + [""] * width)[:width] for r in body]
    # drop columns whose header is empty/artifact AND whose values are all empty
    keep = []
    for i, h in enumerate(headers):
        col_vals = [r[i].strip() for r in body]
        artifact = (not h or set(h) <= set('"')) and not any(col_vals)
        if not artifact:
            keep.append(i)
    headers = [headers[i] for i in keep]
    body = [[r[i] for i in keep] for r in body]
    return headers, body, delim


def col_values(body, idx):
    return [r[idx].strip() for r in body]


def nonnull(values):
    return [v for v in values if v != ""]


# ============================================================ detectors ======
def is_date(v):
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    return False


def is_numeric(v):
    try:
        float(v.replace(",", "").split()[0])  # tolerate "1200 U/L"
        return True
    except (ValueError, IndexError):
        return False


def frac(pred, values):
    vals = nonnull(values)
    return (sum(1 for v in vals if pred(v)) / len(vals)) if vals else 0.0


def unit_from_header(header):
    m = re.search(r"[\(\[]\s*([^)\]]+?)\s*[\)\]]", header)
    return m.group(1) if m else None


def clean_header_for_search(header):
    h = re.sub(r"[\(\[].*?[\)\]]", " ", header)      # drop "(U/L)"
    h = re.sub(r"[_\-]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


# ============================================================ classify =======
def classify(header, values, searcher, args, idx=0):
    vals = nonnull(values)
    n_distinct = len(set(vals))
    ev = {"column": header, "n_nonnull": len(vals), "n_distinct": n_distinct,
          "sample": vals[:5], "unit": unit_from_header(header)}

    def out(lane, model, conf, note, **extra):
        ev.update(lane=lane, care_sm_model=model, confidence=conf, note=note)
        ev.update(extra)
        ev["review"] = conf != "high"
        return ev

    if not vals:
        return out("DROP", None, "high", "empty column / export artifact")

    # 1. direct identifiers (privacy) ---------------------------------------
    if PII_PATTERNS.search(header):
        return out("PII", None, "high", "direct identifier — DROP, never map")

    # 2. keys ----------------------------------------------------------------
    # NB: "all-unique numeric" alone is NOT enough — lab columns look like that
    # too. Only treat an unnamed all-unique numeric column as a key if it's the
    # first column (the usual id position) and has no unit in its header.
    all_unique_numeric = n_distinct == len(vals) and frac(is_numeric, vals) > 0.9
    if KEY_PATTERNS.search(header) or (idx == 0 and all_unique_numeric and not unit_from_header(header)):
        return out("KEY", "pid", "high", "record/patient identifier")

    # 3. CURIE (pre-coded) ---------------------------------------------------
    if frac(lambda v: bool(CURIE_RE.match(v)) and not v[0].isdigit(), vals) >= 0.8:
        prefix = Counter(v.split(":")[0].upper() for v in vals).most_common(1)[0][0]
        model = "Diagnosis" if DATE_SUBTYPE[-1][0].search(header) or prefix in {"ORPHA", "MONDO", "OMIM"} else None
        return out("CURIE", model, "high",
                   f"pre-coded {prefix} identifiers -> expand IRI + xref-resolve to NMDO",
                   curie_prefix=prefix)

    # 4. dates ---------------------------------------------------------------
    if frac(is_date, vals) >= 0.7:
        model, conf, note = None, "low", "date column — meaning not resolvable by search"
        for rx, m in DATE_SUBTYPE:
            if rx.search(header):
                model, conf, note = m, "medium", f"date -> {m} (by header keyword)"
                break
        return out("DATE", model, conf, note)

    # 5. boolean flag (header-as-concept) -----------------------------------
    low = {v.lower() for v in vals}
    if low <= BOOLEAN_TOKENS and len(low) <= 4:
        hit = searcher.top(clean_header_for_search(header))
        model, conf = None, "low"
        if hit and "_error" not in hit:
            if hit["prefix"] == "hp" and hit["score"] >= args.score:
                model, conf = "Phenotype", "medium"
            elif hit["prefix"] == "mondo" and hit["score"] >= args.score:
                model, conf = "Diagnosis", "medium"
        maps = [_map_hit(header, "header", hit)] if model and hit and "_error" not in hit else []
        return out("BOOLEAN", model, conf,
                   "presence/absence flag — header is the concept, cell is Yes/No",
                   header_hit=_fmt_hit(hit), proposed_mappings=maps)

    # 6. sex (special small vocab) ------------------------------------------
    if low <= SEX_TOKENS and len(low) <= 4:
        return out("DICTIONARY", "Sex", "high",
                   "small controlled vocab -> curated lookup (search is unreliable here)")

    # 7. numeric measurement (header-as-measurement) ------------------------
    num = frac(is_numeric, vals)
    sentinels = [v for v in vals if v.lower() in NUMERIC_SENTINELS]
    if num >= 0.6:
        hit = searcher.top(clean_header_for_search(header))
        model, conf = None, "low"
        if hit and "_error" not in hit:
            lbl = hit.get("label", "").lower()
            if hit["score"] >= args.score:
                if "test" in lbl or "scale" in lbl or "walk" in lbl:
                    model, conf = "Examination", "medium"
                elif hit["prefix"] in {"hp", "ncit"}:
                    # NMDO's analyte hits are the *phenotype* form -> Lab/Exam ambiguity
                    model, conf = "Laboratory", "low"
        dtype = "xsd:float" if any("." in v for v in vals if is_numeric(v)) else "xsd:integer"
        maps = ([_map_hit(clean_header_for_search(header), "header", hit)]
                if hit and "_error" not in hit and hit.get("score", 0) >= args.score else [])
        return out("NUMERIC", model, conf,
                   "header-as-measurement; verify Lab-vs-Examination + unit source",
                   header_hit=_fmt_hit(hit), value_datatype=dtype,
                   sentinels=sentinels[:3] or None, proposed_mappings=maps)

    # 8. small controlled vocabulary ----------------------------------------
    if n_distinct <= args.small_vocab and n_distinct / max(len(vals), 1) < 0.5:
        raw = {v: searcher.top(v) for v in sorted(set(vals))[:args.small_vocab]}
        maps = [_map_hit(v, "value", h) for v, h in raw.items()
                if h and "_error" not in h and h.get("score", 0) >= args.score]
        return out("DICTIONARY", None, "low",
                   "small controlled vocab -> build curated lookup; search only suggests",
                   distinct_values=sorted(set(vals))[:SMALL_VOCAB_MAX],
                   value_hits={v: _fmt_hit(h) for v, h in raw.items()},
                   proposed_mappings=maps)

    # 9. free text -> semantic search lane ----------------------------------
    sample = list(dict.fromkeys(vals))[:args.sample]
    scored = []
    for v in sample:
        # split multi-value cells so each concept is scored on its own
        for part in re.split(r"\s*[;,]\s*|\s+and\s+", v):
            part = part.strip()
            if len(part) < 3:
                continue
            hit = searcher.top(part)
            if hit and "_error" not in hit:
                scored.append((part, hit))
    if not scored:
        return out("SEARCH", None, "low", "free text but no ontology hits — likely unmappable/notes")
    top_scores = [h["score"] for _, h in scored]
    hits_over = [s for s in top_scores if s >= args.score]
    hit_fraction = len(hits_over) / len(top_scores)
    prefixes = Counter(h["prefix"] for _, h in scored if h["score"] >= args.score)
    dom_prefix = prefixes.most_common(1)[0][0] if prefixes else None
    model = {"hp": "Phenotype", "mondo": "Diagnosis", "orpha": "Diagnosis",
             "uberon": None, "ncit": None}.get(dom_prefix)
    # the hit-FRACTION guard: distractors hit on a scattered few, real columns on most
    maps = []
    if hit_fraction >= args.hit_fraction:
        conf = "high" if hit_fraction >= 0.8 and dom_prefix else "medium"
        note = f"free-text -> SEARCH lane (mappable); dominant prefix {dom_prefix}"
        # non-redundant: best hit per distinct source part, above the score bar
        best = {}
        for part, hit in scored:
            if hit["score"] >= args.score and hit["score"] > best.get(part, (None, -1))[1]:
                best[part] = (hit, hit["score"])
        maps = [_map_hit(part, "value", hit) for part, (hit, _) in sorted(best.items())]
    else:
        conf, model = "low", None
        note = (f"REJECTED as distractor: only {hit_fraction:.0%} of values clear "
                f"score {args.score} (median {median(top_scores):.2f}) — scattered hits")
    return out("SEARCH", model, conf, note,
               hit_fraction=round(hit_fraction, 2),
               median_score=round(median(top_scores), 3),
               dominant_prefix=dom_prefix,
               example_hit=_fmt_hit(scored[top_scores.index(max(top_scores))][1]),
               proposed_mappings=maps)


def _fmt_hit(hit):
    if not hit:
        return None
    if "_error" in hit:
        return {"error": hit["_error"]}
    return {"score": hit.get("score"), "prefix": hit.get("prefix"), "label": hit.get("label")}


def _map_hit(source, kind, hit):
    """One proposed mapping row for the curation workbook."""
    return {"source": source, "kind": kind,
            "short_id": hit.get("short_id"), "iri": hit.get("iri"),
            "label": hit.get("label"), "prefix": hit.get("prefix"),
            "score": hit.get("score")}


# ============================================================ report =========
def print_report(path, delim, headers, results, args):
    print(f"\n{'='*78}\nCOLUMN PROFILE  ·  {path}")
    print(f"delimiter={delim!r}   columns={len(headers)}   "
          f"searcher={'OFFLINE-STUB' if args.offline else args.search_url}")
    print(f"thresholds: score>={args.score}  hit_fraction>={args.hit_fraction}\n{'='*78}")
    lane_order = ["SEARCH", "CURIE", "DICTIONARY", "NUMERIC", "BOOLEAN", "DATE",
                  "KEY", "PII", "DROP"]
    for r in sorted(results, key=lambda x: (lane_order.index(x["lane"]), x["column"])):
        flag = "  ⚠ REVIEW" if r["review"] else ""
        model = r["care_sm_model"] or "—"
        print(f"\n▸ {r['column']!r}")
        print(f"    lane={r['lane']:<10} model={model:<16} confidence={r['confidence']}{flag}")
        print(f"    {r['note']}")
        bits = []
        if r.get("unit"):
            bits.append(f"unit(header)={r['unit']!r}")
        if r.get("hit_fraction") is not None:
            bits.append(f"hit_fraction={r['hit_fraction']} median={r.get('median_score')}")
        if r.get("dominant_prefix"):
            bits.append(f"prefix={r['dominant_prefix']}")
        if r.get("header_hit"):
            hh = r["header_hit"]
            bits.append(f"header→{hh.get('label')}({hh.get('prefix')},{hh.get('score')})")
        if r.get("example_hit"):
            eh = r["example_hit"]
            bits.append(f"e.g.→{eh.get('label')}({eh.get('prefix')},{eh.get('score')})")
        if r.get("curie_prefix"):
            bits.append(f"curie={r['curie_prefix']}")
        if r.get("sentinels"):
            bits.append(f"sentinels={r['sentinels']}")
        if bits:
            print("    " + "  ".join(bits))
    # summary
    print(f"\n{'-'*78}\nSUMMARY")
    lanes = Counter(r["lane"] for r in results)
    for lane in lane_order:
        if lanes.get(lane):
            print(f"  {lane:<11} {lanes[lane]}")
    review = [r["column"] for r in results if r["review"]]
    print(f"  needs human review: {len(review)} -> {review}")


# variable_type is the vocabulary a generative model (DBM/VAE) needs; it falls
# out of the same lane classification the CARE-SM mapping uses (dual-use).
_LANE_TO_VARTYPE = {"BOOLEAN": "binary", "DICTIONARY": "categorical",
                    "CURIE": "categorical", "NUMERIC": "continuous",
                    "DATE": "date", "KEY": "identifier", "SEARCH": "freetext",
                    "PII": "ignore", "DROP": "ignore"}


def data_dictionary(headers, body, results):
    """Emit a per-column data dictionary — the natural by-product of profiling,
    and the variable spec a generative model would train from."""
    dd = []
    for i, r in enumerate(results):
        vals = nonnull(col_values(body, i))
        vtype = _LANE_TO_VARTYPE.get(r["lane"], "unknown")
        entry = {"column": r["column"], "variable_type": vtype,
                 "care_sm_model": r.get("care_sm_model"), "lane": r["lane"],
                 "n_nonnull": len(vals), "n_distinct": len(set(vals)),
                 "unit": r.get("unit")}
        if vtype in ("categorical", "binary"):
            entry["levels"] = sorted(set(vals))
        elif vtype == "continuous":
            nums = [float(v.replace(",", "").split()[0]) for v in vals if is_numeric(v)]
            entry["range"] = [min(nums), max(nums)] if nums else None
            entry["value_datatype"] = r.get("value_datatype")
        dd.append(entry)
    return dd


def main():
    ap = argparse.ArgumentParser(description="Profile spreadsheet columns for CARE-SM mapping.")
    ap.add_argument("file")
    ap.add_argument("--search-url", default=DEFAULT_SEARCH_URL)
    ap.add_argument("--offline", action="store_true", help="use offline stub searcher")
    ap.add_argument("--score", type=float, default=SCORE_THRESHOLD)
    ap.add_argument("--hit-fraction", dest="hit_fraction", type=float, default=HIT_FRACTION)
    ap.add_argument("--small-vocab", dest="small_vocab", type=int, default=SMALL_VOCAB_MAX)
    ap.add_argument("--sample", dest="sample", type=int, default=SAMPLE_VALUES)
    ap.add_argument("--json", help="also write full evidence to this JSON path")
    ap.add_argument("--data-dictionary", dest="data_dictionary",
                    help="write a per-column data dictionary (variable spec) to this JSON path")
    args = ap.parse_args()

    searcher = StubSearcher() if args.offline else HttpSearcher(args.search_url)
    headers, body, delim = load_table(args.file)
    if not headers:
        sys.exit(f"No data found in {args.file}")
    results = [classify(h, col_values(body, i), searcher, args, idx=i) for i, h in enumerate(headers)]

    print_report(args.file, delim, headers, results, args)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nfull evidence -> {args.json}")
    if args.data_dictionary:
        with open(args.data_dictionary, "w") as f:
            json.dump(data_dictionary(headers, body, results), f, indent=2)
        print(f"data dictionary -> {args.data_dictionary}")


if __name__ == "__main__":
    main()
