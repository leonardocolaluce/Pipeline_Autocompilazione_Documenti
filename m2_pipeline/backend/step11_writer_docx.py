#!/usr/bin/env python3
from __future__ import annotations


import argparse
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import xml.etree.ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W_NS, "v": V_NS, "o": O_NS, "w10": W10_NS, "mc": MC_NS}


@dataclass(frozen=True)
class Row:
    page: int
    x0: float
    y0: float
    w: float
    h: float
    text: str


def _win_to_wsl_path(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return p
    if os.name == "nt":
        return p
    p = p.replace("/", "\\")
    m = re.match(r"^([A-Za-z]):\\(.*)$", p)
    if not m:
        return p.replace("\\", "/")
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _register_docx_namespaces(doc_xml: bytes) -> None:
    # Best-effort register whatever the document already declares, then our known set.
    try:
        head = doc_xml.decode("utf-8", errors="ignore")
        m = re.search(r"<w:document\s+([^>]+)>", head)
        if m:
            for pref, uri in re.findall(r'xmlns:([A-Za-z0-9]+)="([^"]+)"', m.group(1)):
                try:
                    ET.register_namespace(pref, uri)
                except Exception:
                    pass
    except Exception:
        pass
    ET.register_namespace("w", W_NS)
    ET.register_namespace("v", V_NS)
    ET.register_namespace("o", O_NS)
    ET.register_namespace("w10", W10_NS)
    ET.register_namespace("mc", MC_NS)


def _page_height(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 842.0
    # Word stores size in twentieths of a point.
    return _num(pg_sz.get(f"{{{W_NS}}}h"), 16840.0) / 20.0


def _page_width(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 595.0
    return _num(pg_sz.get(f"{{{W_NS}}}w"), 11900.0) / 20.0


def _bbox_as_rect(bb: list[Any]) -> tuple[float, float, float, float]:
    """
    Normalize bbox to (x0,y0,x1,y1) in points.
    Supports [x0,y0,x1,y1] and [x0,y0,w,h] heuristically.
    """
    x0, y0, a, b = (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    # Dimension-style bbox (x0,y0,w,h) heuristic.
    # We consider it width/height if:
    # - width/height are positive and "small enough" to be plausible dimensions
    # - and interpreting (a,b) as absolute coordinates would require swapping
    #   because they are above/left of (x0,y0) (common when a,b are actually w,h).
    if a >= 0 and b >= 0 and a <= 1000 and b <= 300:
        if (a < x0) or (b < y0):
            return x0, y0, x0 + a, y0 + b
    nx0, nx1 = (x0, a) if x0 <= a else (a, x0)
    ny0, ny1 = (y0, b) if y0 <= b else (b, y0)
    return nx0, ny0, nx1, ny1


def _answer(row: dict[str, Any]) -> str:
    value = str(row.get("answer") or "").strip()
    return "" if value in {"", "N/D"} else value


def _bbox(row: dict[str, Any]) -> list[Any] | None:
    value = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 4 else None


def _page(row: dict[str, Any]) -> int:
    try:
        return max(1, int(row.get("page") or 1))
    except Exception:
        return 1


def _wrap_text_lines(text: str, width: float, font_size: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    max_chars = max(1, int(width / max(1.0, font_size * 0.55)))
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = w if not cur else f"{cur} {w}"
        if len(cand) <= max_chars:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_font_size_to_box(text: str, width: float, height: float, start_fs: int = 10) -> int:
    fs = max(6, int(start_fs))
    height = max(6.0, float(height))
    while fs > 6:
        lines = _wrap_text_lines(text, width, fs)
        needed = max(1, len(lines)) * (fs * 1.18)
        if needed <= height:
            break
        fs -= 1
    return max(6, fs)


def _first_body_paragraph(root: ET.Element) -> ET.Element:
    body = root.find("w:body", NS)
    if body is None:
        raise RuntimeError("DOCX non valido: manca w:body")
    p = body.find("./w:p", NS)
    if p is None:
        # Create a minimal paragraph to anchor shapes if the document has none.
        p = ET.SubElement(body, f"{{{W_NS}}}p")
    return p


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").split())


def _all_paragraphs_in_order(root: ET.Element) -> list[ET.Element]:
    body = root.find("w:body", NS)
    if body is None:
        return root.findall(".//w:p", NS)
    # Depth-first order matches document flow well enough for anchoring.
    return body.findall(".//w:p", NS)


def _paragraph_text(p: ET.Element) -> str:
    texts = [t.text for t in p.findall(".//w:t", NS) if t.text]
    return _normalize_ws("".join(texts))


def _build_page_anchors_from_labels(root: ET.Element, json_rows: list[dict[str, Any]]) -> dict[int, ET.Element]:
    """
    Build anchors {page -> paragraph} by searching for labels from JSON, sequentially.

    This avoids relying on w:lastRenderedPageBreak, which is unreliable when tables span pages.
    We search paragraphs in document order and, for each page, try a handful of longer labels
    from that page, starting the search AFTER the previous page anchor (monotonic cursor).
    """
    paragraphs = _all_paragraphs_in_order(root)
    para_texts = [_paragraph_text(p).lower() for p in paragraphs]

    by_page: dict[int, list[dict[str, Any]]] = {}
    for r in json_rows:
        page = _page(r)
        label = str(r.get("label") or "").strip()
        if not label:
            continue
        # Skip synthetic table column labels.
        if re.fullmatch(r"col_\d+", label.strip().lower()):
            continue
        bb = _bbox(r)
        if not bb:
            continue
        by_page.setdefault(page, []).append(r)

    anchors: dict[int, ET.Element] = {}
    cursor = 0
    for page in sorted(by_page.keys()):
        # Prefer longer labels, and those nearer the top of the page (smaller y0).
        candidates: list[tuple[int, float, str]] = []
        for r in by_page[page]:
            label = _normalize_ws(str(r.get("label") or ""))
            if len(label) < 8:
                continue
            bb = _bbox(r) or [0, 0, 0, 0]
            candidates.append((len(label), float(bb[1]), label.lower()))
        candidates.sort(key=lambda t: (-t[0], t[1]))

        found_idx: Optional[int] = None
        for _, _, needle in candidates[:10]:
            for i in range(cursor, len(para_texts)):
                if needle and needle in para_texts[i]:
                    found_idx = i
                    break
            if found_idx is not None:
                break
        if found_idx is not None:
            anchors[page] = paragraphs[found_idx]
            cursor = found_idx  # monotonic

    return anchors


def _paragraph_page_anchors(root: ET.Element) -> dict[int, ET.Element]:
    """
    Best-effort {page -> paragraph} using explicit breaks and lastRenderedPageBreak.
    We anchor the NEXT paragraph after a break (pending anchors), not the paragraph
    that contains the break.
    """
    paragraphs = _all_paragraphs_in_order(root)
    anchors: dict[int, ET.Element] = {}
    page = 1
    pending: list[int] = []

    for p in paragraphs:
        if pending:
            for pg in pending:
                anchors.setdefault(pg, p)
            pending.clear()

        anchors.setdefault(page, p)

        rendered_breaks = p.findall(".//w:lastRenderedPageBreak", NS)
        hard_breaks = [
            br
            for br in p.findall(".//w:br", NS)
            if str(br.get(f"{{{W_NS}}}type") or "").lower() == "page"
        ]
        breaks_count = len(rendered_breaks) + len(hard_breaks)
        if breaks_count:
            for _ in range(breaks_count):
                page += 1
                pending.append(page)

    return anchors


def _is_in_table(p: ET.Element) -> bool:
    cur = p
    while cur is not None:
        if cur.tag == f"{{{W_NS}}}tbl":
            return True
        cur = getattr(cur, "getparent", lambda: None)()  # type: ignore[attr-defined]
    # xml.etree doesn't support getparent(); fallback to manual scan later.
    return False


def _paragraph_is_inside_table(root: ET.Element, p: ET.Element) -> bool:
    # xml.etree has no parent pointers; detect by scanning tables.
    for tbl in root.findall(".//w:tbl", NS):
        for tp in tbl.findall(".//w:p", NS):
            if tp is p:
                return True
    return False


def _move_anchor_out_of_table(root: ET.Element, paragraphs: list[ET.Element], p: ET.Element) -> ET.Element:
    """
    If the anchor paragraph is inside a table, move the anchor to the first paragraph
    after that table (outside it). This is crucial because Word can't reliably position
    shapes "relative to page" when the anchor is within a table.
    """
    if not _paragraph_is_inside_table(root, p):
        return p

    # Find the table that contains p
    containing_tbl: Optional[ET.Element] = None
    for tbl in root.findall(".//w:tbl", NS):
        if any(tp is p for tp in tbl.findall(".//w:p", NS)):
            containing_tbl = tbl
            break
    if containing_tbl is None:
        return p

    # Find the first paragraph after the table in document order.
    seen_tbl = False
    for q in paragraphs:
        if not seen_tbl:
            # Heuristic: once we encounter any paragraph in the table, mark seen.
            if _paragraph_is_inside_table(root, q) and any(tp is q for tp in containing_tbl.findall(".//w:p", NS)):
                seen_tbl = True
            continue
        # after seen_tbl, pick the first paragraph outside any table
        if not _paragraph_is_inside_table(root, q):
            return q
    return p


def _append_vml_textbox(
    paragraph: ET.Element,
    *,
    shape_id: str,
    x: float,
    y: float,
    width: float,
    height: float,
    text: str,
    font_size: int,
    color_hex: str = "FF0000",
) -> None:
    r = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    pict = ET.SubElement(r, f"{{{W_NS}}}pict")

    shapetype = ET.SubElement(pict, f"{{{V_NS}}}shapetype")
    shapetype.set("id", "_x0000_t202")
    shapetype.set("coordsize", "21600,21600")
    shapetype.set(f"{{{O_NS}}}spt", "202")
    shapetype.set("path", "m,l,21600r21600,l21600,xe")
    ET.SubElement(shapetype, f"{{{V_NS}}}stroke").set("joinstyle", "miter")
    path = ET.SubElement(shapetype, f"{{{V_NS}}}path")
    path.set("gradientshapeok", "t")
    path.set(f"{{{O_NS}}}connecttype", "rect")

    shape = ET.SubElement(pict, f"{{{V_NS}}}shape")
    shape.set("id", shape_id)
    shape.set(f"{{{O_NS}}}spid", f"_x0000_s{100000 + (abs(hash(shape_id)) % 899999)}")
    shape.set(f"{{{O_NS}}}allowincell", "f")
    shape.set("type", "#_x0000_t202")
    shape.set(
        "style",
        (
            "position:absolute;left:0;text-align:left;"
            f"margin-left:{x:g}pt;margin-top:{y:g}pt;"
            f"width:{width:g}pt;height:{height:g}pt;"
            "z-index:251659264;mso-wrap-style:none;"
            "mso-position-horizontal:absolute;"
            "mso-position-vertical:absolute;"
            "mso-position-horizontal-relative:page;"
            "mso-position-vertical-relative:page"
        ),
    )
    shape.set("filled", "f")
    shape.set("stroked", "f")

    textbox = ET.SubElement(shape, f"{{{V_NS}}}textbox")
    textbox.set("inset", "0,0,0,0")
    content = ET.SubElement(textbox, f"{{{W_NS}}}txbxContent")
    p = ET.SubElement(content, f"{{{W_NS}}}p")
    ppr = ET.SubElement(p, f"{{{W_NS}}}pPr")
    spacing = ET.SubElement(ppr, f"{{{W_NS}}}spacing")
    spacing.set(f"{{{W_NS}}}before", "0")
    spacing.set(f"{{{W_NS}}}after", "0")
    rpr = ET.SubElement(ppr, f"{{{W_NS}}}rPr")
    p_color = ET.SubElement(rpr, f"{{{W_NS}}}color")
    p_color.set(f"{{{W_NS}}}val", color_hex)
    p_sz = ET.SubElement(rpr, f"{{{W_NS}}}sz")
    p_sz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    p_szcs = ET.SubElement(rpr, f"{{{W_NS}}}szCs")
    p_szcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))

    rr = ET.SubElement(p, f"{{{W_NS}}}r")
    rrpr = ET.SubElement(rr, f"{{{W_NS}}}rPr")
    rr_color = ET.SubElement(rrpr, f"{{{W_NS}}}color")
    rr_color.set(f"{{{W_NS}}}val", color_hex)
    rr_sz = ET.SubElement(rrpr, f"{{{W_NS}}}sz")
    rr_sz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    rr_szcs = ET.SubElement(rrpr, f"{{{W_NS}}}szCs")
    rr_szcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    # Insert wrapped lines with explicit <w:br/> to keep height predictable.
    for i, line in enumerate(_wrap_text_lines(text, width, font_size)):
        if i:
            ET.SubElement(rr, f"{{{W_NS}}}br")
        t = ET.SubElement(rr, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line

    wrap = ET.SubElement(shape, f"{{{W10_NS}}}wrap")
    wrap.set("anchorx", "page")
    wrap.set("anchory", "page")
    ET.SubElement(shape, f"{{{W10_NS}}}anchorlock")


def _load_rows(json_path: Path) -> list[Row]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SystemExit("JSON non valido: atteso un dict con chiave 'rows' (lista).")

    out: list[Row] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        text = _answer(r)
        bb = _bbox(r)
        if not text or not bb:
            continue
        page = _page(r)
        x0, y0, x1, y1 = _bbox_as_rect(bb)
        w = max(5.0, x1 - x0)
        h = max(6.0, y1 - y0)
        out.append(Row(page=page, x0=float(x0), y0=float(y0), w=float(w), h=float(h), text=text))

    out.sort(key=lambda rr: (rr.page, rr.y0, rr.x0))
    if not out:
        raise SystemExit("Nessun campo valido nel JSON (answer + bbox).")
    return out


def compile_overlay_xml_only(*, src_docx: Path, json_path: Path, out_docx: Path) -> dict[str, Any]:
    out_docx.parent.mkdir(parents=True, exist_ok=True)

    # Need both parsed Row list and original json rows for label-based anchoring.
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    raw_rows = (raw.get("rows") or []) if isinstance(raw, dict) else []
    if not isinstance(raw_rows, list):
        raw_rows = []
    rows = _load_rows(json_path)

    # Work on a copy to keep src intact.
    tmp_src = out_docx.with_suffix(".template_copy.docx")
    shutil.copy2(src_docx, tmp_src)

    applied = 0
    with zipfile.ZipFile(tmp_src, "r") as z:
        doc_xml = z.read("word/document.xml")

    _register_docx_namespaces(doc_xml)
    root = ET.fromstring(doc_xml)
    root.set(f"{{{MC_NS}}}Ignorable", "w14")

    page_w = _page_width(root)
    fallback_anchor = _first_body_paragraph(root)
    # Base anchors from breaks (covers pages even when no labels match),
    # then refine using label matches when available.
    anchors_by_page = _paragraph_page_anchors(root)
    anchors_by_page.update(_build_page_anchors_from_labels(root, raw_rows))
    paragraphs = _all_paragraphs_in_order(root)
    # Ensure we never anchor inside tables (Word positions become table-relative and drift).
    for pg, p in list(anchors_by_page.items()):
        anchors_by_page[pg] = _move_anchor_out_of_table(root, paragraphs, p)
    fallback_anchor = _move_anchor_out_of_table(root, paragraphs, fallback_anchor)

    for i, r in enumerate(rows):
        page = max(1, int(r.page))
        x = float(r.x0)
        y = float(r.y0)
        width = min(max(5.0, float(r.w)), max(5.0, float(page_w) - x - 1.0))
        height = max(6.0, float(r.h))
        fs = _fit_font_size_to_box(r.text, width, height, start_fs=10)

        _append_vml_textbox(
            anchors_by_page.get(page) if anchors_by_page.get(page) is not None else fallback_anchor,
            shape_id=f"campo_xml_{page}_{i}",
            x=x,
            y=y,
            width=width,
            height=height,
            text=r.text,
            font_size=fs,
            color_hex="FF0000",
        )
        applied += 1

    if applied <= 0:
        raise RuntimeError("Nessun campo compilato: nessuna textbox inserita nel DOCX.")

    with zipfile.ZipFile(tmp_src, "r") as z_in, zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
        for info in z_in.infolist():
            if info.filename == "word/document.xml":
                z_out.writestr(info, ET.tostring(root, encoding="utf-8", xml_declaration=True))
            else:
                z_out.writestr(info, z_in.read(info.filename))

    try:
        tmp_src.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass

    print(f"[WRITER] metodo=XML rows={applied} out={out_docx}", flush=True)
    return {"output_path": str(out_docx), "applied": applied, "method": "XML"}

def write_docx_from_mapping(source_docx: str | Path, mapping_json: str | Path, out_docx: str | Path) -> dict[str, Any]:
    src = Path(_win_to_wsl_path(str(source_docx))).resolve()
    js = Path(_win_to_wsl_path(str(mapping_json))).resolve()
    out = Path(_win_to_wsl_path(str(out_docx))).resolve()
    res = compile_overlay_xml_only(src_docx=src, json_path=js, out_docx=out)
    # compatibilità con m2_pipeline/main.py che legge output_path e replaced_count
    applied = int(res.get("applied", 0) or 0) if isinstance(res, dict) else 0
    return {"output_path": str(out), "replaced_count": applied}


def write_docx_preview_from_answers_json(
    source_docx: str | Path,
    mapping_json: str | Path,
    out_docx: str | Path,
    *,
    color_hex: str = "0000FF",
) -> dict[str, Any]:
    # `color_hex` ignorato in XML-only
    return write_docx_from_mapping(source_docx, mapping_json, out_docx)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--word-in", required=True, help="DOCX input")
    ap.add_argument("--json", required=True, help="campo_valore_provvisorio.json")
    ap.add_argument("--word-out", required=True, help="DOCX output")
    args = ap.parse_args(argv)

    src = Path(_win_to_wsl_path(args.word_in)).resolve()
    js = Path(_win_to_wsl_path(args.json)).resolve()
    out = Path(_win_to_wsl_path(args.word_out)).resolve()

    if not src.exists():
        raise SystemExit(f"DOCX non trovato: {src}")
    if src.suffix.lower() != ".docx":
        raise SystemExit(f"Serve un .docx: {src}")
    if not js.exists():
        raise SystemExit(f"JSON non trovato: {js}")

    compile_overlay_xml_only(src_docx=src, json_path=js, out_docx=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
