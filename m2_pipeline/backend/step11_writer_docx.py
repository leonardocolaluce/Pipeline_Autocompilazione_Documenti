import argparse
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import xml.etree.ElementTree as ET

# --- INPUT PATHS ---

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent

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
    font_size: int


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _register_docx_namespaces(doc_xml: bytes) -> None:
    try:
        head = doc_xml.decode("utf-8", errors="ignore")
        m = re.search(r"<w:document\\s+([^>]+)>", head)
        if m:
            for pref, uri in re.findall(r'xmlns:([A-Za-z0-9]+)=\"([^\"]+)\"', m.group(1)):
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
    body = root.find("w:body", NS)
    paragraphs = body.findall(".//w:p", NS) if body is not None else root.findall(".//w:p", NS)

    anchors: dict[int, ET.Element] = {}
    page = 1
    pending_anchors: list[int] = []

    for p in paragraphs:
        if pending_anchors:
            for pg in pending_anchors:
                anchors.setdefault(pg, p)
            pending_anchors.clear()

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
                pending_anchors.append(page)

    return anchors


def _page_width(root: ET.Element) -> float:
    pg_sz = root.find(".//w:sectPr/w:pgSz", NS)
    if pg_sz is None:
        return 595.0
    return _num(pg_sz.get(f"{{{W_NS}}}w"), 11900.0) / 20.0


def _font_size_for_width(text: str, width: float) -> int:
    words = text.split()
    longest = max((len(part) for part in words), default=len(text))
    total = len(text)
    basis = longest
    if len(words) >= 3 and total > (longest * 2):
        basis = total
    if basis <= 0:
        return 10
    return max(6, min(12, int(width / (basis * 0.55))))


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


