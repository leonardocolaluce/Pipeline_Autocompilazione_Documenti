from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import xml.etree.ElementTree as ET

# Manual test inputs. If CLI args are omitted, main() uses these paths and
# writes the output into C:\Users\39334\Desktop\hh\review_render.
WORD_INPUT_PATH = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_2\Millestone_2.2\pipeline_4\new\file_sample\All E - Dichiarazione-conflitto-interessi.docx"
JSON_INPUT_PATH = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_2\Millestone_2.2\pipeline_4\new\file_sample\ALL_E_campo_valore_provvisorio.json"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W_NS, "w14": W14_NS, "w15": W15_NS, "v": V_NS, "o": O_NS, "w10": W10_NS, "mc": MC_NS}


@dataclass(frozen=True)
class OverlayItem:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    item_id: str
    item_type: str
    label: str


@dataclass(frozen=True)
class DocxProfile:
    prefixes: Tuple[str, ...]
    ignorable: str
    table_count: int
    page_break_count: int
    strategy: str
    field_items: int
    skipped_table_items: int
    skipped_checkbox_items: int


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _ps_quote(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _convert_doc_to_docx_with_word(src_doc: Path, out_dir: Path) -> Path:
    if src_doc.suffix.lower() == ".docx":
        return src_doc
    if src_doc.suffix.lower() != ".doc":
        raise SystemExit(f"Serve un .docx o .doc: {src_doc}")

    out_dir.mkdir(parents=True, exist_ok=True)
    converted = out_dir / f"{src_doc.stem}__converted.docx"
    script = (
        f"$src={_ps_quote(src_doc)}; "
        f"$out={_ps_quote(converted)}; "
        "$word=$null; $doc=$null; "
        "try { "
        "$word=New-Object -ComObject Word.Application; "
        "$word.Visible=$false; $word.DisplayAlerts=0; "
        "$doc=$word.Documents.Open($src, $false, $true); "
        "$doc.SaveAs2($out, 16); "
        "Write-Output $out "
        "} finally { "
        "if($doc){$doc.Close($false)}; "
        "if($word){$word.Quit()} "
        "}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not converted.exists():
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"Conversione .doc -> .docx fallita: {src_doc}\n{msg}")
    print(f"[CONVERT] doc_to_docx in={src_doc} out={converted}", flush=True)
    return converted


def _convert_doc_to_docx(src_doc: Path, out_dir: Path) -> Path:
    if src_doc.suffix.lower() == ".docx":
        return src_doc
    if src_doc.suffix.lower() != ".doc":
        raise SystemExit(f"Serve un .docx o .doc: {src_doc}")

    out_dir.mkdir(parents=True, exist_ok=True)
    converted = out_dir / f"{src_doc.stem}__converted.docx"

    # Prefer LibreOffice because it is available on Linux too. Word COM remains
    # a Windows fallback for legacy .doc files that LibreOffice cannot convert.
    soffice_candidates = [
        "soffice",
        "libreoffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
    ]
    for soffice in soffice_candidates:
        try:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    "--nodefault",
                    "--nolockcheck",
                    "--convert-to",
                    'docx:"Office Open XML Text"',
                    "--outdir",
                    str(out_dir),
                    str(src_doc),
                ],
                text=True,
                capture_output=True,
                timeout=90,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            libre_out = out_dir / f"{src_doc.stem}.docx"
            if libre_out.exists():
                if converted.exists():
                    converted.unlink()
                libre_out.replace(converted)
                print(f"[CONVERT] doc_to_docx via=LibreOffice in={src_doc} out={converted}", flush=True)
                return converted

    if os.name == "nt":
        return _convert_doc_to_docx_with_word(src_doc, out_dir)

    raise SystemExit(f"Conversione .doc -> .docx fallita: LibreOffice non disponibile o file non convertibile: {src_doc}")


def _register_docx_namespaces(doc_xml: bytes) -> None:
    # Preserve whatever namespaces are already in the document. ElementTree
    # otherwise serializes many Word namespaces as ns2/ns3, while existing
    # mc:Ignorable values may still reference the original prefixes.
    try:
        for _event, ns in ET.iterparse(io.BytesIO(doc_xml), events=("start-ns",)):
            pref, uri = ns
            if pref:
                try:
                    ET.register_namespace(pref, uri)
                except Exception:
                    pass
    except Exception:
        pass
    ET.register_namespace("w", W_NS)
    ET.register_namespace("w14", W14_NS)
    ET.register_namespace("w15", W15_NS)
    ET.register_namespace("v", V_NS)
    ET.register_namespace("o", O_NS)
    ET.register_namespace("w10", W10_NS)
    ET.register_namespace("mc", MC_NS)


