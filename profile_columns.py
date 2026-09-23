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
import os
import re
import sys
import time
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

# ---- domain-knowledge lookup tables -------------------------------------------
# All header-trigger patterns and value vocabularies live in column_mappings.json,
# NOT here — so a curator can see/edit what maps to what without reading Python.
# This module only holds parsing mechanics (generic regex, date formats) and the
# CODE that consumes the loaded tables (thresholds, exclusion logic, rationale
# for HOW a table is applied — see e.g. resolve_gene_column's docstring).
DEFAULT_MAPPINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "column_mappings.json")


def load_mappings(path=DEFAULT_MAPPINGS_PATH):
    """Load column_mappings.json and compile its regex fields. Returns a dict
    of ready-to-use structures, keyed the same as the module-level constants
    that used to be hardcoded here (PII_PATTERNS, GENE_PATTERNS, etc.)."""
    with open(path) as f:
        raw = json.load(f)

    def rx(pattern):
        return re.compile(pattern, re.I)

    hp = raw["header_patterns"]
    date_subtype = [(rx(e["pattern"]), e["model"]) for e in raw["date_subtype"]]
    zyg_values = {geno_id: (entry["label"], set(entry["triggers"]))
                  for geno_id, entry in raw["zygosity_values"].items()
                  if not geno_id.startswith("_")}
    zygosity_lookup = {_normalize_label(trigger): geno_id
                        for geno_id, (_, triggers) in zyg_values.items()
                        for trigger in triggers}
    return {
        "pii_patterns": rx(hp["pii"]["pattern"]),
        "key_patterns": rx(hp["key"]["pattern"]),
        "gene_patterns": rx(hp["gene"]["pattern"]),
        "entrez_id_header": rx(hp["entrez_id_header"]["pattern"]),
        "hgnc_id_header": rx(hp["hgnc_id_header"]["pattern"]),
        "zygosity_header_patterns": rx(hp["zygosity"]["pattern"]),
        "variant_header_patterns": rx(hp["variant"]["pattern"]),
        "date_subtype": date_subtype,
        "boolean_tokens": set(raw["boolean_tokens"]),
        "sex_tokens": set(raw["sex_tokens"]),
        "numeric_sentinels": set(raw["numeric_sentinels"]),
        "zygosity_values": zyg_values,
        "zygosity_lookup": zygosity_lookup,
        "gene_stub_lexicon": {k: v for k, v in raw["gene_stub_lexicon"].items()
                               if not k.startswith("_")},
        "transcript_stub_lexicon": {k: v for k, v in raw["transcript_stub_lexicon"].items()
                                     if not k.startswith("_")},
        "rsid_stub_lexicon": {k: v for k, v in raw["rsid_stub_lexicon"].items()
                               if not k.startswith("_")},
        "lane_to_vartype": {k: v for k, v in raw["lane_to_vartype"].items()
                             if not k.startswith("_")},
    }


def _normalize_label(s):
    """Lowercase, strip punctuation, collapse whitespace — for exact-match
    comparison only (not for scoring)."""
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# MAPPINGS is loaded once at import time; the module-level names below are
# aliases kept so the rest of this file reads the same as when these tables
# were hardcoded here. To change what maps to what, edit column_mappings.json
# — not these aliases, and not the functions that consume them.
MAPPINGS = load_mappings()
PII_PATTERNS = MAPPINGS["pii_patterns"]
KEY_PATTERNS = MAPPINGS["key_patterns"]
GENE_PATTERNS = MAPPINGS["gene_patterns"]
ENTREZ_ID_HEADER = MAPPINGS["entrez_id_header"]
HGNC_ID_HEADER = MAPPINGS["hgnc_id_header"]
ZYGOSITY_HEADER_PATTERNS = MAPPINGS["zygosity_header_patterns"]
VARIANT_HEADER_PATTERNS = MAPPINGS["variant_header_patterns"]
DATE_SUBTYPE = MAPPINGS["date_subtype"]
BOOLEAN_TOKENS = MAPPINGS["boolean_tokens"]
SEX_TOKENS = MAPPINGS["sex_tokens"]
NUMERIC_SENTINELS = MAPPINGS["numeric_sentinels"]
ZYGOSITY_VALUE_MAP = MAPPINGS["zygosity_values"]
_ZYGOSITY_LOOKUP = MAPPINGS["zygosity_lookup"]
_LANE_TO_VARTYPE = MAPPINGS["lane_to_vartype"]
GENO_BASE = "http://purl.obolibrary.org/obo/"  # fixed ontology namespace, not a mapping table entry

# ---- lexical signals (parsing mechanics, not domain vocabulary) --------------
NUMERIC_ID_RE = re.compile(r"^\d+$")
HGNC_CURIE_PREFIX_RE = re.compile(r"^hgnc[:_]", re.I)
CURIE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[A-Za-z0-9_]+$")
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%Y"]

# HGVS/dbSNP notation shape — these are external-standard formats, not
# partner-specific vocabulary, so (like CURIE_RE/NUMERIC_ID_RE above) they
# stay in code rather than column_mappings.json.
#
# VARIANT_VALUE_RE: a c./g./n./m./p. HGVS prefix immediately followed by a
# position/residue (optionally preceded by "accession(gene):" or "accession:"
# context), OR a bare dbSNP rsID. e.g. "c.1521_1523del", "g.32317682G>A",
# "p.Phe508del", "NM_000341.3(SLC3A1):c.-82T>G", "rs1042522".
VARIANT_VALUE_RE = re.compile(r"(^|:)[cgpnm]\.[-*\w(]|^rs\d+$", re.I)
VARIANT_HIT_THRESHOLD = 0.8  # HGVS syntax is rigid -- a real variant column
                              # should be almost entirely well-formed; a low
                              # hit-fraction is itself suspicious, not just
                              # "partially mappable" (contrast HIT_FRACTION).

