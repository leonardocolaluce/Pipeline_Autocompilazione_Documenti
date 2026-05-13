from __future__ import annotations

import json
import os
import re
import zipfile
import copy
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple, Any

import xml.etree.ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W_NS, "v": V_NS, "o": O_NS, "w10": W10_NS, "mc": MC_NS}

WD_GO_TO_PAGE = 1
WD_GO_TO_ABSOLUTE = 1
MSO_TEXT_ORIENTATION_HORIZONTAL = 1
WD_RELATIVE_HORIZONTAL_POSITION_PAGE = 1
WD_RELATIVE_VERTICAL_POSITION_PAGE = 1
WD_WRAP_FRONT = 3
MSO_BRING_TO_FRONT = 0
MSO_FALSE = 0

# --- PERCORSI ESPLICITI (default) ---
JSON_INPUT_PATH = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_3\pipeline_4\output\m2_output\campo_valore_provvisorio.json"
WORD_TEMPLATE_PATH = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_3\pipeline_4\new\file_sample\allegato 1-istanza di partecipazione.doc"


def _win_to_wsl_path(p: str) -> str:
    """
    Best-effort conversion for this workspace:
    C:\\Users\\... -> /mnt/c/Users/...
    """
    p = (p or "").strip()
    if not p:
        return p
    # If we're running on Windows, keep Windows paths as-is.
    if os.name == "nt":
        return p
    p = p.replace("/", "\\")
    m = re.match(r"^([A-Za-z]):\\(.*)$", p)
    if not m:
        return p.replace("\\", "/")
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _answer(row: dict[str, Any]) -> str:
    value = str(row.get("answer") or "").strip()
    return "" if value in {"", "N/D"} else value


def _bbox(row: dict[str, Any]) -> list[Any] | None:
    value = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 4 else None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _page(row: dict[str, Any]) -> int:
    try:
        return max(1, int(row.get("page") or 1))
    except Exception:
        return 1


def _filled_coordinate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    filled = []
    for row in rows:
        if not _answer(row) or not _bbox(row):
            continue
        filled.append(row)
    return sorted(
        filled,
        key=lambda r: (
            _page(r),
            _num((_bbox(r) or [0, 0, 0, 0])[1]),
            _num((_bbox(r) or [0, 0, 0, 0])[0]),
            str(r.get("item_id") or ""),
        ),
    )


def _rows_by_page(rows: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_page(row), []).append(row)
    return grouped


def _word_color_from_hex(color_hex: str) -> int:
    color_hex = color_hex.strip().lstrip("#")
    if len(color_hex) != 6:
        color_hex = "FF0000"
    return int(color_hex[4:6] + color_hex[2:4] + color_hex[0:2], 16)


def _register_docx_namespaces(doc_xml: bytes) -> None:
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


def _paragraph_page_anchors(root: ET.Element) -> dict[int, ET.Element]:
    # NOTE: `w:lastRenderedPageBreak` is not fully reliable, but it's the best we can do
    # without automating Word pagination. We make it more stable by ensuring anchors are
    # never inside tables (anchors in tables can make shapes position relative to the table).
    parent_map: dict[ET.Element, ET.Element] = {child: parent for parent in root.iter() for child in parent}

    def _is_in_table(el: ET.Element) -> bool:
        cur = el
        while cur in parent_map:
            cur = parent_map[cur]
            if cur.tag == f"{{{W_NS}}}tbl":
                return True
        return False

    body = root.find("w:body", NS)
    paragraphs = body.findall(".//w:p", NS) if body is not None else root.findall(".//w:p", NS)
    anchors: dict[int, ET.Element] = {}
    page = 1

    for p in paragraphs:
        if page not in anchors and not _is_in_table(p):
            anchors[page] = p
        breaks = p.findall(".//w:lastRenderedPageBreak", NS)
        # Hard page breaks also help when present.
        hard_breaks = [
            br for br in p.findall(".//w:br", NS)
            if str(br.get(f"{{{W_NS}}}type") or "").lower() == "page"
        ]
        breaks_count = len(breaks) + len(hard_breaks)
        if breaks_count:
            page += breaks_count
            if page not in anchors and not _is_in_table(p):
                anchors[page] = p

    return anchors


def _page_overlay_offset(root: ET.Element, page: int) -> tuple[float, float]:
    # The bbox coordinates we use are already in page coordinates (points) coming from
    # the PDF layout. Applying margins here makes pages >1 drift.
    return 0.0, 0.0