def _collect_docx_namespaces(doc_xml: bytes) -> List[Tuple[str, str]]:
    namespaces: List[Tuple[str, str]] = []
    seen = set()
    try:
        for _event, ns in ET.iterparse(io.BytesIO(doc_xml), events=("start-ns",)):
            pref, uri = ns
            if not pref:
                continue
            key = (pref, uri)
            if key in seen:
                continue
            seen.add(key)
            namespaces.append(key)
    except Exception:
        pass
    return namespaces


def _serialize_preserving_root_namespaces(root: ET.Element, original_namespaces: List[Tuple[str, str]]) -> bytes:
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    m = re.search(r"<w:document\b([^>]*)>", xml)
    if not m:
        return xml.encode("utf-8")

    root_attrs = m.group(1)
    missing = []
    for pref, uri in original_namespaces:
        if f"xmlns:{pref}=" in root_attrs:
            continue
        missing.append(f' xmlns:{pref}="{uri}"')
    if not missing:
        return xml.encode("utf-8")

    insert_at = m.end() - 1
    xml = xml[:insert_at] + "".join(missing) + xml[insert_at:]
    return xml.encode("utf-8")


def _docx_profile(root: ET.Element, original_namespaces: List[Tuple[str, str]], raw_rows: List[Dict[str, Any]]) -> DocxProfile:
    prefixes = tuple(pref for pref, _uri in original_namespaces)
    ignorable = str(root.get(f"{{{MC_NS}}}Ignorable") or "")
    table_count = len(root.findall(".//w:tbl", NS))
    page_break_count = len(root.findall(".//w:lastRenderedPageBreak", NS)) + len(
        [
            br
            for br in root.findall(".//w:br", NS)
            if str(br.get(f"{{{W_NS}}}type") or "").lower() == "page"
        ]
    )
    field_items = 0
    skipped_table_items = 0
    skipped_checkbox_items = 0
    pages = set()
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        answer = str(r.get("answer") or "").strip()
        if not answer or answer == "N/D":
            continue
        item_id = str(r.get("item_id") or "")
        item_type = str(r.get("item_type") or "")
        if item_id.startswith("table:") or item_type == "table":
            skipped_table_items += 1
            continue
        if item_id.startswith("checkbox:") or item_type == "checkbox" or isinstance(r.get("answer"), bool):
            skipped_checkbox_items += 1
            continue
        field_items += 1
        pages.add(max(1, _int(r.get("page") or 1, 1)))

    strategy = _select_field_overlay_strategy(
        prefixes=prefixes,
        table_count=table_count,
        page_break_count=page_break_count,
        field_pages=pages,
    )
    return DocxProfile(
        prefixes=prefixes,
        ignorable=ignorable,
        table_count=table_count,
        page_break_count=page_break_count,
        strategy=strategy,
        field_items=field_items,
        skipped_table_items=skipped_table_items,
        skipped_checkbox_items=skipped_checkbox_items,
    )


def _select_field_overlay_strategy(
    *,
    prefixes: Tuple[str, ...],
    table_count: int,
    page_break_count: int,
    field_pages: Iterable[int],
) -> str:
    """
    Select how normal fields are anchored. This does not write tables.

    - pagebreak: use explicit/last-rendered page breaks.
    - label: refine page anchors with labels from JSON, useful for multi-page DOCX
      where page breaks are unreliable or content is inside tables.
    - root: anchor everything to the first paragraph; useful for single-page,
      drawing-heavy forms where page-relative VML behaves better.
    """
    pages = set(field_pages)
    if len(pages) <= 1 and table_count == 0 and page_break_count <= 1:
        return "root"
    # Legacy .doc converted to .docx can contain a couple of layout tables but
    # still has reliable page breaks. In that case label anchoring may jump to
    # a later similar paragraph; keep page-break anchoring for the single field page.
    if len(pages) == 1 and table_count <= 2 and page_break_count >= 1:
        return "pagebreak"
    if table_count or len(pages) > max(1, page_break_count + 1):
        return "label"
    if any(p.startswith("w16") for p in prefixes) and len(pages) > 1:
        return "label"
    return "pagebreak"


