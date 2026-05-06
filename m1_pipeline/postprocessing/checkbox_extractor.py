import re
from typing import Any, Dict, List

from docx import Document


_CHECKBOX_RE = re.compile(r"^[\s□■☑☒]+")
_CHECKBOX_ANY_RE = re.compile(r"[□■☑☒]")
_MIN_INDENT_DELTA_PT = 6.0


def extract_checkboxes(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Estrae le caselle da spuntare (checkbox) dai blocchi.
    Considera checkbox solo i blocchi con testo che inizia con simboli □■☑☒.
    Accorpa le righe successive che risultano indentate (continuazione multi-riga).
    """
    items: List[Dict[str, Any]] = []
    if not blocks:
        return items

    for i, block in enumerate(blocks):
        text = str(block.get("text", "")).strip()
        if not text:
            continue
        if _CHECKBOX_RE.search(text) is None:
            continue

        page = block.get("page", 1)
        bbox = block.get("bbox", [0, 0, 0, 0])
        start_x = float(bbox[0]) if bbox else 0.0
        lines = [text]

        # Accorpa righe successive indentate sulla stessa pagina
        prev = block
        for next_block in blocks[i + 1:]:
            if next_block.get("page", 1) != page:
                break
            next_text = str(next_block.get("text", "")).strip()
            if not next_text:
                prev = next_block
                continue
            # Stop se inizia un'altra checkbox
            if _CHECKBOX_RE.search(next_text):
                break

            next_bbox = next_block.get("bbox", [0, 0, 0, 0])
            next_x = float(next_bbox[0]) if next_bbox else 0.0
            if next_x >= start_x + _MIN_INDENT_DELTA_PT:
                lines.append(next_text)
                prev = next_block
                continue
            break

        label = _clean_checkbox_label(text)
        items.append(
            {
                "label": label,
                "text": " ".join(lines).strip(),
                "lines": lines,
                "page": page,
                "bbox": bbox,
                "marker_bbox": _marker_bbox_from_block_bbox(bbox, text),
            }
        )

    return items


def _clean_checkbox_label(text: str) -> str:
    text = _CHECKBOX_RE.sub("", text).strip()
    text = text.strip(" :,;./\\()")
    return text.strip()


def extract_checkboxes_from_docx(docx_path: str) -> List[Dict[str, Any]]:
    """
    Estrae checkbox da un DOCX usando python-docx.
    Riconosce simboli checkbox e controlli checkBox nel XML.
    Accorpa paragrafi successivi indentati come continuazione.
    """
    doc = Document(docx_path)
    items: List[Dict[str, Any]] = []

    # Paragraphs
    items.extend(_extract_from_paragraphs(list(doc.paragraphs)))

    # Table cells
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_items = _extract_from_paragraphs(list(cell.paragraphs), allow_indent=False)
                items.extend(cell_items)

    # Dedupe by (label, text)
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        key = (item.get("label"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def _extract_from_paragraphs(paragraphs, allow_indent: bool = True) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        if not _paragraph_has_checkbox(p):
            i += 1
            continue

        text = _paragraph_text_with_checkbox(p)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        start_indent = _indent_pt(p)

        j = i + 1
        while j < len(paragraphs):
            p2 = paragraphs[j]
            if _paragraph_has_checkbox(p2):
                break
            if not p2.text or not p2.text.strip():
                j += 1
                continue
            if allow_indent and _indent_pt(p2) >= start_indent + _MIN_INDENT_DELTA_PT:
                lines.extend([line.strip() for line in p2.text.splitlines() if line.strip()])
                j += 1
                continue
            if not allow_indent:
                lines.extend([line.strip() for line in p2.text.splitlines() if line.strip()])
                j += 1
                continue
            break

        full_text = " ".join(lines).strip()
        label = _clean_checkbox_label(lines[0]) if lines else ""
        items.append(
            {
                "label": label,
                "text": full_text,
                "lines": lines,
                "page": 1,
                "bbox": None,
                "marker_bbox": None,
            }
        )
        i = j

    return items


def _paragraph_has_checkbox(paragraph) -> bool:
    # Testo diretto
    if _CHECKBOX_ANY_RE.search(paragraph.text or ""):
        return True
    # Simboli in XML (w:sym o controllo checkbox)
    xml = paragraph._p.xml
    if "<w:sym" in xml:
        return True
    if "<w:checkBox" in xml or "<w14:checkbox" in xml:
        return True
    return False


def _paragraph_text_with_checkbox(paragraph) -> str:
    text = paragraph.text or ""
    if _CHECKBOX_ANY_RE.search(text):
        return text
    # Se il simbolo non è nel testo, premetti un placeholder checkbox.
    return f"□ {text}".strip()


def _indent_pt(paragraph) -> float:
    indent = paragraph.paragraph_format.left_indent
    if indent is None:
        return 0.0
    return float(indent.pt)


def _marker_bbox_from_block_bbox(bbox, text: str):
    if not bbox or len(bbox) != 4:
        return None
    x, y, w, h = [float(value) for value in bbox]
    stripped = (text or "").lstrip()
    if not stripped:
        return None
    if stripped[0] not in {"□", "■", "☑", "☒"}:
        return None
    size = max(6.0, min(h, 14.0))
    return [round(x, 2), round(y, 2), round(size, 2), round(h, 2)]