def _page_width(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 595.0
    return _num(pg_sz.get(f"{{{W_NS}}}w"), 11900.0) / 20.0


def _font_size_for_width(text: str, width: float) -> int:
    longest = max((len(part) for part in text.split()), default=len(text))
    if longest <= 0:
        return 10
    return max(6, min(10, int(width / (longest * 0.55))))


def _height_for_text(text: str, width: float, font_size: int, minimum: float) -> float:
    if width <= 0:
        return minimum
    estimated_text_width = max(1.0, len(text) * font_size * 0.55)
    lines = max(1, int((estimated_text_width + width - 1) // width))
    return max(minimum, lines * font_size * 1.35)


def _wrap_text_lines(text: str, width: float, font_size: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]

    max_chars = max(1, int(width / max(1.0, font_size * 0.55)))
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines



def _append_vml_textbox(
    paragraph: ET.Element,
    *,
    shape_id: str,
    x: float,
    y: float,
    width: float,
    height: float,
    text: str,
    font_size: int = 10,
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
    shape.set("type", "#_x0000_t202")
    shape.set(
        "style",
        (
            "position:absolute;left:0;text-align:left;"
            f"margin-left:{x:g}pt;margin-top:{y:g}pt;"
            f"width:{width:g}pt;height:{height:g}pt;"
            "z-index:251659264;mso-wrap-style:none;"
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
    rrpr = ET.SubElement(ppr, f"{{{W_NS}}}rPr")
    ET.SubElement(rrpr, f"{{{W_NS}}}b")
    p_color = ET.SubElement(rrpr, f"{{{W_NS}}}color")
    p_color.set(f"{{{W_NS}}}val", color_hex)

    inner_r = ET.SubElement(p, f"{{{W_NS}}}r")
    rpr = ET.SubElement(inner_r, f"{{{W_NS}}}rPr")
    ET.SubElement(rpr, f"{{{W_NS}}}b")
    sz = ET.SubElement(rpr, f"{{{W_NS}}}sz")
    sz.set(f"{{{W_NS}}}val", str(font_size * 2))
    color = ET.SubElement(rpr, f"{{{W_NS}}}color")
    color.set(f"{{{W_NS}}}val", color_hex)
    for i, line in enumerate(_wrap_text_lines(text, width, font_size)):
        if i:
            ET.SubElement(inner_r, f"{{{W_NS}}}br")
        t = ET.SubElement(inner_r, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line

    wrap = ET.SubElement(shape, f"{{{W10_NS}}}wrap")
    wrap.set("anchorx", "page")
    wrap.set("anchory", "page")
    # Prevent Word from moving the anchor (important for multi-page docs).
    ET.SubElement(shape, f"{{{W10_NS}}}anchorlock")


_PLACEHOLDER_RE = re.compile(r"(?:_{4,}|[.…\.]{4,})")


@dataclass(frozen=True)
class _TextRun:
    t_el: ET.Element
    r_el: ET.Element
    start: int
    end: int


def _build_text_runs(root: ET.Element) -> Tuple[str, list[_TextRun]]:
    runs: list[_TextRun] = []
    parts: list[str] = []
    idx = 0

    for r in root.findall(".//w:r", NS):
        for t in r.findall(".//w:t", NS):
            s = t.text or ""
            parts.append(s)
            runs.append(_TextRun(t_el=t, r_el=r, start=idx, end=idx + len(s)))
            idx += len(s)

    return "".join(parts), runs


def _set_run_red(r_el: ET.Element) -> None:
    rpr = r_el.find("w:rPr", NS)
    if rpr is None:
        rpr = ET.Element(f"{{{W_NS}}}rPr")
        r_el.insert(0, rpr)

    color = rpr.find("w:color", NS)
    if color is None:
        color = ET.SubElement(rpr, f"{{{W_NS}}}color")
    color.set(f"{{{W_NS}}}val", "FF0000")


def _find_label_pos(full_text: str, label: str, start_at: int) -> Optional[Tuple[int, str]]:
    label = (label or "").strip()
    if not label:
        return None

    # 1) Exact search from cursor.
    pos = full_text.find(label, start_at)
    if pos != -1:
        return pos, label

    # 2) Exact search from beginning (some labels may appear before cursor due to OCR quirks).
    pos = full_text.find(label)
    if pos != -1:
        return pos, label

    # 3) Fallback: search with a keyword phrase extracted from the label.
    words = re.findall(r"[0-9A-Za-zÀ-ÿ]+", label)
    for n in (6, 5, 4, 3, 2):
        if len(words) >= n:
            phrase = " ".join(words[:n])
            pos = full_text.find(phrase, start_at)
            if pos != -1:
                return pos, phrase
    for n in (6, 5, 4, 3, 2):
        if len(words) >= n:
            phrase = " ".join(words[-n:])
            pos = full_text.find(phrase, start_at)
            if pos != -1:
                return pos, phrase

    # 4) Last resort: whole-word search for short labels (e.g. "il", "a", "c)").
    if len(label) <= 3:
        pattern = re.compile(rf"(?<![0-9A-Za-zÀ-ÿ]){re.escape(label)}(?![0-9A-Za-zÀ-ÿ])")
        m = pattern.search(full_text, start_at)
        if m:
            return m.start(), label

    return None


def _iter_placeholder_spans(full_text: str) -> list[Tuple[int, int]]:
    spans: list[Tuple[int, int]] = []
    for m in _PLACEHOLDER_RE.finditer(full_text):
        spans.append(m.span())
    return spans


def _sorted_filled_rows(rows: Iterable[dict]) -> list[dict]:
    filled = []
    for r in rows:
        ans = (r.get("answer") or "").strip()
        if not ans:
            continue
        bbox = r.get("bbox") or [0, 0, 0, 0]
        page = r.get("page") or 0
        try:
            key = (int(page), float(bbox[1]), float(bbox[0]))
        except Exception:
            key = (0, 0.0, 0.0)
        filled.append((key, r))
    filled.sort(key=lambda x: x[0])
    return [r for _, r in filled]


def _normalize_key(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _cell_text(tc_el: ET.Element) -> str:
    return "".join(t.text or "" for t in tc_el.findall(".//w:t", NS))


def _ensure_paragraph_run(tc_el: ET.Element) -> ET.Element:
    p = tc_el.find("w:p", NS)
    if p is None:
        p = ET.SubElement(tc_el, f"{{{W_NS}}}p")
    r = ET.SubElement(p, f"{{{W_NS}}}r")
    t = ET.SubElement(r, f"{{{W_NS}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return r


def _set_cell_answer_red(tc_el: ET.Element, answer: str) -> None:
    # Only safe behavior: if the cell is empty (or whitespace), replace its text with a single red run.
    existing = _cell_text(tc_el).strip()
    if existing:
        return

    # Remove all paragraphs and rebuild a minimal one.
    for p in list(tc_el.findall("w:p", NS)):
        tc_el.remove(p)

    p = ET.SubElement(tc_el, f"{{{W_NS}}}p")
    r = ET.SubElement(p, f"{{{W_NS}}}r")
    t = ET.SubElement(r, f"{{{W_NS}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = answer
    _set_run_red(r)


def _try_fill_table_by_header(root: ET.Element, label: str, answer: str) -> bool:
    """
    If `label` matches a table header cell, fill the cell directly below (same column).
    This fixes cases where the data cell is empty (no placeholder underscores/dots).
    """
    key = _normalize_key(label)
    if not key:
        return False
    # Only match exact header text (normalized) to avoid false positives.
    strict = True

    for tbl in root.findall(".//w:tbl", NS):
        rows = tbl.findall("./w:tr", NS)
        if len(rows) < 2:
            continue

        matrix: list[list[ET.Element]] = []
        for tr in rows:
            matrix.append(tr.findall("./w:tc", NS))

        for r_i, tcs in enumerate(matrix[:-1]):  # exclude last row (no below)
            for c_i, tc in enumerate(tcs):
                cell_key = _normalize_key(_cell_text(tc))
                if not cell_key:
                    continue
                if cell_key != key:
                    continue
                    below_row = matrix[r_i + 1]
                    if c_i >= len(below_row):
                        continue
                    target_tc = below_row[c_i]
                    before = _cell_text(target_tc).strip()
                    _set_cell_answer_red(target_tc, answer)
                    after = _cell_text(target_tc).strip()
                    if not before and after:
                        return True

    return False


def _replace_span_with_answer(
    root: ET.Element,
    runs: list[_TextRun],
    span_start: int,
    span_end: int,
    answer: str,
) -> None:
    """
    Replace characters in [span_start, span_end) within the linearized text stream.
    Works even if the span crosses multiple <w:t>.
    """
    if span_end <= span_start:
        return

    def _clone_run_like(src_r: ET.Element) -> ET.Element:
        new_r = ET.Element(f"{{{W_NS}}}r")
        src_rpr = src_r.find("w:rPr", NS)
        if src_rpr is not None:
            new_r.append(copy.deepcopy(src_rpr))
        return new_r

    def _append_text(r_el: ET.Element, text: str) -> ET.Element:
        t = ET.SubElement(r_el, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = text
        return t

    start_i = None
    end_i = None
    for i, run in enumerate(runs):
        if start_i is None and run.start <= span_start < run.end:
            start_i = i
        if run.start < span_end <= run.end:
            end_i = i
            break
    if start_i is None:
        return
    if end_i is None:
        end_i = start_i

    start_run = runs[start_i]
    end_run = runs[end_i]

    # Prefix/suffix around the replaced span.
    start_off = span_start - start_run.start
    end_off = span_end - end_run.start

    start_text = start_run.t_el.text or ""
    end_text = end_run.t_el.text or ""
    prefix = start_text[:start_off]
    suffix = end_text[end_off:]

    parent = None
    # Find the parent container to insert runs (paragraph or cell).
    for p in root.findall(".//w:p", NS):
        for r in p.findall("./w:r", NS):
            if r is start_run.r_el:
                parent = p
                break
        if parent is not None:
            break
    if parent is None:
        # Fallback: do an in-place replacement (still safe) but color only the inserted run is not possible.
        start_run.t_el.text = f"{prefix}{answer}{suffix}" if start_i == end_i else f"{prefix}{answer}"
        _set_run_red(start_run.r_el)
        if end_i != start_i:
            for j in range(start_i + 1, end_i):
                runs[j].t_el.text = ""
            end_run.t_el.text = suffix
        return

    # Index of the start run in its parent.
    siblings = list(parent.findall("./w:r", NS))
    try:
        start_idx = siblings.index(start_run.r_el)
    except ValueError:
        start_idx = None

    # Rewrite start run to only keep prefix.
    start_run.t_el.text = prefix

    # Remove placeholder-carrying text in subsequent runs up to end.
    if end_i != start_i:
        for j in range(start_i + 1, end_i + 1):
            runs[j].t_el.text = ""
        # Put suffix back into end run as separate normal text later.
        end_run.t_el.text = ""

    # Insert a dedicated red run containing the answer.
    red_r = _clone_run_like(start_run.r_el)
    _append_text(red_r, answer)
    _set_run_red(red_r)

    insert_at = (start_idx + 1) if start_idx is not None else len(siblings)
    parent.insert(insert_at, red_r)

    # Insert suffix as normal (non-red) run after the answer.
    if suffix:
        suffix_r = _clone_run_like(start_run.r_el)
        _append_text(suffix_r, suffix)
        parent.insert(insert_at + 1, suffix_r)


def compile_docx_from_json(
    *,
    json_path: Path,
    output_path: Path,
    word_path_override: Path | None = None,
    color_hex: str = "FF0000",
) -> Path:
    data = _read_json(json_path)
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        raise ValueError("JSON non valido: atteso 'rows' come lista.")

    if word_path_override is not None:
        src_docx = word_path_override
    else:
        src = str(data.get("source_document_path") or "").strip()
        if not src:
            raise ValueError("JSON non valido: manca 'source_document_path'.")
        src_docx = Path(_win_to_wsl_path(src))
    if not src_docx.exists():
        raise FileNotFoundError(f"Documento sorgente non trovato: {src_docx}")
    if src_docx.suffix.lower() != ".docx":
        raise ValueError(f"Documento sorgente non è .docx: {src_docx}")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)


    filled_rows = _filled_coordinate_rows(rows)
    if not filled_rows:
        raise RuntimeError("Nessun campo compilato: nel JSON non ci sono answer con bbox valide.")

    applied = 0

    with zipfile.ZipFile(src_docx, "r") as z:
        doc_xml = z.read("word/document.xml")

    _register_docx_namespaces(doc_xml)
    root = ET.fromstring(doc_xml)
    root.set(f"{{{MC_NS}}}Ignorable", "w14")
    anchors = _paragraph_page_anchors(root)
    page_width = _page_width(root)
    fallback_anchor = anchors.get(1)
    if fallback_anchor is None:
        fallback_anchor = root.find(".//w:p", NS)
    if fallback_anchor is None:
        raise RuntimeError("DOCX non valido: nessun paragrafo disponibile per ancorare le textbox.")

    for row in filled_rows:
        bb = _bbox(row)
        answer = _answer(row)
        if not bb or not answer:
            continue

        page = _page(row)
        x0, y0, x1, y1 = [_num(v) for v in bb]
        dx, dy = _page_overlay_offset(root, page)
        x = x0 - dx
        y = y0 - dy
        raw_width = max(30.0, x1 - x0, min(420.0, len(answer) * 5.2))
        width = min(raw_width, max(30.0, page_width - x0 - 4.0))
        font_size = _font_size_for_width(answer, width)
        height = _height_for_text(answer, width, font_size, max(12.0, y1 - y0 + 4.0))
        item_id = str(row.get("item_id") or applied).replace(":", "_")

        _append_vml_textbox(
            anchors.get(page) if anchors.get(page) is not None else fallback_anchor,
            shape_id=f"campo_json_{item_id}",
            x=x,
            y=y,
            width=width,
            height=height,
            text=answer,
            font_size=font_size,
            color_hex=color_hex,
        )

        applied += 1

    with zipfile.ZipFile(src_docx, "r") as z_in, zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
        for info in z_in.infolist():
            if info.filename == "word/document.xml":
                z_out.writestr(info, ET.tostring(root, encoding="utf-8", xml_declaration=True))
            else:
                z_out.writestr(info, z_in.read(info.filename))

    if applied == 0:
        raise RuntimeError("Nessun campo compilato: nessuna textbox inserita nel DOCX.")

    return out_path

def write_docx_from_mapping(
    source_path: str | Path,
    mapping_path: str | Path,
    output_path: str | Path,
) -> dict[str, int | str]:
    src = Path(_win_to_wsl_path(str(source_path))).resolve()
    mapping = Path(_win_to_wsl_path(str(mapping_path))).resolve()
    out = Path(_win_to_wsl_path(str(output_path))).resolve()

    payload = _read_json(mapping)
    rows = payload.get("rows") or []
    replaced_count = len(_filled_coordinate_rows(rows if isinstance(rows, list) else []))

    compiled = compile_docx_from_json(
        json_path=mapping,
        output_path=out,
        word_path_override=src,
        color_hex="000000",
    )
    return {
        "source_path": str(src),
        "mapping_path": str(mapping),
        "output_path": str(compiled),
        "replaced_count": replaced_count,
    }


def write_docx_preview_from_answers_json(
    source_path: str | Path,
    answers_json_path: str | Path,
    output_path: str | Path,
    *,
    color_hex: str = "0000FF",
) -> dict[str, int | str]:
    src = Path(_win_to_wsl_path(str(source_path))).resolve()
    answers = Path(_win_to_wsl_path(str(answers_json_path))).resolve()
    out = Path(_win_to_wsl_path(str(output_path))).resolve()

    payload = _read_json(answers)
    rows = payload.get("rows") or []
    replaced_count = len(_filled_coordinate_rows(rows if isinstance(rows, list) else []))

    compiled = compile_docx_from_json(
        json_path=answers,
        output_path=out,
        word_path_override=src,
        color_hex=color_hex,
    )
    return {
        "source_path": str(src),
        "mapping_path": str(answers),
        "output_path": str(compiled),
        "replaced_count": replaced_count,
    }


def main() -> None:
    here = Path(__file__).resolve().parent

    json_default = Path(_win_to_wsl_path(JSON_INPUT_PATH))
    word_default = Path(_win_to_wsl_path(WORD_TEMPLATE_PATH))

    json_path = Path(os.getenv("M2_JSON_PATH", str(json_default)))
    word_path = Path(os.getenv("M2_WORD_PATH", str(word_default)))
    if not json_path.exists():
        raise FileNotFoundError(f"JSON non trovato: {json_path}")
    word_override = word_path if word_path.exists() and word_path.suffix.lower() == ".docx" else None

    out_path = compile_docx_from_json(
        json_path=json_path,
        output_path=here / "documento_compilato_debug.docx",
        word_path_override=word_override,
    )
    print(str(out_path))


if __name__ == "__main__":
    main()
