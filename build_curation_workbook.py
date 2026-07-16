#!/usr/bin/env python3
"""
build_curation_workbook.py — turn the profiler's proposed mappings into an
Excel curation workbook for the partners' review team.

It runs the same column profiling as profile_columns.py, collects every
*non-redundant* proposed mapping (original term -> mapped term + label), and
writes an .xlsx whose "Curator verdict" column is a drop-down restricted to the
three agreed responses:

    accurate mapping        the mapping is correct and precise
    imprecise mapping       correct but too coarse -> tells curators a more
                            granular NMDO term is probably needed
    unacceptable mapping    the mapping is wrong

A free-text "Notes / suggested term" column lets a reviewer say *what* the
granular term should be (especially useful alongside "imprecise mapping").

Only *fuzzy* mappings (semantic-search / header / dictionary lanes) are listed —
deterministic routes (pre-coded CURIEs, dates, keys, dropped PII) need no
curation and are summarised on the Instructions sheet instead.

No third-party libraries: the .xlsx is written directly (stdlib zipfile + XML),
so it runs anywhere the profiler does, and the resulting file opens in Excel /
LibreOffice with working drop-downs.

Usage:
  python3 build_curation_workbook.py mockdata/dump.mock -o curation.xlsx
  python3 build_curation_workbook.py mockdata/dump.mock -o curation.xlsx --offline
"""

import argparse
import zipfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

import profile_columns as pc

VERDICTS = ["accurate mapping", "imprecise mapping", "unacceptable mapping"]


# ======================================================= minimal xlsx ========
def _col_letter(n):  # 1 -> A, 27 -> AA
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(col, row, value, style=0):
    ref = f"{_col_letter(col)}{row}"
    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape(str(value))
    return (f'<c r="{ref}" t="inlineStr" s="{style}">'
            f'<is><t xml:space="preserve">{text}</t></is></c>')


def _sheet_xml(header, rows, widths, validation=None):
    """validation = (col_index_1based, [options]) applied to data rows."""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
    # freeze the header row
    out.append('<sheetViews><sheetView workbookViewId="0">'
               '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
               '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
               '</sheetView></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if widths:
        out.append("<cols>")
        for i, w in enumerate(widths, start=1):
            out.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        out.append("</cols>")
    out.append("<sheetData>")
    out.append(f'<row r="1">{"".join(_cell(i, 1, h, 1) for i, h in enumerate(header, 1))}</row>')
    for ri, r in enumerate(rows, start=2):
        cells = "".join(_cell(ci, ri, v) for ci, v in enumerate(r, 1))
        out.append(f'<row r="{ri}">{cells}</row>')
    out.append("</sheetData>")
    ncols = len(header)
    last = len(rows) + 1
    out.append(f'<autoFilter ref="A1:{_col_letter(ncols)}{max(last,1)}"/>')
    if validation and rows:
        col, options = validation
        letter = _col_letter(col)
        formula = '"' + ",".join(options) + '"'
        out.append('<dataValidations count="1">'
                   f'<dataValidation type="list" allowBlank="1" showInputMessage="1" '
                   f'showErrorMessage="1" sqref="{letter}2:{letter}{last}">'
                   f'<formula1>{escape(formula)}</formula1></dataValidation>'
                   '</dataValidations>')
    out.append("</worksheet>")
    return "".join(out)


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
           '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill>'
           '<fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border/></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
           '</styleSheet>')