def _namespace_is_used(root: ET.Element, uri: str) -> bool:
    marker = f"{{{uri}}}"
    for el in root.iter():
        if el.tag.startswith(marker):
            return True
        for name in el.attrib:
            if name.startswith(marker):
                return True
    return False


def _clean_ignorable_namespaces(root: ET.Element) -> None:
    ign_attr = f"{{{MC_NS}}}Ignorable"
    ignorable = str(root.get(ign_attr) or "").split()
    if not ignorable:
        return

    known = {
        "w14": W14_NS,
        "w15": W15_NS,
    }
    kept = []
    for prefix in ignorable:
        uri = known.get(prefix)
        if uri is None or _namespace_is_used(root, uri):
            kept.append(prefix)
    if kept:
        root.set(ign_attr, " ".join(kept))
    else:
        root.attrib.pop(ign_attr, None)


def _bbox_as_rect(bb: List[Any]) -> Tuple[float, float, float, float]:
    """
    Normalize bbox to (x0,y0,x1,y1) in points.
    Supports [x0,y0,x1,y1] and [x0,y0,w,h] (heuristic).
    """
    x0, y0, a, b = (_num(bb[0]), _num(bb[1]), _num(bb[2]), _num(bb[3]))
    if a >= 0 and b >= 0 and b <= 80 and y0 >= 100 and a <= 700:
        return x0, y0, x0 + a, y0 + b
    nx0, nx1 = (x0, a) if x0 <= a else (a, x0)
    ny0, ny1 = (y0, b) if y0 <= b else (b, y0)
    return nx0, ny0, nx1, ny1


def _load_overlay_items(json_path: Path) -> List[OverlayItem]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit("JSON non valido: atteso dict con chiave 'rows' oppure lista.")

    items: List[OverlayItem] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        item_id = str(r.get("item_id") or "")
        item_type = str(r.get("item_type") or "")
        if item_id.startswith("table:") or item_type == "table":
            continue
        if item_id.startswith("checkbox:") or item_type == "checkbox" or isinstance(r.get("answer"), bool):
            continue
        text = str(r.get("answer") or "").strip()
        if not text or text == "N/D":
            continue
        bb = r.get("bbox") or r.get("marker_bbox") or r.get("checkbox_bbox")
        if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
            continue
        page = max(1, _int(r.get("page") or 1, 1))
        x0, y0, x1, y1 = _bbox_as_rect(list(bb))
        items.append(
            OverlayItem(
                page=page,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                text=text,
                item_id=item_id,
                item_type=item_type,
                label=str(r.get("label") or ""),
            )
        )

    items.sort(key=lambda it: (it.page, it.y0, it.x0, it.item_id))
    if not items:
        raise SystemExit("Nessun campo compilato (answer+bbox) nel JSON.")
    return items