def _fit_font_size_to_box(text: str, width: float, height: float, start_fs: int) -> int:
    fs = max(6, int(start_fs))
    height = max(6.0, float(height))
    while fs > 6:
        lines = _wrap_text_lines(text, width, fs)
        needed = max(1, len(lines)) * (fs * 1.18)
        if needed <= height:
            break
        fs -= 1
    return max(6, fs)


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
    jc = ET.SubElement(ppr, f"{{{W_NS}}}jc")
    jc.set(f"{{{W_NS}}}val", "center")
    spacing = ET.SubElement(ppr, f"{{{W_NS}}}spacing")
    spacing.set(f"{{{W_NS}}}before", "0")
    spacing.set(f"{{{W_NS}}}after", "0")
    rrpr = ET.SubElement(ppr, f"{{{W_NS}}}rPr")
    p_color = ET.SubElement(rrpr, f"{{{W_NS}}}color")
    p_color.set(f"{{{W_NS}}}val", color_hex)
    p_sz = ET.SubElement(rrpr, f"{{{W_NS}}}sz")
    p_sz.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    p_szcs = ET.SubElement(rrpr, f"{{{W_NS}}}szCs")
    p_szcs.set(f"{{{W_NS}}}val", str(int(font_size) * 2))
    rr = ET.SubElement(p, f"{{{W_NS}}}r")
    t = ET.SubElement(rr, f"{{{W_NS}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text


def _bbox_as_rect(bb: list[Any]) -> tuple[float, float, float, float]:
    """
    Normalizza bbox a (x0,y0,x1,y1) in punti.

    Alcuni JSON salvano bbox come [x0,y0,x1,y1], altri come [x0,y0,w,h].
    """
    x0, y0, a, b = (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    # Dimension-style bbox heuristic (compatibile con scrittura_word.py)
    if a >= 0 and b >= 0 and b <= 80 and y0 >= 100 and a <= 700:
        return x0, y0, x0 + a, y0 + b
    nx0, nx1 = (x0, a) if x0 <= a else (a, x0)
    ny0, ny1 = (y0, b) if y0 <= b else (b, y0)
    return nx0, ny0, nx1, ny1


def _load_rows_tmp(path: Path) -> List[Row]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # Supporta anche `campo_valore_provvisorio.json` (dict con chiave "rows")
    # trasformandolo in una lista rows_tmp compatibile: {page,x0,y0,w,h,text,font_size}
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        extracted: list[dict[str, Any]] = []
        for r in data.get("rows") or []:
            if not isinstance(r, dict):
                continue
            text = str(r.get("answer") or "").strip()
            if (not text) or (text == "N/D"):
                continue
            bb = r.get("bbox") or r.get("marker_bbox") or r.get("checkbox_bbox")
            if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
                continue
            page = int(r.get("page") or 1)
            x0, y0, x1, y1 = _bbox_as_rect(list(bb))
            w = max(1.0, x1 - x0)
            h = max(1.0, y1 - y0)
            extracted.append({"page": page, "x0": x0, "y0": y0, "w": w, "h": h, "text": text, "font_size": None})
        data = extracted

    if not isinstance(data, list):
        raise SystemExit("JSON non valido: atteso una lista (rows_tmp) oppure un dict con 'rows'.")

    rows: List[Row] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        page = int(item.get("page") or 1)
        x0 = float(item.get("x0") or 0)
        y0 = float(item.get("y0") or 0)
        w = float(item.get("w") or 0)
        h = float(item.get("h") or 0)
        start_fs = _font_size_for_width(text, w)  # stima iniziale
        font_size = _fit_font_size_to_box(text, w, h, start_fs)  # scende finché entra nel box
        rows.append(Row(page=page, x0=x0, y0=y0, w=w, h=h, text=text, font_size=font_size))


    rows.sort(key=lambda r: (r.page, r.y0, r.x0))
    if not rows:
        raise SystemExit("Nessun campo trovato nel JSON rows_tmp.")
    return rows


def _compile_with_word_com(*, src_docx: Path, out_docx: Path, rows: List[Row]) -> None:
    """
    Usa Word (COM) via PowerShell per posizionare textbox per pagina con coordinate in pt.
    Richiede Microsoft Word installato sulla macchina Windows (powershell.exe).
    """
    ps_bin = "powershell" if os.name == "nt" else "powershell.exe"
    src_win = str(src_docx) if os.name == "nt" else str(src_docx).replace("/mnt/c/", "C:/").replace("/", "\\")
    out_win = str(out_docx) if os.name == "nt" else str(out_docx).replace("/mnt/c/", "C:/").replace("/", "\\")

    tmp = out_docx.with_suffix(".rows_tmp.json")
    payload = [
        {"page": r.page, "x0": r.x0, "y0": r.y0, "w": max(5.0, r.w), "h": max(10.0, r.h), "text": r.text, "font_size": int(r.font_size)}
        for r in rows
    ]
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_win = str(tmp) if os.name == "nt" else str(tmp).replace("/mnt/c/", "C:/").replace("/", "\\")

    # Office RGB property uses BGR order; red is 255.
    ps = rf"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Invoke-Retry([scriptblock]$sb, [int]$tries = 12) {{
  $last = $null
  for ($i = 0; $i -lt $tries; $i++) {{
    try {{ return & $sb }} catch {{ $last = $_; Start-Sleep -Milliseconds (200 + 200 * $i) }}
  }}
  throw $last
}}
$docx = "{src_win}"
$out = "{out_win}"
$rowsPath = "{tmp_win}"
$rows = Get-Content -LiteralPath $rowsPath -Raw | ConvertFrom-Json
$word = Invoke-Retry {{ New-Object -ComObject Word.Application }}
$word.Visible = $false
$word.DisplayAlerts = 0
$doc = Invoke-Retry {{ $word.Documents.Open($docx, $false, $false) }}
try {{
  # Word constants (numeric to avoid interop issues):
  # wdGoToPage=1, wdGoToAbsolute=1, wdCollapseStart=1
  # wdWithInTable=12 (Range.Information)
  $wdWithInTable = 12
  $wdCollapseStart = 1

  function Get-PageAnchor([int]$page) {{
    $rng = $doc.GoTo(1, 1, $page)
    $rng.Collapse($wdCollapseStart) | Out-Null

    # If the range is inside a table (common when a page starts with a table),
    # move the anchor to the first paragraph AFTER that table. This prevents
    # Word from reinterpreting coordinates relative to the table/cell.
    try {{
      if ($rng.Information($wdWithInTable)) {{
        $tbl = $rng.Tables(1)
        $rng = $tbl.Range
        # Collapse to end of table, then advance one character to be outside.
        $rng.Collapse(0) | Out-Null
        $rng.MoveStart(1, 1) | Out-Null
        $rng.Collapse($wdCollapseStart) | Out-Null
      }}
    }} catch {{}}
    return $rng
  }}

  foreach ($r in $rows) {{
    $p = [int]$r.page
    $x0 = [double]$r.x0; $y0 = [double]$r.y0
    $w = [double]$r.w; $h = [double]$r.h
    $text = [string]$r.text
    $fs = [int]$r.font_size
    $rngPage = Get-PageAnchor $p
    $shape = $doc.Shapes.AddTextbox(1, $x0, $y0, $w, $h, $rngPage)
    if ($shape.TextFrame2 -ne $null) {{
      $shape.TextFrame2.TextRange.Text = $text
      $shape.TextFrame2.TextRange.ParagraphFormat.Alignment = 1  # center
      $shape.TextFrame2.TextRange.Font.Fill.ForeColor.RGB = 0
      $shape.TextFrame2.TextRange.Font.Bold = 0
      $shape.TextFrame2.TextRange.Font.Size = $fs
      $shape.TextFrame2.WordWrap = 1
      $shape.TextFrame2.AutoSize = 0
      $shape.TextFrame2.MarginLeft = 0; $shape.TextFrame2.MarginRight = 0
      $shape.TextFrame2.MarginTop = 0; $shape.TextFrame2.MarginBottom = 0
    }} else {{
      $shape.TextFrame.TextRange.Text = $text
      $shape.TextFrame.TextRange.ParagraphFormat.Alignment = 1  # center
      $shape.TextFrame.TextRange.Font.Color = 0
      $shape.TextFrame.TextRange.Font.Bold = $false
      $shape.TextFrame.TextRange.Font.Size = $fs
      $shape.TextFrame.WordWrap = $true
      $shape.TextFrame.AutoSize = 0
      $shape.TextFrame.MarginLeft = 0; $shape.TextFrame.MarginRight = 0
      $shape.TextFrame.MarginTop = 0; $shape.TextFrame.MarginBottom = 0
    }}
    $shape.Fill.Visible = 0
    $shape.Line.Visible = 0
    $shape.RelativeHorizontalPosition = 1
    $shape.RelativeVerticalPosition = 1
    $shape.WrapFormat.Type = 3
    $shape.LayoutInCell = $false
    try {{ $shape.LockAnchor = $true }} catch {{}}
    $shape.Left = $x0
    $shape.Top = $y0
  }}
  Invoke-Retry {{ $doc.SaveAs([ref]$out, [ref]16) | Out-Null }} | Out-Null
}} finally {{
  try {{ $doc.Close([ref]0) | Out-Null }} catch {{}}
  try {{ $word.Quit() | Out-Null }} catch {{}}
  try {{ [System.Runtime.Interopservices.Marshal]::ReleaseComObject($doc) | Out-Null }} catch {{}}
  try {{ [System.Runtime.Interopservices.Marshal]::ReleaseComObject($word) | Out-Null }} catch {{}}
}}
"""
    timeout_s = int(os.getenv("M2_WORD_COM_TIMEOUT_S", "900"))
    subprocess.run([ps_bin, "-NoProfile", "-Command", ps], check=True, timeout=timeout_s)
    try:
        tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass


def _compile_with_xml(*, src_docx: Path, out_docx: Path, rows: List[Row]) -> None:
    applied = 0
    with zipfile.ZipFile(src_docx, "r") as z:
        doc_xml = z.read("word/document.xml")

    _register_docx_namespaces(doc_xml)
    root = ET.fromstring(doc_xml)
    root.set(f"{{{MC_NS}}}Ignorable", "w14")

    anchors = _paragraph_page_anchors(root)
    page_width = _page_width(root)
    fallback_anchor = anchors.get(1) or root.find(".//w:p", NS)
    if fallback_anchor is None:
        raise RuntimeError("DOCX non valido: nessun paragrafo disponibile per ancorare le textbox.")

    for i, r in enumerate(rows):
        page = max(1, int(r.page))
        x = float(r.x0)
        y = float(r.y0)
        width = min(max(5.0, float(r.w)), max(5.0, page_width - x - 1.0))
        height = max(10.0, float(r.h))
        fs = _fit_font_size_to_box(r.text, width, height, int(r.font_size))
        anchor = anchors.get(page) if anchors.get(page) is not None else fallback_anchor
        _append_vml_textbox(
            anchor,
            shape_id=f"campo_rows_{page}_{i}",
            x=x,
            y=y,
            width=width,
            height=height,
            text=r.text,
            font_size=fs,
            color_hex="000000",
        )
        applied += 1

    if applied <= 0:
        raise RuntimeError("Nessun campo compilato: nessuna textbox inserita nel DOCX.")

    with zipfile.ZipFile(src_docx, "r") as z_in, zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
        for info in z_in.infolist():
            if info.filename == "word/document.xml":
                z_out.writestr(info, ET.tostring(root, encoding="utf-8", xml_declaration=True))
            else:
                z_out.writestr(info, z_in.read(info.filename))

def write_docx_from_mapping(source_docx: str | Path, mapping_json: str | Path, out_docx: str | Path) -> dict[str, Any]:
    src_docx = Path(source_docx).resolve()
    json_path = Path(mapping_json).resolve()
    out_docx = Path(out_docx).resolve()

    out_docx.parent.mkdir(parents=True, exist_ok=True)

    rows = _load_rows_tmp(json_path)

    tmp_src = out_docx.with_suffix(".template_copy.docx")
    shutil.copy2(src_docx, tmp_src)

    used_com = False
    ps_bin = "powershell" if os.name == "nt" else "powershell.exe"
    if shutil.which(ps_bin) is not None:
        try:
            _compile_with_word_com(src_docx=tmp_src, out_docx=out_docx, rows=rows)
            used_com = True
        except Exception:
            used_com = False

    if not used_com:
        _compile_with_xml(src_docx=tmp_src, out_docx=out_docx, rows=rows)

    try:
        tmp_src.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass

    return {"output_path": str(out_docx), "replaced_count": len(rows)}


def write_docx_preview_from_answers_json(
    source_docx: str | Path,
    mapping_json: str | Path,
    out_docx: str | Path,
    *,
    color_hex: str = "0000FF",
) -> dict[str, Any]:
    # Nota: questo writer attuale scrive in rosso; `color_hex` qui viene ignorato.
    return write_docx_from_mapping(source_docx, mapping_json, out_docx)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Compila un Word (.docx) scrivendo campi alle coordinate (pt).")
    ap.add_argument("--word-in", default=PATH_WORD, help="DOCX template input")
    ap.add_argument("--json", default=PATH_JSON, help="JSON rows_tmp (page,x0,y0,w,h,text,font_size)")
    ap.add_argument("--word-out", default=str(HERE / "compiled.docx"), help="DOCX output compilato")
    ap.add_argument("--no-com", action="store_true", help="Disabilita Word COM (usa solo metodo XML).")
    args = ap.parse_args(argv)

    src_docx = Path(args.word_in).resolve()
    json_path = Path(args.json).resolve()
    out_docx = Path(args.word_out).resolve()

    if not src_docx.exists():
        raise SystemExit(f"DOCX non trovato: {src_docx}")
    if src_docx.suffix.lower() != ".docx":
        raise SystemExit(f"Serve un .docx (non .doc): {src_docx}")
    if not json_path.exists():
        raise SystemExit(f"JSON non trovato: {json_path}")

    out_docx.parent.mkdir(parents=True, exist_ok=True)

    rows = _load_rows_tmp(json_path)

    # Copia template -> out, poi compila (così non tocchiamo l’originale).
    tmp_src = out_docx.with_suffix(".template_copy.docx")
    shutil.copy2(src_docx, tmp_src)

    used_com = False
    ps_bin = "powershell" if os.name == "nt" else "powershell.exe"
    if (not args.no_com) and (shutil.which(ps_bin) is not None):
        try:
            _compile_with_word_com(src_docx=tmp_src, out_docx=out_docx, rows=rows)
            used_com = True
        except Exception:
            used_com = False

    if not used_com:
        _compile_with_xml(src_docx=tmp_src, out_docx=out_docx, rows=rows)

    try:
        tmp_src.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass

    print(f"OK: creato {out_docx} (rows={len(rows)}, metodo={'COM' if used_com else 'XML'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
