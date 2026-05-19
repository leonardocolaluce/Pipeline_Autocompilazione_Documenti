from __future__ import annotations

import argparse
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

try:
    from .step11_writer_docx_fields import _convert_doc_to_docx
except ImportError:
    from step11_writer_docx_fields import _convert_doc_to_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W_NS, "v": V_NS, "o": O_NS, "w10": W10_NS, "mc": MC_NS}


@dataclass(frozen=True)
class TableOverlay:
    page: int
    x: float
    y: float
    w: float
    h: float
    text: str
    item_id: str


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _register_namespaces(doc_xml: bytes) -> List[Tuple[str, str]]:
    namespaces: List[Tuple[str, str]] = []
    seen = set()
    try:
        for _event, ns in ET.iterparse(io.BytesIO(doc_xml), events=("start-ns",)):
            prefix, uri = ns
            if not prefix:
                continue
            key = (prefix, uri)
            if key in seen:
                continue
            seen.add(key)
            namespaces.append(key)
            try:
                ET.register_namespace(prefix, uri)
            except Exception:
                pass
    except Exception:
        pass
    for prefix, uri in [
        ("w", W_NS),
        ("v", V_NS),
        ("o", O_NS),
        ("w10", W10_NS),
        ("mc", MC_NS),
    ]:
        try:
            ET.register_namespace(prefix, uri)
        except Exception:
            pass
    return namespaces


def _serialize_preserving_namespaces(root: ET.Element, namespaces: List[Tuple[str, str]]) -> bytes:
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    m = re.search(r"<w:document\b([^>]*)>", xml)
    if not m:
        return xml.encode("utf-8")
    attrs = m.group(1)
    missing = [f' xmlns:{prefix}="{uri}"' for prefix, uri in namespaces if f"xmlns:{prefix}=" not in attrs]
    if missing:
        xml = xml[: m.end() - 1] + "".join(missing) + xml[m.end() - 1 :]
    return xml.encode("utf-8")


def _answer(row: Dict[str, Any]) -> str:
    value = row.get("answer")
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return "" if text in {"", "N/D"} else text


def _bbox(row: Dict[str, Any]) -> Optional[List[Any]]:
    value = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 4 else None


def _bbox_to_xywh(bb: List[Any]) -> Tuple[float, float, float, float]:
    x, y, a, b = (_num(bb[0]), _num(bb[1]), _num(bb[2]), _num(bb[3]))
    # Table JSON bboxes are normally [x, y, width, height]. Keep that faithful.
    if a >= 0 and b >= 0 and a <= 700 and b <= 200:
        return x, y, max(2.0, a), max(2.0, b)
    x0, x1 = (x, a) if x <= a else (a, x)
    y0, y1 = (y, b) if y <= b else (b, y)
    return x0, y0, max(2.0, x1 - x0), max(2.0, y1 - y0)


def _load_table_overlays(json_path: Path) -> List[TableOverlay]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit("JSON non valido: atteso dict con chiave rows oppure lista.")

    out: List[TableOverlay] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "")
        item_type = str(row.get("item_type") or "")
        if not item_id.startswith("table:") and item_type != "table_cell":
            continue
        text = _answer(row)
        bb = _bbox(row)
        if not text or not bb:
            continue
        x, y, w, h = _bbox_to_xywh(bb)
        out.append(
            TableOverlay(
                page=max(1, _int(row.get("page") or 1, 1)),
                x=x,
                y=y,
                w=w,
                h=h,
                text=text,
                item_id=item_id,
            )
        )
    out.sort(key=lambda item: (item.page, item.y, item.x, item.item_id))
    return out