# gene-lookup-from-variant: two SEPARATE identifier spaces/APIs, never
# combined into one query (same collision-avoidance principle as
# resolve_gene_column's HGNC-vs-entrezgene split) -- rsIDs resolve via
# myvariant.info (dbSNP), transcript accessions via mygene.info's own
# scope=refseq. NC_/NG_/NW_/NT_ (chromosome/genomic-level) accessions are
# deliberately NOT matched here -- confirmed live (2026-09-23) that
# mygene.info's refseq scope returns 0 hits for a chromosome-level accession
# like NC_000023.9; only transcript-level (NM_/NR_/XM_/XR_) accessions are
# gene-specific enough to resolve this way.
TRANSCRIPT_ACCESSION_RE = re.compile(r"\b([NX][MR]_\d+(?:\.\d+)?)\b")
RSID_RE = re.compile(r"\b(rs\d+)\b", re.I)


# ============================================================ searchers ======
class HttpSearcher:
    """Calls the live nmdo-search endpoint; caches identical queries.

    Also supports batch prewarming (see `prewarm`): a wide dataset (one
    column per questionnaire item per visit) can need many thousands of
    distinct queries, and at one request each that's a multi-hour job no
    matter how much client-side concurrency is used — the bottleneck is
    server-side (see nmdo-search's HANDOFF.md). Batching amortizes it.
    """

    # suffix on the single-query URL -> suffix for its batch counterpart
    _BATCH_URL_SUFFIXES = {"/llm_search/search": "/llm_search/search_batch",
                            "/search": "/search_batch"}

    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self.cache = {}       # query -> top-1 hit (or {"_error": ...})
        self.cache_k = {}     # query -> full top-k list
        self._batch_url = self._derive_batch_url(url)
        self._batch_supported = self._batch_url is not None

    @classmethod
    def _derive_batch_url(cls, url):
        for suffix, batch_suffix in cls._BATCH_URL_SUFFIXES.items():
            if url.endswith(suffix):
                return url[: -len(suffix)] + batch_suffix
        return None

    def _fetch(self, query, k):
        u = self.url + "?" + urllib.parse.urlencode({"q": query, "top_k": k})
        with urllib.request.urlopen(u, timeout=self.timeout) as r:
            return json.load(r).get("results", [])

    def _fetch_batch(self, queries, k, timeout=None):
        body = json.dumps({"queries": queries, "top_k": k}).encode("utf-8")
        req = urllib.request.Request(
            self._batch_url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.load(r)["results"]  # {query: [hit, hit, ...]}

    def prewarm(self, queries, batch_size=256, quiet=False):
        """Populate this searcher's caches for every query in `queries` up
        front, via as few round trips as possible, so the real profiling/
        template-emission pass that follows hits cache instead of the
        network. Falls back to one-at-a-time `top()` calls — for this batch
        only — the first time the batch endpoint errors (e.g. talking to a
        search service that doesn't have it), so this is safe against any
        nmdo-search-compatible deployment, not just an upgraded one."""
        queries = sorted({q for q in queries if q and q.strip()} - self.cache.keys())
        if not queries:
            return
        if not quiet:
            print(f"Prewarming {len(queries)} unique queries "
                  f"({'batched' if self._batch_supported else 'sequential — no batch endpoint'}) ...",
                  file=sys.stderr)
        t0 = time.time()
        for i in range(0, len(queries), batch_size):
            chunk = queries[i:i + batch_size]
            if self._batch_supported:
                try:
                    results = self._fetch_batch(chunk, k=3, timeout=max(self.timeout, 60))
                    for q, hit_list in results.items():
                        self.cache[q] = hit_list[0] if hit_list else None
                        self.cache_k[q] = hit_list[:3]
                    continue
                except Exception as e:
                    self._batch_supported = False  # don't keep retrying a batch endpoint that isn't there
                    if not quiet:
                        print(f"  batch endpoint unavailable ({e}); "
                              f"falling back to sequential for the rest of this run", file=sys.stderr)
            for q in chunk:
                self.top(q)
            if not quiet:
                print(f"\r  {min(i + batch_size, len(queries))}/{len(queries)}", end="", file=sys.stderr)
                sys.stderr.flush()
        if not quiet:
            print(f"\nPrewarm done in {time.time() - t0:.1f}s", file=sys.stderr)

    def top(self, query):
        q = (query or "").strip()
        if not q:
            return None
        if q in self.cache:
            return self.cache[q]
        try:
            results = self._fetch(q, 3)
            self.cache_k[q] = results
            hit = results[0] if results else None
        except Exception as e:  # network hiccup: degrade to "no hit", note once
            hit = {"_error": str(e)}
        self.cache[q] = hit
        return hit

    def top_k(self, query, k=3):
        """Full ranked candidate list (not just top-1) — used where a caller
        wants to prefer an exact label/synonym match over the raw top score
        (see pick_exact_match). Reuses top()'s cache when k<=3, since top()
        already fetches top_k=3 from the server."""
        q = (query or "").strip()
        if not q:
            return []
        if q in self.cache_k and k <= 3:
            return self.cache_k[q][:k]
        try:
            results = self._fetch(q, k)
        except Exception:
            return []
        if k <= 3:
            self.cache_k[q] = results
        return results


# ==================================================== gene resolution =========
# Gene symbols/IDs are precise codes, not free text — cosine similarity over
# a sentence embedder is structurally the wrong tool for them regardless of
# indexing coverage (see nmdo-search project notes: PMP22/SMN1 never
# surfaced in top-3 even once correctly indexed, while short unrelated
# symbols outscored the real match). mygene.info is used instead: validated
# live to resolve bare symbols, Ensembl IDs, and HGNC IDs through the same
# endpoint, format-agnostically, in one call.
MYGENE_URL = "https://mygene.info/v3/query"
MYGENE_BATCH_MAX = 5000  # documented hard cap on query terms per POST; a
                          # request over this gets HTTP 400. Batch right up
                          # against it rather than conservatively under it —
                          # a wide dataset's gene column can have thousands
                          # of distinct values and each request is a real
                          # network round trip.
GENE_SAMPLE_SIZE = 20      # distinct values sampled for numeric-ID-space disambiguation
GENE_SPACE_HIT_THRESHOLD = 0.95  # a scope must clear this hit-fraction to be trusted
GENE_SPACE_MARGIN = 0.20         # ...and beat the OTHER scope by at least this much


class GeneResolver:
    """Batched gene-identifier resolution via mygene.info's POST /v3/query.

    species=human is hardcoded, never a per-partner setting: this pipeline is
    human-patient data only, and an unfiltered symbol query can match mouse/
    rat homologs (confirmed live: bare "SCN4A" without species=human returned
    599 hits spanning 5+ species).
    """

    def __init__(self, url=MYGENE_URL, timeout=30):
        self.url = url
        self.timeout = timeout
        self._cache = {}  # (scope, query) -> list of hit dicts (possibly empty)

    def batch_query(self, queries, scope, fields="symbol,name,HGNC"):
        """Resolve every distinct value in `queries` against a SINGLE scope
        (never combine scopes like HGNC+entrezgene — see resolve_gene_column
        docstring: a bare number can collide between ID spaces and silently
        return a confident but WRONG gene with no structural tell). Returns
        {query: [hit, ...]} — 0, 1, or >1 hits per query."""
        queries = sorted({q for q in queries if q})
        need = [q for q in queries if (scope, q) not in self._cache]
        for i in range(0, len(need), MYGENE_BATCH_MAX):
            chunk = need[i:i + MYGENE_BATCH_MAX]
            body = urllib.parse.urlencode({
                "q": ",".join(chunk), "scopes": scope, "species": "human",
                "fields": fields,
            }).encode("utf-8")
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    results = json.load(r)
            except Exception as e:
                results = [{"query": q, "_error": str(e)} for q in chunk]
            by_query = {}
            for hit in results:
                by_query.setdefault(hit["query"], []).append(hit)
            for q in chunk:
                self._cache[(scope, q)] = by_query.get(q, [])
        return {q: self._cache[(scope, q)] for q in queries}


class StubGeneResolver:
    """Offline stand-in for demos/CI — a tiny hardcoded lexicon of real NMD-
    panel genes (captured live from mygene.info, 2026-09-23). NOT a
    substitute for the live resolver; every hit is tagged `_stub`."""

    _LEX = MAPPINGS["gene_stub_lexicon"]  # symbol -> HGNC numeric id; see column_mappings.json
    _BY_HGNC = {v: k for k, v in _LEX.items()}
    _TRANSCRIPTS = MAPPINGS["transcript_stub_lexicon"]  # unversioned RefSeq accession -> symbol

    def batch_query(self, queries, scope, fields=None):
        out = {}
        for q in queries:
            if scope == "HGNC":
                sym = self._BY_HGNC.get(q)
                out[q] = [{"query": q, "symbol": sym, "HGNC": q, "_stub": True}] if sym else []
            elif scope == "entrezgene":
                out[q] = []  # stub lexicon has no Entrez IDs — deliberate: the
                              # offline path must never fabricate a same-number
                              # coincidence for the sampling tiebreak to reason about
            elif scope == "refseq":
                unversioned = q.split(".")[0]
                sym = self._TRANSCRIPTS.get(unversioned)
                hgnc = self._LEX.get(sym) if sym else None
                out[q] = [{"query": q, "symbol": sym, "HGNC": hgnc, "_stub": True}] if sym else []
            else:
                hgnc = self._LEX.get(q.upper())
                out[q] = [{"query": q, "symbol": q.upper(), "HGNC": hgnc, "_stub": True}] if hgnc else []
        return out


# ==================================================== variant->gene resolution
# dbSNP rsID resolution via myvariant.info — a SEPARATE API/identifier space
# from mygene.info's refseq scope (see TRANSCRIPT_ACCESSION_RE/RSID_RE
# comment above); never conflated into one call.
MYVARIANT_URL = "https://myvariant.info/v1/variant"
MYVARIANT_BATCH_MAX = 1000  # documented POST cap on myvariant.info


class MyVariantResolver:
    """Batched rsID -> gene resolution via myvariant.info's POST /v1/variant.
    An rsID can return multiple hits (one per alt allele at that genomic
    position) — batch_query returns ALL of them per query, unfiltered; the
    caller (resolve_variant_column) is the one that decides whether multiple
    hits agree on gene (clean resolve) or disagree (ambiguous -> review),
    mirroring how resolve_gene_column treats >1 hit from mygene.info."""

    def __init__(self, url=MYVARIANT_URL, timeout=30):
        self.url = url
        self.timeout = timeout
        self._cache = {}  # rsid -> list of hit dicts (possibly empty)

    def batch_query(self, rsids, fields="dbsnp.gene.symbol"):
        rsids = sorted({r for r in rsids if r})
        need = [r for r in rsids if r not in self._cache]
        for i in range(0, len(need), MYVARIANT_BATCH_MAX):
            chunk = need[i:i + MYVARIANT_BATCH_MAX]
            body = urllib.parse.urlencode({"ids": ",".join(chunk), "fields": fields}).encode("utf-8")
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    results = json.load(r)
            except Exception as e:
                results = [{"query": q, "_error": str(e)} for q in chunk]
            by_query = {}
            for hit in results:
                by_query.setdefault(hit["query"], []).append(hit)
            for q in chunk:
                self._cache[q] = by_query.get(q, [])
        return {r: self._cache[r] for r in rsids}


class StubMyVariantResolver:
    """Offline stand-in for demos/CI — a tiny hardcoded rsID lexicon
    (captured live from myvariant.info, 2026-09-23; NOT NMD-panel-specific,
    see column_mappings.json). NOT a substitute for the live resolver."""

    _LEX = MAPPINGS["rsid_stub_lexicon"]  # rsID -> gene symbol

    def batch_query(self, rsids, fields=None):
        out = {}
        for r in rsids:
            sym = self._LEX.get(r)
            out[r] = [{"query": r, "dbsnp": {"gene": {"symbol": sym}}, "_stub": True}] if sym else []
        return out


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

    def top_k(self, query, k=3):
        q = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
        if not q:
            return []
        scored = []
        for label, (prefix, kws) in self.LEX.items():
            overlap = len(q & kws)
            if not overlap:
                continue
            score = overlap / (len(q | kws) ** 0.5)
            scored.append({"score": round(min(score, 0.95), 3), "prefix": prefix,
                            "label": label, "iri": f"stub:{label}", "_stub": True})
        scored.sort(key=lambda h: -h["score"])
        return scored[:k]


# ==================================================== rerank / selection =====
def pick_exact_match(query, candidates, score_threshold):
    """Given the top-k candidates for `query`, prefer one whose label OR any
    synonym is an exact (normalized) match to the query text, over the raw
    top-scoring candidate — as long as that exact match still clears
    score_threshold. This catches a real, measured failure mode: the
    single highest-scoring neighbor is an imprecise/over-specified relative
    of the true term (e.g. query "proximal muscle weakness" top-scores on
    "Proximal upper limb muscle weakness" while an exact "Proximal muscle
    weakness" sits at rank 2 or 3 — see the 2026-09 model benchmark).

    Does NOT fix a different failure mode: a genuinely wrong/distractor
    VALUE outscoring a real one across different query strings within a
    column — that's profile_columns.py's hit-fraction guard's job, not
    this function's (this only reranks among candidates for ONE query).

    Returns (chosen_hit_or_None, reranked: bool) — `reranked` is True only
    when the override actually changed which candidate was chosen, so
    callers can log/count it rather than have it happen silently.
    """
    cands = [c for c in (candidates or [])
             if c and "_error" not in c and c.get("score", 0) >= score_threshold]
    if not cands:
        return None, False
    top = cands[0]
    nq = _normalize_label(query)

    def is_exact(c):
        if _normalize_label(c.get("label")) == nq:
            return True
        return any(_normalize_label(s) == nq for s in (c.get("synonyms") or []))

    if is_exact(top):
        return top, False  # top score is already the exact match
    for c in cands[1:]:
        if is_exact(c):
            return c, True
    return top, False


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


def resolve_gene_column(header, vals, gene_resolver, args):
    """Resolve a GENE-lane column's distinct values to NMDO's own HGNC IRI
    namespace (http://identifiers.org/hgnc/NNNNN), via mygene.info.

    ID-space detection, cheapest/most-reliable signal first:
      1. Header keyword (HGNC / Entrez / NCBI Gene ID) — free, no network,
         and more reliable than guessing from values (mirrors GENE_PATTERNS
         itself: a header hint beats trying to infer from data shape).
      2. Symbol-shaped values (contain any letter, e.g. "SCN4A") -> resolve
         directly against scopes=symbol,alias,retired,ensembl.gene. This
         combo is safe: empirically only BARE NUMBERS collide across ID
         spaces (HGNC numbering vs Entrez numbering); a symbol string can't
         accidentally also be a valid number in the other space.
      3. Only when values are purely numeric AND the header gives no hint:
         sample up to GENE_SAMPLE_SIZE distinct values and probe scope=HGNC
         and scope=entrezgene SEPARATELY — never combined. (Combining them
         was tested and rejected: querying a bare number like "10591" under
         both scopes at once returned TWO results tagged with the identical
         query string, one correct (HGNC 10591 = SCN4A) and one a
         completely different, confidently single-matched gene (Entrez
         10591 = DNPH1) — no multi-match or notfound marks the wrong one.)
         A scope is trusted only if it clears GENE_SPACE_HIT_THRESHOLD AND
         beats the other scope by GENE_SPACE_MARGIN. Even then, this branch
         ALWAYS returns confidence="low" (forces human review) — a live
         test of 19 known-HGNC numbers against the WRONG scope alone still
         "hit" 79% of the time, each one a different, confidently single-
         matched gene. That's too close to the 95% trust bar to auto-apply
         without a human checking a sample.
    """
    distinct = sorted(set(vals))
    stripped = [HGNC_CURIE_PREFIX_RE.sub("", v) for v in distinct]
    is_numeric_id = all(NUMERIC_ID_RE.match(v) for v in stripped)

    if not is_numeric_id:
        scope = "symbol,alias,retired,ensembl.gene"
        fields = "symbol,name,HGNC"
        queries = distinct
        id_space_note = "symbol-shaped values -> scopes=symbol,alias,retired,ensembl.gene"
        conf = "high"
    else:
        queries = stripped
        if HGNC_ID_HEADER.search(header):
            scope, id_space_note, conf = "HGNC", "header names HGNC explicitly", "high"
        elif ENTREZ_ID_HEADER.search(header):
            scope, id_space_note, conf = "entrezgene", "header names Entrez/NCBI Gene ID explicitly", "high"
        else:
            sample = queries[:GENE_SAMPLE_SIZE]
            hgnc_hits = gene_resolver.batch_query(sample, "HGNC", fields="symbol,HGNC")
            entrez_hits = gene_resolver.batch_query(sample, "entrezgene", fields="symbol,entrezgene")
            hgnc_frac = sum(1 for q in sample if hgnc_hits.get(q)) / len(sample) if sample else 0.0
            entrez_frac = sum(1 for q in sample if entrez_hits.get(q)) / len(sample) if sample else 0.0
            if hgnc_frac >= GENE_SPACE_HIT_THRESHOLD and hgnc_frac - entrez_frac >= GENE_SPACE_MARGIN:
                scope = "HGNC"
            elif entrez_frac >= GENE_SPACE_HIT_THRESHOLD and entrez_frac - hgnc_frac >= GENE_SPACE_MARGIN:
                scope = "entrezgene"
            else:
                scope = None
            id_space_note = (
                f"no header hint; sampled {len(sample)} numeric values against HGNC "
                f"(hit-fraction={hgnc_frac:.2f}) and entrezgene (hit-fraction={entrez_frac:.2f}) "
                + (f"-> guessed scope={scope}" if scope else "-> UNRESOLVED, ambiguous")
                + " — always human-review regardless of outcome (see docstring)")
            conf = "low"
        fields = "symbol,name,HGNC" if scope == "HGNC" else "symbol,name,entrezgene,HGNC"

    if scope is None:
        return {"distinct_values": distinct[:SMALL_VOCAB_MAX], "note": id_space_note,
                "confidence": "low", "proposed_mappings": []}

    hits = gene_resolver.batch_query(queries, scope, fields=fields)
    maps, unresolved, ambiguous = [], [], []
    for orig, q in zip(distinct, queries):
        h = hits.get(q, [])
        if not h:
            unresolved.append(orig)
        elif len(h) > 1:
            ambiguous.append(orig)
        else:
            hgnc_id = h[0].get("HGNC")
            if not hgnc_id:
                unresolved.append(orig)
                continue
            maps.append({"source": orig, "kind": "value", "short_id": f"hgnc:{hgnc_id}",
                         "iri": f"http://identifiers.org/hgnc/{hgnc_id}",
                         "label": h[0].get("symbol"), "prefix": "hgnc", "score": None})
    n = len(distinct)
    return {"distinct_values": distinct[:SMALL_VOCAB_MAX],
            "note": id_space_note + f"; resolved {len(maps)}/{n} distinct values via mygene.info (scope={scope})",
            "confidence": conf, "proposed_mappings": maps,
            "unresolved": unresolved[:10] or None, "ambiguous": ambiguous[:10] or None,
            "hit_fraction": round(len(maps) / n, 2) if n else 0.0}


# =================================================== zygosity resolution =====
# Zygosity is one leg of the gene/variant/zygosity triad (see project design
# notes, 2026-09-23): a column whose value states the allelic state of a
# variant/gene, e.g. "Heterozygous", "Hom", "Compound het". Unlike GENE
# resolution, there's no external identifier-resolution API for this — GENO's
# own OBO synonyms are almost nonexistent (only GENO_0000137/"unspecified
# zygosity" has real alt-labels: "unknown zygosity", "indeterminate zygosity",
# "no-call zygosity"). This vocabulary is built from expected clinical/
# registry shorthand instead, and is deliberately conservative: match is
# EXACT-after-normalization (lowercase, strip punctuation/whitespace via
# _normalize_label), never substring, to avoid false hits like "heterogeneous"
# matching on "het". Values that carry real zygosity information but aren't in
# this table (e.g. "biallelic", "monoallelic" -- clinically overloaded, can
# mean compound-het OR "two variants found" OR other things depending on lab
# convention) are deliberately left OUT and fall through to human review
# rather than guessed at.
#
# Only plain heterozygous/compound-heterozygous are covered -- NOT "simple
# heterozygous" (GENO_0000458): plain "het"/"heterozygous" always resolves to
# GENO_0000135, never inferred as compound (GENO_0000402) or simple (458)
# without an explicit qualifier in the value itself, and "simple heterozygous"
# has no trigger at all until real data is seen using that term (see
# HANDOFF/conversation, 2026-09-23 -- don't guess at synonyms for terms we
# haven't observed in partner data yet).
#
# The header pattern, the GENO id -> label/triggers table, and the exclusions
# above are all data, defined in column_mappings.json (see ZYGOSITY_HEADER_
# PATTERNS / ZYGOSITY_VALUE_MAP / _ZYGOSITY_LOOKUP aliases near the top of
# this file) -- edit that file, not this function, to add a synonym.
def resolve_zygosity_value(v):
    """Map one cell value to a GENO zygosity term, or flag it for human
    review. Returns a dict: {"geno_id", "short_id", "iri", "label"} on a
    match, or {"unmapped": v} when the value isn't in the vocabulary --
    the caller decides what "unmapped" means for the record as a whole
    (e.g. an empty cell should default to GENO_0000137, not fall in here)."""
    key = _normalize_label(v)
    geno_id = _ZYGOSITY_LOOKUP.get(key)
    if not geno_id:
        return {"unmapped": v}
    label = ZYGOSITY_VALUE_MAP[geno_id][0]
    return {"geno_id": geno_id, "short_id": f"geno:{geno_id.split('_')[1]}",
            "iri": GENO_BASE + geno_id, "label": label}


def resolve_zygosity_column(vals):
    """Column-level wrapper around resolve_zygosity_value: classify every
    distinct value, split into resolved/unresolved, in the same report shape
    classify() expects from the other lanes."""
    distinct = sorted(set(vals))
    maps, unresolved = [], []
    for v in distinct:
        r = resolve_zygosity_value(v)
        if "unmapped" in r:
            unresolved.append(v)
        else:
            maps.append({"source": v, "kind": "value", "short_id": r["short_id"],
                         "iri": r["iri"], "label": r["label"], "prefix": "geno", "score": None})
    n = len(distinct)
    conf = "high" if not unresolved else "medium"
    return {"distinct_values": distinct[:SMALL_VOCAB_MAX],
            "note": f"resolved {len(maps)}/{n} distinct values against the GENO zygosity vocabulary",
            "confidence": conf, "proposed_mappings": maps,
            "unresolved": unresolved[:10] or None,
            "hit_fraction": round(len(maps) / n, 2) if n else 0.0}


# ==================================================== variant resolution =====
def is_variant_like(v):
    return bool(VARIANT_VALUE_RE.search(v))


def variant_gene_queries(vals):
    """For every distinct value, extract whichever gene-identifying token is
    present (rsID takes priority over a transcript accession when a value
    somehow has both -- dbSNP is the more specific/authoritative source for
    its own variant). Returns (rsid_by_value, transcript_by_value) -- dicts
    from ORIGINAL value to the extracted token, only for values where one was
    found. Values with neither (bare c./g./p. notation, or only a chromosome-
    level NC_/NG_ accession) are absent from both -- there's nothing in the
    value itself to resolve a gene from.

    Transcript accessions are captured WITHOUT their version suffix (e.g.
    "NM_000341.3" -> "NM_000341"): confirmed live (2026-09-23) that
    mygene.info's POST batch endpoint (which GeneResolver uses) is
    unreliable with the version suffix on at least one real record --
    NM_000341.3 returned notfound via POST on 5/5 repeated calls despite
    resolving fine via a GET single-query AND via POST once unversioned,
    while a different accession (NM_000546.6) resolved fine versioned either
    way. Stripping the version sidesteps the inconsistency entirely and
    cannot cause a wrong resolution -- a RefSeq version only tracks sequence
    revisions of the SAME transcript/gene, never a different one."""
    rsid_by_value, transcript_by_value = {}, {}
    for v in vals:
        m = RSID_RE.search(v)
        if m:
            rsid_by_value[v] = m.group(1).lower()
            continue
        m = TRANSCRIPT_ACCESSION_RE.search(v)
        if m:
            transcript_by_value[v] = m.group(1).split(".")[0]
    return rsid_by_value, transcript_by_value


def resolve_variant_column(header, vals, gene_resolver, myvariant_resolver, args):
    """Resolve the GENE behind a VARIANT-lane column's distinct values, when
    no separate Gene column exists to supply it (the "variant alone" row of
    the gene/variant/zygosity truth table). Two SEPARATE identifier spaces,
    never combined into one call (same collision-avoidance principle as
    resolve_gene_column's HGNC-vs-entrezgene split):

      1. dbSNP rsID -> myvariant.info. A single rsID can return multiple hits
         (one per alt allele at that genomic position) -- treated as a clean
         resolve only if EVERY hit for that rsID agrees on gene symbol;
         disagreement is flagged ambiguous, exactly like resolve_gene_column
         treats >1 mygene.info hit.
      2. RefSeq transcript accession (NM_/NR_/XM_/XR_ only -- confirmed live
         2026-09-23 that chromosome-level NC_/NG_ accessions return 0 hits)
         -> mygene.info scope=refseq, reusing the same GeneResolver as the
         Gene lane.

    A value with neither token present (bare HGVS notation, no accession) has
    nothing to resolve a gene from and is reported unresolved.
    """
    distinct = sorted(set(vals))
    rsid_by_value, transcript_by_value = variant_gene_queries(distinct)

    rsid_hits = myvariant_resolver.batch_query(set(rsid_by_value.values())) if rsid_by_value else {}
    transcript_hits = (gene_resolver.batch_query(set(transcript_by_value.values()), "refseq",
                                                  fields="symbol,name,HGNC")
                        if transcript_by_value else {})

    # second hop: every rsID that cleanly resolved to exactly one gene symbol
    # needs that symbol turned into an HGNC id -- batched in ONE call across
    # the whole column, not one round trip per value.
    rsid_symbol = {}  # value -> resolved symbol (only where unambiguous)
    for v, rsid in rsid_by_value.items():
        symbols = {h.get("dbsnp", {}).get("gene", {}).get("symbol")
                   for h in rsid_hits.get(rsid, []) if h.get("dbsnp", {}).get("gene", {}).get("symbol")}
        if len(symbols) == 1:
            rsid_symbol[v] = next(iter(symbols))
    symbol_hits = (gene_resolver.batch_query(set(rsid_symbol.values()),
                                              "symbol,alias,retired,ensembl.gene", fields="symbol,HGNC")
                    if rsid_symbol else {})

    maps, unresolved, ambiguous, no_identifier = [], [], [], []
    for v in distinct:
        if v in rsid_by_value:
            hits = rsid_hits.get(rsid_by_value[v], [])
            n_symbols = len({h.get("dbsnp", {}).get("gene", {}).get("symbol")
                              for h in hits if h.get("dbsnp", {}).get("gene", {}).get("symbol")})
            if n_symbols == 0:
                unresolved.append(v)
            elif n_symbols > 1:
                ambiguous.append(v)
            else:
                sym = rsid_symbol[v]
                hgnc = symbol_hits.get(sym, [])
                hgnc_id = hgnc[0].get("HGNC") if len(hgnc) == 1 else None
                if not hgnc_id:
                    unresolved.append(v)
                else:
                    maps.append({"source": v, "kind": "value", "short_id": f"hgnc:{hgnc_id}",
                                 "iri": f"http://identifiers.org/hgnc/{hgnc_id}",
                                 "label": sym, "prefix": "hgnc", "score": None,
                                 "via": f"rsID {rsid_by_value[v]}"})
        elif v in transcript_by_value:
            h = transcript_hits.get(transcript_by_value[v], [])
            if not h:
                unresolved.append(v)
            elif len(h) > 1:
                ambiguous.append(v)
            else:
                hgnc_id = h[0].get("HGNC")
                if not hgnc_id:
                    unresolved.append(v)
                else:
                    maps.append({"source": v, "kind": "value", "short_id": f"hgnc:{hgnc_id}",
                                 "iri": f"http://identifiers.org/hgnc/{hgnc_id}",
                                 "label": h[0].get("symbol"), "prefix": "hgnc", "score": None,
                                 "via": f"transcript {transcript_by_value[v]}"})
        else:
            no_identifier.append(v)

    n = len(distinct)
    conf = "high" if not (unresolved or ambiguous or no_identifier) else "medium"
    return {"distinct_values": distinct[:SMALL_VOCAB_MAX],
            "note": (f"gene-from-variant: resolved {len(maps)}/{n} distinct values "
                     f"({len(rsid_by_value)} via rsID/myvariant.info, "
                     f"{len(transcript_by_value)} via transcript/mygene.info refseq scope)"),
            "confidence": conf, "proposed_mappings": maps,
            "unresolved": unresolved[:10] or None, "ambiguous": ambiguous[:10] or None,
            "no_identifier": no_identifier[:10] or None,
            "hit_fraction": round(len(maps) / n, 2) if n else 0.0}


# ============================================================ classify =======
def classify(header, values, searcher, args, idx=0, gene_resolver=None, myvariant_resolver=None):
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

    # 3.5 gene identifiers (header keyword; needs a lookup, not search) ------
    # Short symbolic codes (SCN4A, PMP22, ...) carry too little semantic
    # content for the embedder to discriminate reliably (see nmdo-search
    # notes) even once they're indexed correctly. The header is a much more
    # reliable signal than the values here, so route on it directly and skip
    # SEARCH entirely — resolve via mygene.info instead (see resolve_gene_column).
    if GENE_PATTERNS.search(header):
        res = resolve_gene_column(header, vals, gene_resolver or StubGeneResolver(), args)
        conf = res["confidence"]
        if conf == "high" and (res.get("unresolved") or res.get("ambiguous")):
            conf = "medium"  # some values didn't resolve cleanly -> still worth a human look
        return out("GENE", "Genetic", conf,
                   "gene identifier column (by header keyword) -> resolved via "
                   "mygene.info, NOT semantic search. " + res["note"],
                   distinct_values=res["distinct_values"],
                   proposed_mappings=res["proposed_mappings"],
                   unresolved=res.get("unresolved"), ambiguous=res.get("ambiguous"),
                   hit_fraction=res.get("hit_fraction"))

    # 3.6 variant identifiers (value-shape definitive, header confirmatory) --
    # Unlike GENE, where the header is the reliable signal, HGVS/rsID notation
    # is a rigid external standard: the VALUES are the definitive test here,
    # and partner header naming for "variant"/"allele" columns is expected to
    # be inconsistent, so it's only confirmatory. Disagreement between the two
    # signals is never silently resolved either way -- it goes to human
    # review rather than guessing (decided 2026-09-23).
    variant_hit_frac = frac(is_variant_like, vals)
    header_says_variant = bool(VARIANT_HEADER_PATTERNS.search(header))
    value_says_variant = variant_hit_frac >= VARIANT_HIT_THRESHOLD
    if value_says_variant or header_says_variant:
        if not value_says_variant:  # header says variant, values don't look like it -> disagreement
            return out("VARIANT", "Genetic", "low",
                       f"header suggests a variant column but only {variant_hit_frac:.0%} of "
                       "values look like HGVS/rsID notation -- header and value-shape signals "
                       "disagree, needs human review rather than an auto-guess",
                       hit_fraction=round(variant_hit_frac, 2))
        conf = "high" if header_says_variant else "medium"  # values alone still get a look if header didn't confirm
        res = resolve_variant_column(header, vals, gene_resolver or StubGeneResolver(),
                                      myvariant_resolver or StubMyVariantResolver(), args)
        if conf == "high" and (res.get("unresolved") or res.get("ambiguous") or res.get("no_identifier")):
            conf = "medium"
        agree_note = ("value shape + header agree" if header_says_variant
                       else "by value shape, header did not confirm")
        return out("VARIANT", "Genetic", conf,
                   f"variant column ({agree_note}) -> " + res["note"],
                   distinct_values=res["distinct_values"],
                   proposed_mappings=res["proposed_mappings"],
                   unresolved=res.get("unresolved"), ambiguous=res.get("ambiguous"),
                   no_identifier=res.get("no_identifier"), hit_fraction=res.get("hit_fraction"),
                   variant_value_hit_fraction=round(variant_hit_frac, 2))

    # 3.7 zygosity (header keyword; small closed vocabulary) -----------------
    if ZYGOSITY_HEADER_PATTERNS.search(header):
        res = resolve_zygosity_column(vals)
        return out("ZYGOSITY", "Genetic", res["confidence"],
                   "zygosity/allelic-state column (by header keyword) -> resolved against "
                   "GENO vocabulary. " + res["note"],
                   distinct_values=res["distinct_values"],
                   proposed_mappings=res["proposed_mappings"],
                   unresolved=res.get("unresolved"), hit_fraction=res.get("hit_fraction"))

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


# ==================================================== query prewarming =======
# What follows lets a caller discover, up front and for free (no network),
# every literal string classify() will pass to a searcher — so an HttpSearcher
# can prewarm them all via one batched round trip instead of one network call
# per column. Deliberately recomputed from the column's own values rather than
# read back off classify()'s returned report dict: that dict is a *display*
# format (e.g. its "sample" field is only the first 5 raw values, for the
# human-readable report), not a promise of exactly what got queried — reading
# it as a query plan silently under-covers columns classify() will still hit
# the network for at "real" run time. Recomputing directly from `vals` cannot
# drift from classify()'s own logic that way.
def literal_queries_for_column(header, vals, lane, args):
    queries = set()
    if lane in ("BOOLEAN", "NUMERIC"):
        queries.add(clean_header_for_search(header))
    elif lane == "DICTIONARY":
        queries.update(sorted(set(vals))[: args.small_vocab])
    elif lane == "SEARCH":
        sample = list(dict.fromkeys(vals))[: args.sample]
        for v in sample:
            for part in re.split(r"\s*[;,]\s*|\s+and\s+", v):
                part = part.strip()
                if len(part) >= 3:
                    queries.add(part)
    return queries


def ensure_pid_column(headers, body, results):
    """If no column was recognized as a patient/record identifier (lane ==
    'KEY'), synthesize one from row position and prepend it to headers/body/
    results. Every CARE-SM builder requires a pid to emit a row at all (its
    per-row loop does `if not pid: continue`), so a source file with no ID
    column doesn't error — it silently emits ZERO rows for every model, which
    reads as "nothing here was mappable" rather than "there was no ID column".
    Observed for real on a 17,431-column MYODRAFT export (2026-09): a
    de-identified/anonymised dump had no patient or record ID field at all.
    A synthetic ID is the only way to get any output at all in that case, so
    not backfilling one is strictly worse.

    CAVEAT this function cannot make safe by itself — callers must surface it,
    not just log it: the synthetic ID is row POSITION, not a real registry
    identity. It is stable across CARE-SM model CSVs generated from THIS SAME
    file in THIS SAME row order in one run family (so Phenotype.csv row N and
    Diagnosis.csv row N do refer to the same patient) — but it will NOT match
    up with a previous run's IDs if the source file is ever re-sorted,
    filtered, or re-exported. Never treat it as a persistent patient
    identifier outside the run that produced it.

    Returns (headers, body, results, injected: bool).
    """
    if any(r["lane"] == "KEY" for r in results):
        return headers, body, results, False

    width = max(len(str(len(body))), 4)
    synthetic_col = "synthetic_row_id"
    new_headers = [synthetic_col] + headers
    new_body = [[f"ROW_{i + 1:0{width}d}"] + row for i, row in enumerate(body)]
    synthetic_result = {
        "column": synthetic_col, "lane": "KEY", "care_sm_model": "pid",
        "confidence": "high", "review": True, "n_nonnull": len(body),
        "n_distinct": len(body), "sample": new_body[0][0:1] if new_body else [],
        "unit": None,
        "note": "SYNTHETIC — no patient/record identifier column was found in "
                "the source file; this is a row-position placeholder, not a "
                "real registry ID. Stable only within this run's outputs; "
                "do not treat as stable across re-exports or re-runs.",
    }
    new_results = [synthetic_result] + results
    return new_headers, new_body, new_results, True


def collect_literal_queries(headers, body, args):
    """Free (StubSearcher, no network) dry run purely to learn each column's
    lane — lane routing is decided by value SHAPE (is it mostly numeric? a
    small vocabulary? Yes/No tokens?), never by what a searcher returns, so
    any searcher gives the same lane for the same data. Returns (dry-run
    results, the literal query set). The dry-run results are NOT a substitute
    for the real classify() pass — model/confidence *within* a lane can
    depend on actual search scores — only the lane assignment is reusable."""
    stub = StubSearcher()
    gene_stub = StubGeneResolver()
    myvariant_stub = StubMyVariantResolver()
    dry_results = []
    queries = set()
    for i, h in enumerate(headers):
        vals = nonnull(col_values(body, i))
        r = classify(h, col_values(body, i), stub, args, idx=i, gene_resolver=gene_stub,
                     myvariant_resolver=myvariant_stub)
        dry_results.append(r)
        queries |= literal_queries_for_column(h, vals, r["lane"], args)
    return dry_results, queries


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
    lane_order = ["SEARCH", "CURIE", "GENE", "VARIANT", "ZYGOSITY", "DICTIONARY", "NUMERIC",
                  "BOOLEAN", "DATE", "KEY", "PII", "DROP"]
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
        if r.get("unresolved"):
            bits.append(f"unresolved={r['unresolved']}")
        if r.get("ambiguous"):
            bits.append(f"ambiguous={r['ambiguous']}")
        if r.get("no_identifier"):
            bits.append(f"no_identifier={r['no_identifier']}")
        if r.get("variant_value_hit_fraction") is not None:
            bits.append(f"variant_value_hit_fraction={r['variant_value_hit_fraction']}")
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
    ap.add_argument("--batch-size", type=int, default=256,
                    help="queries per prewarm round trip against a live search service")
    ap.add_argument("--no-prewarm", action="store_true",
                    help="skip batch prewarming and query the network one column at a time "
                         "(slow on wide datasets — see prewarm() docstring)")
    args = ap.parse_args()

    searcher = StubSearcher() if args.offline else HttpSearcher(args.search_url)
    gene_resolver = StubGeneResolver() if args.offline else GeneResolver()
    myvariant_resolver = StubMyVariantResolver() if args.offline else MyVariantResolver()
    headers, body, delim = load_table(args.file)
    if not headers:
        sys.exit(f"No data found in {args.file}")

    t0 = time.time()
    if isinstance(searcher, HttpSearcher) and not args.no_prewarm:
        _, queries = collect_literal_queries(headers, body, args)
        print(f"Offline triage: {time.time() - t0:.1f}s (free, no network) "
              f"-> {len(queries)} unique live queries needed", file=sys.stderr)
        searcher.prewarm(queries, batch_size=args.batch_size)

    results = [classify(h, col_values(body, i), searcher, args, idx=i, gene_resolver=gene_resolver,
                        myvariant_resolver=myvariant_resolver)
               for i, h in enumerate(headers)]
    if isinstance(searcher, HttpSearcher):
        print(f"Total profiling time: {time.time() - t0:.1f}s", file=sys.stderr)

    headers, body, results, synthetic_pid = ensure_pid_column(headers, body, results)
    if synthetic_pid:
        print("WARNING: no patient/record identifier column found — synthesized one "
              "from row position ('synthetic_row_id'). See ensure_pid_column() docstring: "
              "this ID is not a real registry identifier and is not stable across re-runs.",
              file=sys.stderr)

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
