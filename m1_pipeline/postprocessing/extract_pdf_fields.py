import argparse
import json
import os
import re
from typing import Any, Dict, List, Tuple

_default_pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input.pdf")
_default_out_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campi_pdf.json")

parser = argparse.ArgumentParser()
parser.add_argument("--input-pdf", default=_default_pdf_path)
parser.add_argument("--out-json", default=_default_out_json_path)
args = parser.parse_args()

pdf_path = args.input_pdf
out_json_path = args.out_json


BLANK_RE = re.compile(r"^[_]{3,}$|^[.]{3,}$|^[-]{3,}$|^([_ .-])\1{4,}$")
INLINE_BLANK_RE = re.compile(r"([_]{3,}|[.]{3,}|[-]{3,})")
BLANK_CHARS = {"_", ".", "-"}


def _try_import_fitz():
    try:
        import fitz  # type: ignore

        return fitz
    except Exception as e:
        raise SystemExit(
            "Errore: manca PyMuPDF.\n"
            "Installa con: pip install PyMuPDF\n"
            f"Dettaglio: {e}"
        )


def _try_import_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        return cv2, np
    except Exception:
        return None, None


def _is_blank_token(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return BLANK_RE.match(t) is not None


def _bbox_iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def _dedupe_candidates(cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in cands:
        bb = c["bbox"]
        duplicate = False
        for o in out:
            # Soglia alta: su moduli ci sono campi vicinissimi sulla stessa riga,
            # e una soglia bassa rischia di fondere campi diversi.
            if _bbox_iou(bb, o["bbox"]) > 0.70:
                duplicate = True
                break
        if not duplicate:
            out.append(c)
    return out


def _make_campo_from_width(width: float) -> str:
    # Heuristica: 1 underscore ogni ~6pt, minimo 5, max 60
    n = int(max(5, min(60, round(width / 6.0))))
    return "_" * n


def _clean_context_word(text: str) -> str:
    # Rimuove "blank inline" tipo: Nome__________ -> Nome
    cleaned = INLINE_BLANK_RE.sub("", text).strip()
    # Se rimane solo punteggiatura/spazi, scarta.
    if not cleaned or BLANK_RE.match(cleaned):
        return ""
    return cleaned


def _normalize_context(context: str) -> str:
    # Rimuove simboli di checkbox/bullet presenti in alcuni PDF
    c = context.replace("", "").replace("", "").replace("□", "").replace("■", "")
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda t: (t[0], t[1]))
    merged: List[Tuple[float, float]] = []
    cur0, cur1 = intervals[0]
    for a, b in intervals[1:]:
        if a <= cur1:
            cur1 = max(cur1, b)
        else:
            merged.append((cur0, cur1))
            cur0, cur1 = a, b
    merged.append((cur0, cur1))
    return merged


def _subtract_intervals(span: Tuple[float, float], obstacles: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    x0, x1 = span
    out: List[Tuple[float, float]] = []
    cur = x0
    for a, b in _merge_intervals(obstacles):
        if b <= cur:
            continue
        if a > cur:
            out.append((cur, min(a, x1)))
        cur = max(cur, b)
        if cur >= x1:
            break
    if cur < x1:
        out.append((cur, x1))
    # filtra segmenti degeneri
    return [(a, b) for a, b in out if b > a]


def _cluster_words_into_lines(words: List[List[Any]], y_tol: float = 3.5) -> List[List[List[Any]]]:
    """
    Raggruppa words (x0,y0,x1,y1,text,...) in linee usando il centro Y.
    Restituisce lista di linee; ogni linea è lista di words ordinate per x0.
    """
    if not words:
        return []

    enriched = []
    for w in words:
        cy = (float(w[1]) + float(w[3])) / 2.0
        enriched.append((cy, w))
    enriched.sort(key=lambda t: t[0])

    lines: List[List[List[Any]]] = []
    current: List[List[Any]] = []
    current_y: float | None = None
    for cy, w in enriched:
        if current_y is None or abs(cy - current_y) <= y_tol:
            current.append(w)
            current_y = cy if current_y is None else (current_y * 0.8 + cy * 0.2)
        else:
            current.sort(key=lambda ww: float(ww[0]))
            lines.append(current)
            current = [w]
            current_y = cy
    current.sort(key=lambda ww: float(ww[0]))
    lines.append(current)
    return lines


def _find_line_for_bbox(lines: List[List[List[Any]]], bbox: Tuple[float, float, float, float], y_tol: float = 8.0):
    x0, y0, x1, y1 = bbox
    cy = (y0 + y1) / 2.0
    best = None
    best_dist = None
    for line in lines:
        if not line:
            continue
        # stima y della linea dal primo/ultimo elemento
        lcy = (float(line[0][1]) + float(line[0][3])) / 2.0
        dist = abs(lcy - cy)
        if dist <= y_tol and (best_dist is None or dist < best_dist):
            best = line
            best_dist = dist
    if best is not None:
        return best

    # Fallback: underline/linea spesso sta sotto al testo, quindi cerca la linea subito sopra.
    above_best = None
    above_dist = None
    for line in lines:
        if not line:
            continue
        lcy = (float(line[0][1]) + float(line[0][3])) / 2.0
        if lcy <= cy:
            dist = cy - lcy
            if dist <= 22.0 and (above_dist is None or dist < above_dist):
                above_best = line
                above_dist = dist
    return above_best


def _context_for_field(
    lines: List[List[List[Any]]],
    field_bbox: Tuple[float, float, float, float],
    line_field_bboxes: List[Tuple[float, float, float, float]],
) -> str:
    """
    Associa al campo il testo immediatamente precedente sulla stessa riga visiva.

    Logica:
    1. trova la riga di testo più vicina al campo
    2. trova il campo precedente sulla stessa riga
    3. prende solo le parole comprese tra il campo precedente e il campo attuale
    4. pulisce parentesi e simboli isolati
    """

    x0, y0, x1, y1 = field_bbox
    field_cy = (y0 + y1) / 2.0

    # Trova la riga testuale più compatibile con il campo.
    # Preferisce la riga leggermente sopra o sulla stessa altezza.
    best_line = None
    best_score = None

    for line in lines:
        if not line:
            continue

        line_y0 = min(float(w[1]) for w in line)
        line_y1 = max(float(w[3]) for w in line)
        line_cy = (line_y0 + line_y1) / 2.0

        # Distanza verticale dal campo
        dist = abs(line_cy - field_cy)

        # Nei PDF i testi spesso stanno poco sopra la linea del campo
        if line_cy <= field_cy:
            score = dist
        else:
            score = dist + 8.0

        if dist <= 28.0 and (best_score is None or score < best_score):
            best_line = line
            best_score = score

    if best_line is None:
        return ""

    # Trova i campi sulla stessa riga visiva
    same_row_fields = []
    for bb in line_field_bboxes:
        bx0, by0, bx1, by1 = bb
        bcy = (by0 + by1) / 2.0

        if abs(bcy - field_cy) <= 8.0:
            same_row_fields.append(bb)

    same_row_fields.sort(key=lambda bb: bb[0])

    # Trova il campo precedente sulla stessa riga
    prev_field = None
    for bb in same_row_fields:
        if bb[2] <= x0:
            prev_field = bb
        else:
            break

    left_limit = prev_field[2] + 2.0 if prev_field else -1e9
    right_limit = x0 - 2.0

    # Prende solo le parole tra campo precedente e campo attuale
    selected_words = []

    for w in best_line:
        wx0 = float(w[0])
        wy0 = float(w[1])
        wx1 = float(w[2])
        wy1 = float(w[3])
        text = str(w[4])

        if not text.strip():
            continue

        word_center_x = (wx0 + wx1) / 2.0

        if word_center_x > left_limit and word_center_x < right_limit:
            cleaned = _clean_context_word(text)
            if cleaned:
                selected_words.append(cleaned)

    if not selected_words:
        return ""

    context = " ".join(selected_words)
    context = _normalize_context(context)

    # Pulizia casi comuni:
    # "(prov." diventa "prov."
    # ") cap" diventa "cap"
    # ", in qualità di:" resta valido
    context = context.strip()

    context = re.sub(r"^\)+\s*", "", context)
    context = re.sub(r"^\(+\s*", "", context)
    context = re.sub(r"\s*\)+$", "", context)

    # Evita contesti composti solo da punteggiatura
    if not re.search(r"[A-Za-zÀ-ÿ0-9]", context):
        return ""

    return context.strip()


def _merge_adjacent_line_candidates(page, cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Dopo split/deduce, unisci segmenti di linea adiacenti sulla stessa riga se:
    - sono molto vicini in Y
    - gap piccolo
    - tra i due non c'è testo
    Serve per underline spezzati in più pezzi.
    """
    words = page.get_text("words")

    def has_text_between(cy: float, x0: float, x1: float) -> bool:
        for w in words:
            wx0, wy0, wx1, wy1, wt = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
            if not wt or wt.isspace():
                continue
            wcy = (wy0 + wy1) / 2.0
            if abs(wcy - cy) > 10.0:
                continue
            if wx1 >= x0 and wx0 <= x1:
                return True
        return False

    line_like = []
    others = []
    for c in cands:
        x0, y0, x1, y1 = c["bbox"]
        h = y1 - y0
        if c.get("placeholder") is None and h <= 16.0 and (x1 - x0) >= 10.0:
            line_like.append(c)
        else:
            others.append(c)

    if not line_like:
        return cands

    line_like.sort(key=lambda c: ((c["bbox"][1] + c["bbox"][3]) / 2.0, c["bbox"][0]))
    merged: List[Dict[str, Any]] = []
    y_tol = 2.5
    max_gap = 8.0

    for c in line_like:
        x0, y0, x1, y1 = c["bbox"]
        cy = (y0 + y1) / 2.0
        if not merged:
            merged.append(c)
            continue
        lx0, ly0, lx1, ly1 = merged[-1]["bbox"]
        lcy = (ly0 + ly1) / 2.0
        if abs(cy - lcy) <= y_tol and x0 <= lx1 + max_gap and not has_text_between(lcy, lx1, x0):
            # merge
            merged[-1]["bbox"] = (min(lx0, x0), min(ly0, y0), max(lx1, x1), max(ly1, y1))
        else:
            merged.append(c)

    return others + merged


def _extract_text_blanks(page) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        if _is_blank_token(text):
            cands.append(
                {
                    "kind": "text",
                    "placeholder": text.strip(),
                    "bbox": (float(x0), float(y0), float(x1), float(y1)),
                }
            )
    return cands


def _extract_inline_blanks_with_context(page) -> List[Dict[str, Any]]:
    """
    Estrae campi "inline" (underscore/dot/linee tratteggiate) anche quando
    sono attaccati al testo nello stesso span (es: 'Nome__________ nato a').
    Restituisce direttamente anche il contesto per riga (solo testo prima del campo).
    """
    cands: List[Dict[str, Any]] = []
    raw = page.get_text("rawdict")

    def union_bbox(bb: Tuple[float, float, float, float], cb: Tuple[float, float, float, float]):
        x0, y0, x1, y1 = bb
        cx0, cy0, cx1, cy1 = cb
        return (min(x0, cx0), min(y0, cy0), max(x1, cx1), max(y1, cy1))

    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            # Flatten chars in reading order (spans order + char order)
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    chars.append(ch)

            if not chars:
                continue

            context_buf = ""
            run_type = None  # "text" | "blank"
            run_bbox: Tuple[float, float, float, float] | None = None
            blank_count = 0

            def flush():
                nonlocal context_buf, run_type, run_bbox, blank_count
                if run_type == "blank" and run_bbox is not None and blank_count >= 3:
                    placeholder = "_" * int(max(5, min(80, blank_count)))
                    cands.append(
                        {
                            "kind": "inline",
                            "placeholder": placeholder,
                            "context": context_buf.strip(),
                            "bbox": run_bbox,
                        }
                    )
                    context_buf = ""
                elif run_type == "text":
                    # Normalizza spazi multipli
                    context_buf = re.sub(r"\s+", " ", context_buf).strip() + " "

                run_type = None
                run_bbox = None
                blank_count = 0

            for ch in chars:
                c = ch.get("c", "")
                cb = tuple(map(float, ch.get("bbox", (0, 0, 0, 0))))  # type: ignore[arg-type]

                if not c:
                    continue

                if c.isspace():
                    # Aggiungi spazio solo se siamo in testo e c'è già qualcosa
                    if run_type == "text" and context_buf and not context_buf.endswith(" "):
                        context_buf += " "
                    continue

                typ = "blank" if c in BLANK_CHARS else "text"

                if run_type is None:
                    run_type = typ
                    run_bbox = cb
                    blank_count = 1 if typ == "blank" else 0
                elif typ != run_type:
                    flush()
                    run_type = typ
                    run_bbox = cb
                    blank_count = 1 if typ == "blank" else 0
                else:
                    run_bbox = cb if run_bbox is None else union_bbox(run_bbox, cb)
                    if typ == "blank":
                        blank_count += 1

                if typ == "text":
                    # Pulisci eventuali blank inline dentro testo (raro a livello char)
                    if c in BLANK_CHARS:
                        continue
                    context_buf += c

            flush()

    return cands


def _extract_drawn_lines(page) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return cands

    for d in drawings:
        for item in d.get("items", []):
            if not item:
                continue
            op = item[0]
            if op == "l" and len(item) >= 3:
                p1 = item[1]
                p2 = item[2]
                x0, y0 = float(p1.x), float(p1.y)
                x1, y1 = float(p2.x), float(p2.y)
                if abs(y1 - y0) <= 0.8 and abs(x1 - x0) >= 40.0:
                    left, right = (x0, x1) if x0 <= x1 else (x1, x0)
                    top = min(y0, y1) - 1.0
                    bottom = max(y0, y1) + 1.0
                    cands.append(
                        {
                            "kind": "line",
                            "placeholder": None,
                            "bbox": (left, top, right, bottom),
                        }
                    )
    return cands


def _extract_merged_horizontal_segments(page) -> List[Dict[str, Any]]:
    """
    Per moduli che usano underline/leader disegnati come tanti segmenti orizzontali piccoli
    (es. spazi sottolineati), unisce i segmenti vicini in un singolo "campo".
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    segments: List[Tuple[float, float, float]] = []  # (y, x0, x1)
    for d in drawings:
        for item in d.get("items", []):
            if not item:
                continue
            op = item[0]
            if op != "l" or len(item) < 3:
                continue
            p1 = item[1]
            p2 = item[2]
            x0, y0 = float(p1.x), float(p1.y)
            x1, y1 = float(p2.x), float(p2.y)
            if abs(y1 - y0) > 1.2:
                continue
            length = abs(x1 - x0)
            if length < 6.0:
                continue
            left, right = (x0, x1) if x0 <= x1 else (x1, x0)
            y = (y0 + y1) / 2.0
            segments.append((y, left, right))

    if not segments:
        return []

    # Cluster per Y e unisci per X con gap piccolo.
    segments.sort(key=lambda s: (s[0], s[1]))
    y_tol = 6.0
    # Gap più piccolo per evitare di fondere campi vicini sulla stessa riga.
    max_gap = 12.0
    # Molti campi sono corti (es. prov., CAP): abbassiamo il minimo.
    min_merged_len = 6.0

    merged_fields: List[Dict[str, Any]] = []
    current_y = None
    current: List[Tuple[float, float, float]] = []

    # Prepara words per "ostacoli": se c'è testo tra due segmenti, non unirli.
    words = page.get_text("words")
    obstacle_y_tol = 8.0

    def has_obstacle(my: float, gx0: float, gx1: float) -> bool:
        for w in words:
            wx0, wy0, wx1, wy1, wt = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
            if not wt or wt.isspace():
                continue
            wcy = (wy0 + wy1) / 2.0
            if abs(wcy - my) > obstacle_y_tol:
                continue
            # word overlap or sits inside gap region
            if wx1 >= gx0 and wx0 <= gx1:
                return True
        return False

    def flush_cluster():
        nonlocal current
        if not current:
            return
        current.sort(key=lambda s: s[1])
        mx0 = current[0][1]
        mx1 = current[0][2]
        my = sum(s[0] for s in current) / len(current)
        for _, sx0, sx1 in current[1:]:
            gap_x0 = mx1
            gap_x1 = sx0
            # Se in mezzo c'è testo, non unire (campi distinti sulla stessa riga).
            if sx0 <= mx1 + max_gap and not has_obstacle(my, gap_x0, gap_x1):
                mx1 = max(mx1, sx1)
            else:
                if (mx1 - mx0) >= min_merged_len:
                    merged_fields.append(
                        {
                            "kind": "merged_line",
                            "placeholder": None,
                            "bbox": (mx0, my - 1.2, mx1, my + 1.2),
                        }
                    )
                mx0, mx1 = sx0, sx1
        if (mx1 - mx0) >= min_merged_len:
            merged_fields.append(
                {
                    "kind": "merged_line",
                    "placeholder": None,
                    "bbox": (mx0, my - 1.2, mx1, my + 1.2),
                }
            )
        current = []

    for seg in segments:
        y, x0, x1 = seg
        if current_y is None:
            current_y = y
            current = [seg]
            continue
        if abs(y - current_y) <= y_tol:
            current.append(seg)
            # media mobile per stabilizzare
            current_y = current_y * 0.8 + y * 0.2
        else:
            flush_cluster()
            current_y = y
            current = [seg]
    flush_cluster()

    return merged_fields


def _extract_image_based_lines(page) -> List[Dict[str, Any]]:
    """
    Fallback robusto: renderizza la pagina e trova linee orizzontali (underline/leader)
    anche quando non esistono come vettori (es. underline su spazi).

    Richiede: opencv-python + numpy.
    """
    cv2, np = _try_import_cv2()
    if cv2 is None or np is None:
        return []

    dpi = 200
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    w, h = pix.width, pix.height
    if w <= 0 or h <= 0:
        return []

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    # PyMuPDF tipicamente fornisce RGB
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    # Binarizza e isola tratti scuri
    bw = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        10,
    )

    # Estrai linee orizzontali
    kernel_w = max(10, w // 60)
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 1))
    horiz = cv2.erode(bw, horiz_kernel, iterations=1)
    horiz = cv2.dilate(horiz, horiz_kernel, iterations=1)

    contours, _ = cv2.findContours(horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    scale_x = float(page.rect.width) / float(w)
    scale_y = float(page.rect.height) / float(h)

    cands: List[Dict[str, Any]] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if ch > 12:
            continue
        if cw < 25:
            continue
        # bbox in coordinate PDF
        x0 = x * scale_x
        x1 = (x + cw) * scale_x
        y0 = y * scale_y
        y1 = (y + ch) * scale_y
        cands.append({"kind": "img_line", "placeholder": None, "bbox": (x0, y0, x1, y1)})

    return cands


def _split_line_candidates_by_text_obstacles(
    page,
    line_cands: List[Dict[str, Any]],
    min_len: float = 18.0,
) -> List[Dict[str, Any]]:
    """
    Se un underline è lungo e attraversa testo (es. '(prov.___)'), spezzalo.
    """
    words = page.get_text("words")
    out: List[Dict[str, Any]] = []
    for c in line_cands:
        x0, y0, x1, y1 = c["bbox"]
        cy = (y0 + y1) / 2.0
        obstacles: List[Tuple[float, float]] = []
        for w in words:
            wx0, wy0, wx1, wy1, wt = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
            if not wt or wt.isspace():
                continue
            wcy = (wy0 + wy1) / 2.0
            if abs(wcy - cy) > 10.0:
                continue
            # se il word sta dentro il tratto, aggiungilo come ostacolo
            if wx1 >= x0 and wx0 <= x1:
                obstacles.append((wx0 - 1.5, wx1 + 1.5))

        segments = _subtract_intervals((x0, x1), obstacles)
        for sx0, sx1 in segments:
            if (sx1 - sx0) < min_len:
                continue
            out.append({**c, "bbox": (sx0, y0, sx1, y1)})
    return out


def _extract_widgets(page) -> List[Dict[str, Any]]:
    """
    Se il PDF ha campi compilabili (AcroForm), PyMuPDF li espone come widgets.
    """
    cands: List[Dict[str, Any]] = []
    try:
        widgets = page.widgets() or []
    except Exception:
        return cands

    for w in widgets:
        try:
            r = w.rect  # type: ignore[attr-defined]
            x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
            cands.append({"kind": "widget", "placeholder": None, "bbox": (x0, y0, x1, y1)})
        except Exception:
            continue
    return cands


def _extract_drawn_rectangles(page) -> List[Dict[str, Any]]:
    """
    Molti moduli non usano underscore, ma BOX/RETTANGOLI disegnati (bordo del campo).
    Qui estraiamo rettangoli abbastanza "piatti" per essere campi testo / checkbox.
    """
    cands: List[Dict[str, Any]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return cands

    for d in drawings:
        for item in d.get("items", []):
            if not item:
                continue
            op = item[0]
            # In PyMuPDF i rettangoli possono comparire come 're' con un Rect
            if op == "re" and len(item) >= 2:
                r = item[1]
                x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                w = abs(x1 - x0)
                h = abs(y1 - y0)
                # campi testo lunghi o piccoli checkbox
                if (w >= 20.0 and 6.0 <= h <= 40.0) or (6.0 <= w <= 22.0 and 6.0 <= h <= 22.0):
                    cands.append({"kind": "rect", "placeholder": None, "bbox": (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))})
    return cands

def _sort_candidates_reading_order(candidates: List[Dict[str, Any]], y_tol: float = 8.0) -> List[Dict[str, Any]]:
            rows: List[List[Dict[str, Any]]] = []

            for c in sorted(candidates, key=lambda cc: ((cc["bbox"][1] + cc["bbox"][3]) / 2.0, cc["bbox"][0])):
                x0, y0, x1, y1 = c["bbox"]
                cy = (y0 + y1) / 2.0

                placed = False
                for row in rows:
                    row_cy = sum((r["bbox"][1] + r["bbox"][3]) / 2.0 for r in row) / len(row)
                    if abs(cy - row_cy) <= y_tol:
                        row.append(c)
                        placed = True
                        break

                if not placed:
                    rows.append([c])

            for row in rows:
                row.sort(key=lambda cc: cc["bbox"][0])

            rows.sort(key=lambda row: sum((r["bbox"][1] + r["bbox"][3]) / 2.0 for r in row) / len(row))

            ordered = []
            for row in rows:
                ordered.extend(row)

            return ordered

def _remove_fields_without_context(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Elimina dal JSON finale i campi che hanno contesto vuoto.
    Rinumerazione campo_id in ordine.
    """
    cleaned_fields = []

    for field in fields:
        context = str(field.get("contesto", "")).strip()

        if context:
            cleaned_fields.append(field)

    for i, field in enumerate(cleaned_fields, start=1):
        field["campo_id"] = i

    return cleaned_fields

def _clean_double_contexts_from_fields(
    fields: List[Dict[str, Any]],
    max_same_row_y_gap: float = 8.0,
    min_field_gap: float = 55.0,
) -> List[Dict[str, Any]]:
    """
    Pulisce contesti doppi dopo l'estrazione finale.
    Usa solo i campi già estratti nel JSON.

    Esempio:
    "nata/o a (prov." -> "prov."
    solo se il campo precedente è sulla stessa riga ed è distante.
    """

    def field_cy(field: Dict[str, Any]) -> float:
        cc = field["cc"]
        return (float(cc["y0"]) + float(cc["y1"])) / 2.0

    def clean_context_piece(context: str) -> str:
        context = context.strip()

        pieces = re.split(r"[()]", context)
        pieces = [p.strip(" ,;:") for p in pieces]
        pieces = [p for p in pieces if p and re.search(r"[A-Za-zÀ-ÿ0-9]", p)]

        if not pieces:
            return context

        return pieces[-1].strip()

    for i in range(1, len(fields)):
        prev_field = fields[i - 1]
        curr_field = fields[i]

        if prev_field.get("pagina") != curr_field.get("pagina"):
            continue

        prev_cc = prev_field["cc"]
        curr_cc = curr_field["cc"]

        same_row = abs(field_cy(prev_field) - field_cy(curr_field)) <= max_same_row_y_gap
        horizontal_gap = float(curr_cc["x0"]) - float(prev_cc["x1"])

        context = str(curr_field.get("contesto", "")).strip()

        suspicious_context = (
            "(" in context
            or ")" in context
            or len(context.split()) >= 3
        )

        if same_row and horizontal_gap >= min_field_gap and suspicious_context:
            curr_field["contesto"] = clean_context_piece(context)

    return fields

def _extract_dotted_leaders(page) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []

    DOT_CHARS = {".", "…", "⋯", "·", "∙", "•", "‧", "⁃", "˙", "。", "｡", "﹒", "．"," . . . ","..."}

    try:
        raw = page.get_text("rawdict")
    except Exception:
        return cands

    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            items = []

            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    char = ch.get("c", "")
                    x0, y0, x1, y1 = map(float, ch.get("bbox", (0, 0, 0, 0)))

                    if char in DOT_CHARS:
                        items.append({"type": "dot", "x0": x0, "y0": y0, "x1": x1, "y1": y1, "char": char})
                    elif char.strip():
                        items.append({"type": "text", "x0": x0, "y0": y0, "x1": x1, "y1": y1, "char": char})

            if not items:
                continue

            items.sort(key=lambda d: d["x0"])

            groups = []
            current = []

            for item in items:
                if item["type"] == "dot":
                    if not current:
                        current = [item]
                    else:
                        gap = item["x0"] - current[-1]["x1"]
                        if gap <= 22.0:
                            current.append(item)
                        else:
                            if len(current) >= 4:
                                groups.append(current)
                            current = [item]
                else:
                    if len(current) >= 4:
                        groups.append(current)
                    current = []

            if len(current) >= 4:
                groups.append(current)

            for group in groups:
                x0 = min(d["x0"] for d in group)
                y0 = min(d["y0"] for d in group)
                x1 = max(d["x1"] for d in group)
                y1 = max(d["y1"] for d in group)

                if x1 - x0 < 10:
                    continue

                cands.append({
                    "kind": "dotted_leader",
                    "placeholder": "." * min(len(group), 80),
                    "bbox": (x0, y0, x1, y1),
                })

    return cands

def main() -> None:
    fitz = _try_import_fitz()

    if not os.path.exists(pdf_path):
        raise SystemExit(f"PDF non trovato: {pdf_path}")

    doc = fitz.open(pdf_path)
    all_fields: List[Dict[str, Any]] = []
    field_id = 1

    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        words = page.get_text("words")
        lines = _cluster_words_into_lines(words)

        candidates = []
        inline = _extract_inline_blanks_with_context(page)
        candidates.extend(
            [
                {"kind": c["kind"], "placeholder": c["placeholder"], "bbox": c["bbox"], "context": c.get("context", "")}
                for c in inline
            ]
        )
        candidates.extend(_extract_text_blanks(page))
        candidates.extend(_extract_dotted_leaders(page))
        candidates.extend(_extract_widgets(page))
        candidates.extend(_extract_drawn_rectangles(page))
        candidates.extend(_extract_drawn_lines(page))
        merged_vec_lines = _extract_merged_horizontal_segments(page)
        candidates.extend(_split_line_candidates_by_text_obstacles(page, merged_vec_lines))

        # Fallback: linee trovate via rendering + CV, poi split con ostacoli testuali
        img_lines = _extract_image_based_lines(page)
        candidates.extend(_split_line_candidates_by_text_obstacles(page, img_lines))
        candidates = _merge_adjacent_line_candidates(page, candidates)
        candidates = _dedupe_candidates(candidates)

        candidates = _sort_candidates_reading_order(candidates)

        # Ordina sempre i campi in ordine di lettura (alto->basso, sinistra->destra)
        # così il JSON segue la presenza sul documento.

        # Per calcolare "contesto precedente", serve sapere quali campi stanno sulla stessa linea
        # (usiamo solo bbox, poi _context_for_field seleziona quello giusto).
        all_bboxes = [c["bbox"] for c in candidates]

        for c in candidates:
            x0, y0, x1, y1 = c["bbox"]
            width = x1 - x0
            placeholder = c.get("placeholder") or _make_campo_from_width(width)
            context = (c.get("context") or "").strip() or _context_for_field(lines, c["bbox"], all_bboxes)
            context = _normalize_context(context)

            all_fields.append(
                {
                    "campo_id": field_id,
                    "contesto": context,
                    "campo": placeholder,
                    "cc": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    "pagina": page_index + 1,
                }
            )
            field_id += 1

    all_fields = _clean_double_contexts_from_fields(all_fields)
    all_fields = _remove_fields_without_context(all_fields)
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(all_fields, f, ensure_ascii=False, indent=2)

    print(f"OK: trovati {len(all_fields)} campi")
    print(f"JSON: {out_json_path}")


if __name__ == "__main__":
    main()