def _page_width(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 595.0
    return _num(pg_sz.get(f"{{{W_NS}}}w"), 11900.0) / 20.0


def _paragraphs(root: ET.Element) -> List[ET.Element]:
    body = root.find("w:body", NS)
    if body is None:
        return root.findall(".//w:p", NS)
    return body.findall(".//w:p", NS)


def _page_anchors(root: ET.Element) -> Dict[int, ET.Element]:
    paragraphs = _paragraphs(root)
    anchors: Dict[int, ET.Element] = {}
    page = 1
    pending: List[int] = []

    for p in paragraphs:
        if pending:
            for pg in pending:
                anchors.setdefault(pg, p)
            pending.clear()
        anchors.setdefault(page, p)

        breaks = len(p.findall(".//w:lastRenderedPageBreak", NS)) + len(
            [br for br in p.findall(".//w:br", NS) if str(br.get(f"{{{W_NS}}}type") or "").lower() == "page"]
        )
        for _ in range(breaks):
            page += 1
            pending.append(page)

    if not anchors and paragraphs:
        anchors[1] = paragraphs[0]
    return anchors


def _wrap_lines(text: str, width_pt: float, font_size: int) -> List[str]:
    words = text.split()
    if not words:
        return [text]
    max_chars = max(1, int(width_pt / max(1.0, font_size * 0.52)))
    lines: List[str] = []
    cur = ""
    for word in words:
        candidate = word if not cur else f"{cur} {word}"
        if len(candidate) <= max_chars:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_font(text: str, width_pt: float, height_pt: float) -> int:
    fs = 8
    while fs > 5:
        lines = _wrap_lines(text, width_pt, fs)
        if len(lines) * fs * 1.1 <= max(5.0, height_pt):
            break
        fs -= 1
    return fs


def _append_table_overlay(paragraph: ET.Element, item: TableOverlay, *, page_width: float) -> None:
    width = min(max(2.0, item.w), max(2.0, page_width - item.x - 1.0))
    height = max(5.0, item.h)
    fs = _fit_font(item.text, width, height)
    shape_id = "tbl_" + re.sub(r"[^A-Za-z0-9_]+", "_", item.item_id or "cell")[:42]

    run = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    pict = ET.SubElement(run, f"{{{W_NS}}}pict")

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
            f"margin-left:{item.x:g}pt;margin-top:{item.y:g}pt;"
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

    rr = ET.SubElement(p, f"{{{W_NS}}}r")
    rpr = ET.SubElement(rr, f"{{{W_NS}}}rPr")
    color = ET.SubElement(rpr, f"{{{W_NS}}}color")
    color.set(f"{{{W_NS}}}val", "FF0000")
    sz = ET.SubElement(rpr, f"{{{W_NS}}}sz")
    sz.set(f"{{{W_NS}}}val", str(fs * 2))
    szcs = ET.SubElement(rpr, f"{{{W_NS}}}szCs")
    szcs.set(f"{{{W_NS}}}val", str(fs * 2))
    for idx, line in enumerate(_wrap_lines(item.text, width, fs)):
        if idx:
            ET.SubElement(rr, f"{{{W_NS}}}br")
        t = ET.SubElement(rr, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line

    wrap = ET.SubElement(shape, f"{{{W10_NS}}}wrap")
    wrap.set("anchorx", "page")
    wrap.set("anchory", "page")
    ET.SubElement(shape, f"{{{W10_NS}}}anchorlock")


def compile_tables_only(*, src_docx: Path, json_path: Path, out_docx: Path) -> Dict[str, Any]:
    items = _load_table_overlays(json_path)
    if not items:
        if src_docx.suffix.lower() == ".docx":
            src_docx = _convert_doc_to_docx(src_docx, out_docx.parent)
        else:
            print(f"[TABLE_OVERLAY] items=0 no_table_rows skip_doc_conversion in={src_docx} out={out_docx}", flush=True)
            return {"output_path": "", "items": 0, "applied": 0}
    else:
        src_docx = _convert_doc_to_docx(src_docx, out_docx.parent)

    with zipfile.ZipFile(src_docx, "r") as zin:
        doc_xml = zin.read("word/document.xml")
    namespaces = _register_namespaces(doc_xml)
    root = ET.fromstring(doc_xml)
    anchors = _page_anchors(root)
    fallback = anchors.get(1)
    if fallback is None:
        fallback = root.find(".//w:p", NS)
    if fallback is None:
        raise RuntimeError("DOCX non valido: nessun paragrafo.")
    page_width = _page_width(root)

    applied = 0
    for item in items:
        anchor = anchors.get(item.page) if anchors.get(item.page) is not None else fallback
        _append_table_overlay(anchor, item, page_width=page_width)
        applied += 1

    out_docx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_docx, "r") as zin, zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename == "word/document.xml":
                zout.writestr(info, _serialize_preserving_namespaces(root, namespaces))
            else:
                zout.writestr(info, zin.read(info.filename))

    print(f"[TABLE_OVERLAY] items={len(items)} applied={applied} out={out_docx}", flush=True)
    return {"output_path": str(out_docx), "items": len(items), "applied": applied}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write only table answers as XML/VML overlays at JSON bbox coordinates.")
    parser.add_argument("--word-in", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--word-out", required=True)
    args = parser.parse_args()
    compile_tables_only(
        src_docx=Path(args.word_in).resolve(),
        json_path=Path(args.json).resolve(),
        out_docx=Path(args.word_out).resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