def _page_width_pt(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 595.0
    return _num(pg_sz.get(f"{{{W_NS}}}w"), 11900.0) / 20.0


def _page_height_pt(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 842.0
    return _num(pg_sz.get(f"{{{W_NS}}}h"), 16840.0) / 20.0


def _page_margins_pt(root: ET.Element) -> Tuple[float, float, float, float]:
    """
    Return (left, top, right, bottom) margins in points.
    Word stores margins in twips (1/20 pt).
    """
    pg_mar = root.find(".//w:sectPr/w:pgMar", NS)
    if pg_mar is None:
        return 0.0, 0.0, 0.0, 0.0
    left = _num(pg_mar.get(f"{{{W_NS}}}left"), 0.0) / 20.0
    top = _num(pg_mar.get(f"{{{W_NS}}}top"), 0.0) / 20.0
    right = _num(pg_mar.get(f"{{{W_NS}}}right"), 0.0) / 20.0
    bottom = _num(pg_mar.get(f"{{{W_NS}}}bottom"), 0.0) / 20.0
    return left, top, right, bottom


def _infer_coord_mode(items: List[OverlayItem], *, page_w: float, page_h: float, mar_l: float, mar_t: float) -> str:
    """
    Heuristic to guess whether overlay coordinates are page-relative or margin-relative.

    - If many items are very near the top/left (e.g., y0 < ~top_margin*0.7), it's likely PAGE coords.
    - If many items start near (0,0) and never use the margin area, it's likely MARGIN coords.
    """
    if not items:
        return "page"
    xs = [it.x0 for it in items]
    ys = [it.y0 for it in items]
    near_top = sum(1 for y in ys if y < max(12.0, mar_t * 0.7))
    near_left = sum(1 for x in xs if x < max(12.0, mar_l * 0.7))
    # If coords regularly go into the physical margin area, they must be page-relative.
    if near_top + near_left >= max(3, int(0.2 * len(items))):
        return "page"
    # If everything is comfortably inside the printable area and some coordinates are tiny,
    # prefer margin-relative (common when coords come from a rendered "content box").
    if (min(xs) >= 0 and min(ys) >= 0) and (min(xs) < 20 or min(ys) < 20):
        return "margin"
    return "page"


def _wrap_text_lines(text: str, width_pt: float, font_size: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return [text]
    max_chars = max(1, int(width_pt / max(1.0, font_size * 0.55)))
    lines: List[str] = []
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


def _fit_font_size(text: str, width_pt: float, height_pt: float, start_fs: int = 10) -> int:
    fs = max(6, int(start_fs))
    height_pt = max(6.0, float(height_pt))
    while fs > 6:
        lines = _wrap_text_lines(text, width_pt, fs)
        needed = max(1, len(lines)) * (fs * 1.18)
        if needed <= height_pt:
            break
        fs -= 1
    return max(6, fs)


def _fit_single_line_font_size(text: str, width_pt: float, start_fs: int = 10, min_fs: int = 4) -> int:
    text_len = max(1, len(text or ""))
    fs = int(start_fs)
    while fs > min_fs and (text_len * fs * 0.55 + 2.0) > width_pt:
        fs -= 1
    return max(min_fs, fs)


def _paragraph_page_anchors(root: ET.Element) -> Dict[int, ET.Element]:
    """
    Best-effort {page -> paragraph} based on w:lastRenderedPageBreak.
    Rule: page N anchor = first paragraph AFTER the break that ended page N-1.
    Prefer anchors outside tables (Word positions relative-to-page break when anchor is inside table).
    """
    body = root.find("w:body", NS)
    if body is None:
        return {}
    paragraphs = body.findall(".//w:p", NS)

    parent_map: Dict[ET.Element, ET.Element] = {child: parent for parent in root.iter() for child in parent}

    def in_table(el: ET.Element) -> bool:
        cur = el
        while cur in parent_map:
            cur = parent_map[cur]
            if cur.tag == f"{{{W_NS}}}tbl":
                return True
        return False

    anchors: Dict[int, ET.Element] = {}
    first_any: Dict[int, ET.Element] = {}
    page = 1
    pending: List[int] = []

    for p in paragraphs:
        first_any.setdefault(page, p)

        if pending and (not in_table(p)):
            for pg in pending:
                anchors.setdefault(pg, p)
            pending.clear()

        if (page not in anchors) and (not in_table(p)):
            anchors[page] = p

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

    # Fill missing anchors with first paragraph seen on that page (even if in table).
    for pg, p in first_any.items():
        anchors.setdefault(pg, p)
    return anchors


def _first_body_paragraph(root: ET.Element) -> Optional[ET.Element]:
    body = root.find("w:body", NS)
    if body is not None:
        p = body.find("./w:p", NS)
        if p is not None:
            return p
    return root.find(".//w:p", NS)


def _field_anchors_by_strategy(root: ET.Element, raw_rows: List[Dict[str, Any]], strategy: str) -> Dict[int, ET.Element]:
    """
    Build anchors for normal-field overlay only. Tables are never written here.
    """
    if strategy == "root":
        first = _first_body_paragraph(root)
        if first is None:
            return {}
        pages = {
            max(1, _int(r.get("page") or 1, 1))
            for r in raw_rows
            if isinstance(r, dict)
            and str(r.get("answer") or "").strip()
            and not str(r.get("item_id") or "").startswith("table:")
            and not str(r.get("item_id") or "").startswith("checkbox:")
            and str(r.get("item_type") or "") not in {"table", "checkbox"}
        }
        return {page: first for page in pages or {1}}

    anchors = _paragraph_page_anchors(root)
    if strategy == "label":
        anchors.update(_build_page_anchors_from_labels(root, raw_rows))
    return anchors


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").split())


def _all_paragraphs_in_order(root: ET.Element) -> List[ET.Element]:
    body = root.find("w:body", NS)
    if body is None:
        return root.findall(".//w:p", NS)
    return body.findall(".//w:p", NS)


def _paragraph_text(p: ET.Element) -> str:
    texts = [t.text for t in p.findall(".//w:t", NS) if t.text]
    return _normalize_ws("".join(texts))


def _raw_bbox(row: Dict[str, Any]) -> Optional[List[Any]]:
    value = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 4 else None


def _build_page_anchors_from_labels(root: ET.Element, raw_rows: List[Dict[str, Any]]) -> Dict[int, ET.Element]:
    paragraphs = _all_paragraphs_in_order(root)
    para_texts = [_paragraph_text(p).lower() for p in paragraphs]

    by_page: Dict[int, List[Dict[str, Any]]] = {}
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "").strip()
        if not label or re.fullmatch(r"col_\d+", label.strip().lower()):
            continue
        if not _raw_bbox(r):
            continue
        by_page.setdefault(max(1, _int(r.get("page") or 1, 1)), []).append(r)

    anchors: Dict[int, ET.Element] = {}
    cursor = 0
    for page in sorted(by_page):
        candidates: List[Tuple[int, float, str]] = []
        for r in by_page[page]:
            label = _normalize_ws(str(r.get("label") or ""))
            if len(label) < 8:
                continue
            bb = _raw_bbox(r) or [0, 0, 0, 0]
            candidates.append((len(label), _num(bb[1]), label.lower()))
        candidates.sort(key=lambda t: (-t[0], t[1]))

        found_idx: Optional[int] = None
        for _length, _y0, needle in candidates[:10]:
            for i in range(cursor, len(para_texts)):
                if needle and needle in para_texts[i]:
                    found_idx = i
                    break
            if found_idx is not None:
                break
        if found_idx is not None:
            anchors[page] = paragraphs[found_idx]
            cursor = found_idx
    return anchors


def _paragraph_is_inside_table(root: ET.Element, p: ET.Element) -> bool:
    for tbl in root.findall(".//w:tbl", NS):
        for tp in tbl.findall(".//w:p", NS):
            if tp is p:
                return True
    return False


def _move_anchor_out_of_table(root: ET.Element, paragraphs: List[ET.Element], p: ET.Element) -> ET.Element:
    if not _paragraph_is_inside_table(root, p):
        return p

    containing_tbl: Optional[ET.Element] = None
    for tbl in root.findall(".//w:tbl", NS):
        if any(tp is p for tp in tbl.findall(".//w:p", NS)):
            containing_tbl = tbl
            break
    if containing_tbl is None:
        return p

    seen_tbl = False
    for q in paragraphs:
        if not seen_tbl:
            if any(tp is q for tp in containing_tbl.findall(".//w:p", NS)):
                seen_tbl = True
            continue
        if not _paragraph_is_inside_table(root, q):
            return q
    return p


def _append_vml_textbox(
    paragraph: ET.Element,
    *,
    shape_id: str,
    x_pt: float,
    y_pt: float,
    w_pt: float,
    h_pt: float,
    text: str,
    font_size: int,
    font_name: str = "Times New Roman",
    color_hex: str = "FF0000",
    inset_top_pt: float = 0.0,
    relative_to: str = "page",
) -> None:
    r = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    pict = ET.SubElement(r, f"{{{W_NS}}}pict")

    shape_type_id = f"{shape_id}_type"
    shapetype = ET.SubElement(pict, f"{{{V_NS}}}shapetype")
    shapetype.set("id", shape_type_id)
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
    shape.set("type", f"#{shape_type_id}")
    shape.set(
        "style",
        (
            "position:absolute;text-align:left;"
            f"left:{x_pt:g}pt;top:{y_pt:g}pt;"
            f"width:{w_pt:g}pt;height:{h_pt:g}pt;"
            "z-index:251659264;"
            "mso-wrap-style:none;"
            f"mso-position-horizontal-relative:{relative_to};"
            f"mso-position-vertical-relative:{relative_to}"
        ),
    )
    shape.set("filled", "f")
    shape.set("stroked", "f")

    textbox = ET.SubElement(shape, f"{{{V_NS}}}textbox")
    # VML textbox inset is internal padding. Some templates render text "too high"
    # relative to underlines; a small top inset stabilizes baseline visually.
    if inset_top_pt and inset_top_pt > 0:
        textbox.set("inset", f"0,{inset_top_pt:g},0,0")
    else:
        textbox.set("inset", "0,0,0,0")
    content = ET.SubElement(textbox, f"{{{W_NS}}}txbxContent")
    p = ET.SubElement(content, f"{{{W_NS}}}p")
    ppr = ET.SubElement(p, f"{{{W_NS}}}pPr")
    spacing = ET.SubElement(ppr, f"{{{W_NS}}}spacing")
    spacing.set(f"{{{W_NS}}}before", "0")
    spacing.set(f"{{{W_NS}}}after", "0")

    # Paragraph-default run properties (some renderers ignore these inside VML textboxes,
    # so we also set the same properties on the actual run below).
    prpr = ET.SubElement(ppr, f"{{{W_NS}}}rPr")
    pcolor = ET.SubElement(prpr, f"{{{W_NS}}}color")
    pcolor.set(f"{{{W_NS}}}val", color_hex)
    psz = ET.SubElement(prpr, f"{{{W_NS}}}sz")
    psz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    pszcs = ET.SubElement(prpr, f"{{{W_NS}}}szCs")
    pszcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))

    rr = ET.SubElement(p, f"{{{W_NS}}}r")
    rrpr = ET.SubElement(rr, f"{{{W_NS}}}rPr")
    rrf = ET.SubElement(rrpr, f"{{{W_NS}}}rFonts")
    rrf.set(f"{{{W_NS}}}ascii", font_name)
    rrf.set(f"{{{W_NS}}}hAnsi", font_name)
    rrf.set(f"{{{W_NS}}}cs", font_name)
    rrcolor = ET.SubElement(rrpr, f"{{{W_NS}}}color")
    rrcolor.set(f"{{{W_NS}}}val", color_hex)
    rrsz = ET.SubElement(rrpr, f"{{{W_NS}}}sz")
    rrsz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    rrszcs = ET.SubElement(rrpr, f"{{{W_NS}}}szCs")
    rrszcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    for i, line in enumerate(_wrap_text_lines(text, w_pt, int(font_size))):
        if i:
            ET.SubElement(rr, f"{{{W_NS}}}br")
        t = ET.SubElement(rr, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line

    wrap = ET.SubElement(shape, f"{{{W10_NS}}}wrap")
    wrap.set("anchorx", relative_to)
    wrap.set("anchory", relative_to)
    ET.SubElement(shape, f"{{{W10_NS}}}anchorlock")


def _append_vml_textbox_complex(
    paragraph: ET.Element,
    *,
    shape_id: str,
    x_pt: float,
    y_pt: float,
    w_pt: float,
    h_pt: float,
    text: str,
    font_size: int,
    color_hex: str = "FF0000",
) -> None:
    r = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    pict = ET.SubElement(r, f"{{{W_NS}}}pict")

    shape_type_id = f"{shape_id}_type"
    shapetype = ET.SubElement(pict, f"{{{V_NS}}}shapetype")
    shapetype.set("id", shape_type_id)
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
    shape.set("type", f"#{shape_type_id}")
    shape.set(
        "style",
        (
            "position:absolute;left:0;text-align:left;"
            f"margin-left:{x_pt:g}pt;margin-top:{y_pt:g}pt;"
            f"width:{w_pt:g}pt;height:{h_pt:g}pt;"
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
    prpr = ET.SubElement(ppr, f"{{{W_NS}}}rPr")
    pcolor = ET.SubElement(prpr, f"{{{W_NS}}}color")
    pcolor.set(f"{{{W_NS}}}val", color_hex)
    psz = ET.SubElement(prpr, f"{{{W_NS}}}sz")
    psz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    pszcs = ET.SubElement(prpr, f"{{{W_NS}}}szCs")
    pszcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))

    rr = ET.SubElement(p, f"{{{W_NS}}}r")
    rrpr = ET.SubElement(rr, f"{{{W_NS}}}rPr")
    rrcolor = ET.SubElement(rrpr, f"{{{W_NS}}}color")
    rrcolor.set(f"{{{W_NS}}}val", color_hex)
    rrsz = ET.SubElement(rrpr, f"{{{W_NS}}}sz")
    rrsz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    rrszcs = ET.SubElement(rrpr, f"{{{W_NS}}}szCs")
    rrszcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    for i, line in enumerate(_wrap_text_lines(text, w_pt, int(font_size))):
        if i:
            ET.SubElement(rr, f"{{{W_NS}}}br")
        t = ET.SubElement(rr, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line

    wrap = ET.SubElement(shape, f"{{{W10_NS}}}wrap")
    wrap.set("anchorx", "page")
    wrap.set("anchory", "page")
    ET.SubElement(shape, f"{{{W10_NS}}}anchorlock")


def compile_overlay_docx(
    *,
    src_docx: Path,
    json_path: Path,
    out_docx: Path,
    coords: str = "auto",
    strategy_override: str = "auto",
) -> None:
    items = _load_overlay_items(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    raw_rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(raw_rows, list):
        raw_rows = []

    with zipfile.ZipFile(src_docx, "r") as z:
        doc_xml = z.read("word/document.xml")

    _register_docx_namespaces(doc_xml)
    original_namespaces = _collect_docx_namespaces(doc_xml)
    root = ET.fromstring(doc_xml)
    profile = _docx_profile(root, original_namespaces, raw_rows)
    legacy_strategy_map = {
        "standard": "pagebreak",
        "complex": "label",
    }
    if strategy_override in legacy_strategy_map:
        strategy_override = legacy_strategy_map[strategy_override]
    if strategy_override in {"root", "pagebreak", "label"}:
        profile = DocxProfile(
            prefixes=profile.prefixes,
            ignorable=profile.ignorable,
            table_count=profile.table_count,
            page_break_count=profile.page_break_count,
            strategy=strategy_override,
            field_items=profile.field_items,
            skipped_table_items=profile.skipped_table_items,
            skipped_checkbox_items=profile.skipped_checkbox_items,
        )
    print(
        "[DOCX_PROFILE] "
        f"strategy={profile.strategy} prefixes={','.join(profile.prefixes)} "
        f"ignorable='{profile.ignorable}' tables={profile.table_count} page_breaks={profile.page_break_count} "
        f"fields={profile.field_items} skipped_tables={profile.skipped_table_items} skipped_checkbox={profile.skipped_checkbox_items}",
        flush=True,
    )

    anchors = _field_anchors_by_strategy(root, raw_rows, profile.strategy)
    if profile.strategy == "label":
        anchors.update(_build_page_anchors_from_labels(root, raw_rows))
        paragraphs = _all_paragraphs_in_order(root)
        for pg, p in list(anchors.items()):
            anchors[pg] = _move_anchor_out_of_table(root, paragraphs, p)
    page_width = _page_width_pt(root)
    page_height = _page_height_pt(root)
    mar_l, mar_t, mar_r, mar_b = _page_margins_pt(root)

    coord_mode = (coords or "").strip().lower()
    if coord_mode not in {"page", "margin", "auto", ""}:
        coord_mode = "auto"
    if not coord_mode or coord_mode == "auto":
        env_mode = os.environ.get("WORD_OVERLAY_COORDS", "").strip().lower()
        if env_mode in {"page", "margin"}:
            coord_mode = env_mode
        else:
            coord_mode = _infer_coord_mode(items, page_w=page_width, page_h=page_height, mar_l=mar_l, mar_t=mar_t)

    # Avoid relying on Element truthiness (deprecated in recent ElementTree versions).
    fallback_anchor = anchors.get(1)
    if fallback_anchor is None:
        fallback_anchor = root.find(".//w:p", NS)
    if fallback_anchor is None:
        raise RuntimeError("DOCX non valido: nessun paragrafo per ancorare overlay.")
    if profile.strategy == "label":
        fallback_anchor = _move_anchor_out_of_table(root, _all_paragraphs_in_order(root), fallback_anchor)

    applied = 0
    compact_single_line = (
        profile.strategy == "pagebreak"
        and profile.table_count <= 2
        and profile.field_items >= 5
        and len({it.page for it in items}) == 1
    )
    for it in items:
        anchor = anchors.get(it.page) if anchors.get(it.page) is not None else fallback_anchor

        x0, y0, x1, y1 = it.x0, it.y0, it.x1, it.y1
        # Coordinate modes:
        # - page: (0,0) is top-left of physical page.
        # - margin: (0,0) is top-left of the text area inside margins.
        if coord_mode == "margin":
            x0 += mar_l
            y0 += mar_t
            x1 += mar_l
            y1 += mar_t
        box_w = max(5.0, x1 - x0)
        raw_h = max(0.0, y1 - y0)
        box_h = max(6.0, raw_h)
        w_pt = min(box_w, max(5.0, page_width - x0 - 1.0))
        h_pt = box_h

        # Font size policy:
        # Underline-style bboxes (raw_h ~ 1pt) have meaningless height; keep font 10 when it
        # fits horizontally, otherwise shrink to fit width. For real boxes, fit by width+height.
        if raw_h < 3.0:
            # Thin bboxes usually mark an underline. Keep font size fixed and
            # move the textbox up so the text sits on the line.
            max_w = max(5.0, page_width - x0 - 1.0)
            # Times/Word rendering is wider than the rough wrap heuristic in
            # short underlined slots. Give single-line overlays enough room so
            # Word/LibreOffice do not wrap and clip the second word.
            needed_w = max(5.0, len(it.text) * 7.0 + 10.0)
            w_pt = min(max_w, max(w_pt, needed_w))
            fs = 10
            h_pt = max(14.0, float(h_pt))
            y_shift = -(fs * 1.05)
        else:
            fs = _fit_font_size(it.text, w_pt, h_pt, start_fs=10)
            if compact_single_line:
                fs = min(fs, _fit_single_line_font_size(it.text, w_pt, start_fs=10, min_fs=4))
            y_shift = 0.0
        safe_id = (it.item_id or f"idx_{applied}").replace(":", "_").replace("/", "_")
        if profile.strategy == "label":
            _append_vml_textbox_complex(
                anchor,
                shape_id=f"ov_{safe_id}",
                x_pt=x0,
                y_pt=y0,
                w_pt=w_pt,
                h_pt=h_pt,
                text=it.text,
                font_size=_fit_font_size(it.text, w_pt, h_pt, start_fs=10),
                color_hex="FF0000",
            )
        else:
            _append_vml_textbox(
                anchor,
                shape_id=f"ov_{safe_id}",
                x_pt=x0,
                y_pt=y0 + y_shift,
                w_pt=w_pt,
                h_pt=h_pt,
                text=it.text,
                font_size=fs,
                color_hex="FF0000",
                inset_top_pt=0.0,
                relative_to=("margin" if coord_mode == "margin" else "page"),
            )
        applied += 1

    out_docx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_docx, "r") as z_in, zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
        for info in z_in.infolist():
            if info.filename == "word/document.xml":
                z_out.writestr(info, _serialize_preserving_root_namespaces(root, original_namespaces))
            else:
                z_out.writestr(info, z_in.read(info.filename))

    print(f"[WRITER] metodo=XML_OVERLAY applied={applied} out={out_docx}", flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Compile DOCX by overlaying answers (XML/VML only).")
    ap.add_argument("--word-in", default=WORD_INPUT_PATH, help="Input DOCX template")
    ap.add_argument("--json", default=JSON_INPUT_PATH, help="Input JSON with rows (answer+bbox+page)")
    ap.add_argument("--word-out", default="", help="Output DOCX")
    ap.add_argument(
        "--coords",
        default="auto",
        choices=["auto", "page", "margin"],
        help="Coordinate system for bbox: auto (default), page (0,0=page corner), margin (0,0=text area).",
    )
    ap.add_argument(
        "--strategy",
        default="auto",
        choices=["auto", "root", "pagebreak", "label", "standard", "complex"],
        help="Field overlay strategy: auto (default), root, pagebreak, label. standard/complex are legacy aliases.",
    )
    args = ap.parse_args(argv)

    src = Path(args.word_in).resolve()
    js = Path(args.json).resolve()
    out = Path(args.word_out).resolve() if args.word_out else Path.cwd() / "review_render" / f"{src.stem}__XML_OVERLAY.docx"

    if not src.exists():
        raise SystemExit(f"Word non trovato: {src}")
    if src.suffix.lower() not in {".docx", ".doc"}:
        raise SystemExit(f"Supporto solo .docx o .doc, non {src.suffix}: {src}")
    if not js.exists():
        raise SystemExit(f"JSON non trovato: {js}")

    src = _convert_doc_to_docx(src, out.parent)
    compile_overlay_docx(src_docx=src, json_path=js, out_docx=out, coords=args.coords, strategy_override=args.strategy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