def write_xlsx(path, sheets):
    """sheets = [(name, header, rows, widths, validation_or_None), ...]"""
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(len(sheets)):
        ct.append(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    ct.append("</Types>")

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')

    sheet_tags, wb_rels = [], []
    for i, (name, *_rest) in enumerate(sheets):
        rid = f"rId{i+1}"
        sheet_tags.append(f'<sheet name="{escape(name)}" sheetId="{i+1}" r:id="{rid}"/>')
        wb_rels.append(f'<Relationship Id="{rid}" '
                       'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                       f'Target="worksheets/sheet{i+1}.xml"/>')
    styles_rid = f"rId{len(sheets)+1}"
    wb_rels.append(f'<Relationship Id="{styles_rid}" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                   'Target="styles.xml"/>')

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{"".join(sheet_tags)}</sheets></workbook>')
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     f'{"".join(wb_rels)}</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, (name, header, rows, widths, validation) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", _sheet_xml(header, rows, widths, validation))


# ======================================================= build rows ==========
MAP_BASIS = {"value": "cell value", "header": "column header"}


def collect_mappings(results):
    """Flatten proposed_mappings across columns, non-redundant per
    (source column, original term, mapped id)."""
    seen, rows = set(), []
    for r in results:
        for m in r.get("proposed_mappings") or []:
            mid = m.get("short_id") or (m.get("iri") or "").rsplit("/", 1)[-1]
            key = (r["column"], m["source"], mid)
            if key in seen:
                continue
            seen.add(key)
            rows.append([
                "",                                   # Curator verdict (drop-down) — FIRST
                r["column"],                          # Source column
                r.get("care_sm_model") or "—",        # CARE-SM model
                MAP_BASIS.get(m["kind"], m["kind"]),  # Map basis
                m["source"],                          # Original term
                mid,                                  # Mapped ID
                m.get("label") or "",                 # Mapped label
                (m.get("prefix") or "").upper(),      # Ontology
                m.get("score") if m.get("score") is not None else "",  # Score
                "",                                   # Notes / suggested term
            ])
    rows.sort(key=lambda x: (x[1].lower(), str(x[4]).lower()))
    return rows


CURATION_HEADER = ["Curator verdict", "Source column", "CARE-SM model", "Map basis",
                   "Original term", "Mapped ID", "Mapped label", "Ontology", "Score",
                   "Notes / suggested term"]
CURATION_WIDTHS = [20, 26, 15, 13, 34, 20, 34, 9, 7, 34]


def instructions_sheet(src, results, n_maps):
    lanes = pc.Counter(r["lane"] for r in results)
    rows = [
        ["Source file", src],
        ["Mappings to review", n_maps],
        ["", ""],
        ["HOW TO USE", ""],
        ["1.", "For each row, pick a value in the 'Curator verdict' drop-down."],
        ["2.", "accurate mapping = correct and precise."],
        ["3.", "imprecise mapping = correct but too coarse; NMDO likely needs a "
               "more granular term. Suggest it in 'Notes / suggested term'."],
        ["4.", "unacceptable mapping = wrong mapping."],
        ["", ""],
        ["NOT listed here (need no curation):", ""],
        ["  pre-coded CURIE columns", "e.g. ORPHA: diagnosis codes -> deterministic expansion"],
        ["  date / key / dropped-PII columns", "no ontology mapping performed"],
        ["", ""],
        ["Lane distribution (all columns):", ""],
    ]
    for lane, c in lanes.most_common():
        rows.append([f"  {lane}", c])
    return ("Instructions", ["Field", "Value"], rows, [34, 60], None)


def main():
    ap = argparse.ArgumentParser(description="Build an Excel curation workbook from proposed mappings.")
    ap.add_argument("file")
    ap.add_argument("-o", "--out", required=True, help="output .xlsx path")
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
    headers, body, _delim = pc.load_table(a.file)
    if not headers:
        raise SystemExit(f"No data found in {a.file}")
    results = [pc.classify(h, pc.col_values(body, i), searcher, args, idx=i)
               for i, h in enumerate(headers)]

    rows = collect_mappings(results)
    sheets = [
        ("Mappings to review", CURATION_HEADER, rows, CURATION_WIDTHS, (1, VERDICTS)),
        instructions_sheet(a.file, results, len(rows)),
    ]
    write_xlsx(a.out, sheets)
    print(f"Wrote {len(rows)} mappings to review -> {a.out}")
    by_col = pc.Counter(r[1] for r in rows)
    for col, c in by_col.most_common():
        print(f"  {c:>3}  {col}")


if __name__ == "__main__":
    main()
