import json
import os
import sys
import win32com.client as win32


# ─── COSTANTI COM ────────────────────────────────────────────────────────────
MSO_TEXT_ORIENTATION_HORIZONTAL = 1
WD_REL_H_PAGE = 1   # wdRelativeHorizontalPositionPage  (dal bordo fisico del foglio)
WD_REL_V_PAGE = 1   # wdRelativeVerticalPositionPage    (dal bordo fisico del foglio)

MIN_WIDTH  = 60
MIN_HEIGHT = 14
FONT_SIZE  = 10
# Vertical shift (in points) applied to every textbox.
# Default: 0. Set WORD_Y_OFFSET=-10 (or any int/float) to move text up.
def _y_offset() -> float:
    raw = os.getenv("WORD_Y_OFFSET", "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0

Y_OFFSET = _y_offset()
# ─────────────────────────────────────────────────────────────────────────────


# ═════════════════════════════════════════════════════════════════════════════
#  STEP 1 – RILEVAMENTO DOCUMENTO (solo print informativo, non usato per calcoli)
# ═════════════════════════════════════════════════════════════════════════════

def rileva_documento(doc) -> None:
    setup = doc.Sections(1).PageSetup

    page_w = setup.PageWidth
    page_h = setup.PageHeight
    m_l    = setup.LeftMargin
    m_r    = setup.RightMargin
    m_t    = setup.TopMargin
    m_b    = setup.BottomMargin
    h_dist = setup.HeaderDistance
    f_dist = setup.FooterDistance

    w_in = page_w / 72
    h_in = page_h / 72
    if   abs(w_in - 8.268) < 0.05 and abs(h_in - 11.693) < 0.05: fmt = "A4 verticale"
    elif abs(w_in - 11.693) < 0.05 and abs(h_in - 8.268)  < 0.05: fmt = "A4 orizzontale"
    elif abs(w_in - 8.5)   < 0.05 and abs(h_in - 11.0)   < 0.05: fmt = "US Letter verticale"
    elif abs(w_in - 11.0)  < 0.05 and abs(h_in - 8.5)    < 0.05: fmt = "US Letter orizzontale"
    else: fmt = "formato personalizzato"

    has_header = doc.Sections(1).Headers(1).Exists
    has_footer = doc.Sections(1).Footers(1).Exists

    sep = "=" * 60
    print(f"\n{sep}")
    print("  RILEVAMENTO DOCUMENTO")
    print(sep)
    print(f"  Formato pagina   : {page_w:.2f} x {page_h:.2f} pt  ({fmt})")
    print(f"  Margini (pt)     : sx={m_l:.2f}  dx={m_r:.2f}  top={m_t:.2f}  bot={m_b:.2f}")
    print(f"  Header dal bordo : {h_dist:.2f} pt  |  Footer dal bordo: {f_dist:.2f} pt")
    print(f"  Header attivo    : {'SI' if has_header else 'NO'}  |  Footer attivo: {'SI' if has_footer else 'NO'}")
    print(f"  Area testo       : {page_w - m_l - m_r:.2f} x {page_h - m_t - m_b:.2f} pt")
    print(f"\n  Strategia posizionamento: PAGE-RELATIVE")
    print(f"  Le bbox JSON sono coordinate assolute dal bordo fisico del foglio.")
    print(f"  Vengono passate direttamente a Word senza alcuna conversione.")
    print(sep)


# ═════════════════════════════════════════════════════════════════════════════
#  STEP 2 – COMPILAZIONE
# ═════════════════════════════════════════════════════════════════════════════

def carica_json(json_path: str) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def campi_da_compilare(data: dict) -> list:
    return [
        row for row in data.get("rows", [])
        if row.get("item_type") in {"field", "table_cell"}
        and str(row.get("answer", "")).strip()
        and row.get("bbox")
    ]


def aggiungi_textbox(doc, page: int, x1: float, y1: float,
                     width: float, height: float,
                     testo: str) -> None:
    """
    Posiziona la textbox con coordinate assolute di pagina (PAGE-RELATIVE).
    x1, y1 sono punti dal bordo fisico del foglio (angolo top-left),
    esattamente come li fornisce il JSON. Nessuna conversione necessaria.
    """
    WD_GOTO_PAGE = 1
    WD_GOTO_ABSOLUTE = 1

    anchor = doc.GoTo(
        What=WD_GOTO_PAGE,
        Which=WD_GOTO_ABSOLUTE,
        Count=int(page)
    )

    shape = doc.Shapes.AddTextbox(
        MSO_TEXT_ORIENTATION_HORIZONTAL,
        x1, y1,        # posizione iniziale (verra' confermata sotto)
        width, height,
        anchor
    )

    # Posizionamento assoluto: dal bordo fisico del foglio
    shape.RelativeHorizontalPosition = WD_REL_H_PAGE
    shape.RelativeVerticalPosition   = WD_REL_V_PAGE
    shape.Left = x1
    try:
        print(
            f"[WRITER] WORD_Y_OFFSET_env={os.getenv('WORD_Y_OFFSET')} "
            f"Y_OFFSET_const={Y_OFFSET} y1={y1} top={y1 + Y_OFFSET}",
            flush=True,
        )
    except Exception:
        pass
    shape.Top  = y1 + Y_OFFSET

    # Nessun bordo, nessuno sfondo
    shape.Line.Visible = False
    shape.Fill.Visible = False

    # Margini interni a zero
    tf = shape.TextFrame
    tf.MarginLeft   = 0
    tf.MarginRight  = 0
    tf.MarginTop    = 0
    tf.MarginBottom = 0

    # Testo
    tr = tf.TextRange
    tr.Text = testo

    font_size = FONT_SIZE

    tr.Font.Bold = False
    tr.Font.Color = 0x000000

    # Riduce il font finché il testo entra
    while font_size >= 4:
        tr.Font.Size = font_size

        try:
            if tr.BoundWidth <= width and tr.BoundHeight <= height:
                break
        except:
            break

        font_size -= 1


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def compila_word(word_path: str, json_path: str, output_path: str) -> dict:

    print(f"\nCarico JSON: {json_path}")
    data  = carica_json(json_path)
    campi = campi_da_compilare(data)

    if not campi:
        print("Nessun campo con risposta trovato nel JSON. Uscita.")
        return {"output_path": os.path.abspath(word_path), "replaced_count": 0}
    print(f"Trovati {len(campi)} campi da compilare.")

    print(f"\nApro Word: {word_path}")
    word = win32.gencache.EnsureDispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    word.AutomationSecurity = 3
    doc = word.Documents.Open(
        FileName=os.path.abspath(word_path),
        ConfirmConversions=False,
        ReadOnly=False,
        AddToRecentFiles=False,
        OpenAndRepair=False,
        NoEncodingDialog=True
    )

    try:
        # STEP 1: rileva e stampa geometria documento
        rileva_documento(doc)

        # STEP 2: inserisce textbox con coordinate assolute di pagina
        print("\nCompilo i campi:")
        for campo in campi:
            bbox = campo["bbox"]

            if campo.get("item_type") == "table_cell":
                x1, y1, w, h = bbox
                width = w
                height = h
            else:
                x1, y1, x2, y2 = bbox
                width = max(x2 - x1, MIN_WIDTH)
                height = max(y2 - y1, MIN_HEIGHT)
            testo  = campo["answer"]
            label  = campo.get("label", "")[:45]

            page = int(campo.get("page", 1))
            aggiungi_textbox(doc, page, x1, y1, width, height, testo)

            print(f'  OK "{label}"')
            print(f'     risposta : "{testo}"')
            print(f'     bbox     : x={x1:.1f}  y={y1:.1f}  w={width:.1f}  h={height:.1f} pt')

        # STEP 3: salva come nuovo file, originale intatto
        doc.SaveAs(os.path.abspath(output_path))
        return {"output_path": os.path.abspath(output_path), "replaced_count": len(campi)}
        print(f"\nSalvato come: {output_path}")
        print("Originale NON modificato.")

    except Exception as exc:
        print(f"\n[ERRORE] {exc}")
        raise

    finally:
        word.Quit()

from pathlib import Path

def write_docx_from_mapping(source_docx: Path, mapping_path: Path, output_docx: Path) -> dict:
    return compila_word(str(source_docx), str(mapping_path), str(output_docx))

def write_docx_preview_from_answers_json(source_docx: Path, answers_json: Path, output_docx: Path, color_hex: str = "0000FF") -> dict:
    return compila_word(str(source_docx), str(answers_json), str(output_docx))
