import json
import shutil
import sys
import argparse
from pathlib import Path

import pdfplumber

from loaders.pdf_loader import load_pdf
from loaders.scan_loader import load_scanned_pdf
from loaders.word_loader import load_word, convert_doc_to_docx, convert_word_to_pdf
from preprocessing.image_processor import preprocess_image
from postprocessing.block_parser import parse_blocks
import subprocess
from postprocessing import extract_pdf_fields 
from postprocessing.checkbox_extractor import extract_checkboxes, extract_checkboxes_from_docx
from postprocessing.table_extractor import extract_tables
from postprocessing.annotation_renderer import render_annotations_from_m1
from postprocessing.pdf_table_layout import enrich_tables_with_pdf_layout
from ocr.ocr_engine import run_ocr


# ==============================================================================
#  CONFIGURAZIONE DIRETTA 
# ==============================================================================

INPUT_FILE   = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_2\pipeline_2\output\documento_compilato_finale.docx"      # Es: r"C:\Documenti\mio_file.pdf"  oppure  r"C:\Documenti\mio_file.doc"
OUTPUT_PATH  = None      # None = salva automaticamente in output/<nome_file>.json
MERGE_NEARBY = False     # True = unisce blocchi testuali vicini sulla stessa riga

# ==============================================================================


# ---------------------------------------------------------------------------
# Rilevamento tipo file
# ---------------------------------------------------------------------------

def detect_file_type(file_path: str) -> str:
    """
    Determina il tipo di documento.

    Returns:
        "pdf_native" | "pdf_scanned" | "word"
    """
    suffix = Path(file_path).suffix.lower()

    if suffix in (".docx", ".doc"):
        return "word"

    if suffix == ".pdf":
        return _classify_pdf(file_path)

    raise ValueError(f"Formato non supportato: '{suffix}'. Usa PDF, DOCX o DOC.")


def _classify_pdf(file_path: str) -> str:
    """Restituisce 'pdf_native' se il PDF contiene testo estraibile, altrimenti 'pdf_scanned'."""
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if len(text.strip()) > 20:
                    return "pdf_native"
    except Exception:
        pass
    return "pdf_scanned"


# ---------------------------------------------------------------------------
# Pipeline principale
# ---------------------------------------------------------------------------

def process(
    file_path: str,
    output_path: str = None,
    merge_nearby: bool = False,
    output_dir: str | None = None,
) -> list:
    """
    Esegue l'intera pipeline sul documento indicato.

    Args:
        file_path:    Percorso al file da elaborare.
        output_path:  Percorso JSON di output (default: output/<nome_file>.json).
        merge_nearby: Se True, unisce blocchi vicini nella stessa riga.

    Returns:
        Lista di blocchi elaborati.
    """
    file_path = str(file_path)

    if not Path(file_path).exists():
        print(f"[ERRORE] File non trovato: {file_path}")
        sys.exit(1)

    file_type = detect_file_type(file_path)
    print(f"[INFO] Tipo rilevato: {file_type}")

    blocks = []
    tmp_dir = None          # cartella temporanea per conversione .doc
    docx_path = file_path   # percorso .docx da usare per tabelle
    render_pdf_path = None
    render_cleanup_dir = None

    if file_type == "pdf_native":
        render_pdf_path = file_path
        raw = load_pdf(file_path)
        print(f"[INFO] Estratti {len(raw)} blocchi grezzi dal PDF nativo.")
        blocks = parse_blocks(raw, merge_nearby=merge_nearby)

    elif file_type == "word":
        # Se è un .doc, converte UNA SOLA VOLTA e riusa il .docx per tutto
        if Path(file_path).suffix.lower() == ".doc":
            print(f"[INFO] File .doc rilevato — conversione in .docx in corso...")
            docx_path = convert_doc_to_docx(file_path)
            tmp_dir = str(Path(docx_path).parent)
            print(f"[INFO] Conversione completata: {docx_path}")
        try:
            post_dir = Path(__file__).resolve().parent / "postprocessing"
            pdf_fixed_path = post_dir / "input.pdf"
            script_convert = post_dir / "convert_docx_to_pdf.py"
            subprocess.run(
                [sys.executable, str(script_convert), "--input-docx", str(Path(docx_path).resolve()), "--out-pdf", str(pdf_fixed_path)],
                check=True,
            )
            render_pdf_path = str(pdf_fixed_path)
            render_cleanup_dir = None
            print(f"[INFO] PDF di appoggio per annotazioni: {render_pdf_path}")
        except Exception:
            render_pdf_path = None

        raw = load_word(docx_path)
        print(f"[INFO] Estratti {len(raw)} blocchi grezzi dal file Word.")
        blocks = parse_blocks(raw, merge_nearby=merge_nearby)

    elif file_type == "pdf_scanned":
        render_pdf_path = file_path
        print(f"[INFO] PDF scansionato rilevato. Avvio OCR con PaddleOCR (Python 3.11)...")
        blocks = run_ocr(file_path)
        print(f"[INFO] OCR completato. Estratti {len(blocks)} blocchi.")

    # --- Rilevamento campi da compilare (NUOVO: PDF-based) ---
    from pathlib import Path as _Path
    import subprocess as _subprocess

    post_dir = _Path(__file__).resolve().parent / "postprocessing"
    script_convert = post_dir / "convert_docx_to_pdf.py"
    script_extract = post_dir / "extract_pdf_fields.py"
    script_preview = post_dir / "render_boxes_preview.py"

    pdf_fixed_path = post_dir / "input.pdf"
    campi_pdf_path = post_dir / "campi_pdf.json"
    pdf_fixed_path.unlink(missing_ok=True)
    campi_pdf_path.unlink(missing_ok=True)


    # 1) prepara input.pdf
    if file_type == "word":
        _subprocess.run(
            [sys.executable, str(script_convert), "--input-docx", str(_Path(docx_path).resolve()), "--out-pdf", str(pdf_fixed_path)],
            check=True,
        )
        render_pdf_path = str(pdf_fixed_path)

    else:
        # pdf_native / pdf_scanned: copia il pdf sorgente su postprocessing/input.pdf
        src_pdf = _Path(file_path).resolve()
        shutil.copy2(src_pdf, pdf_fixed_path)

    # 2) estrai campi su campi_pdf.json
    _subprocess.run([sys.executable, str(script_extract)], check=True)

    # 3) converti campi_pdf.json nel formato fields atteso dal resto della pipeline (e da M2)
    if campi_pdf_path.exists():
        raw_fields = json.loads(campi_pdf_path.read_text(encoding="utf-8"))
    else:
        raw_fields = []

    fields = []
    for item in raw_fields:
        cc = item.get("cc") or {}
        bbox = [float(cc["x0"]), float(cc["y0"]), float(cc["x1"]), float(cc["y1"])]
        context = str(item.get("contesto", "")).strip()
        placeholder = str(item.get("campo", "")).strip()
        page = int(item.get("pagina", 1))
        fields.append(
            {
                "campo": context,              # label per M2
                "valore": "",
                "placeholder": placeholder,    # underscore “linea”
                "contesto": context,
                "contesto_riga": context,
                "contesto_sopra": "",
                "bbox": bbox,
                "page": page,
            }
        )

    da_compilare = len(fields) > 0

    # 4) preview immagini con box
    #_subprocess.run([sys.executable, str(script_preview)], check=True)


    if da_compilare:
        print(f"[INFO] Documento DA COMPILARE — campi rilevati: {len(fields)}")
    else:
        print(f"[INFO] Documento NON da compilare (nessun campo trovato)")

    # --- Salvataggio output ---
    if output_dir:
        output_dir = Path(output_dir)
    elif output_path:
        output_dir = Path(output_path).resolve().parent
    else:
        output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(file_path).stem
    compilare_suffix = "_DA_COMPILARE" if da_compilare else ""

    if output_path is None:
        output_path = str(output_dir / f"{stem}{compilare_suffix}.json")

    print(f"[INFO] Salvataggio blocchi DISABILITATO: {output_path}")

    print(f"[INFO] Blocchi totali estratti: {len(blocks)}")

    checkboxes = []
    tables = []

    # --- Salvataggio JSON campi DISABILITATO ---
    if da_compilare:
        fields_path = str(output_dir / f"{stem}_CAMPI.json")
        print(f"[INFO] Salvataggio CAMPI disabilitato: {fields_path}")


        # Slim view (per LLM/debug): solo campi essenziali
        fields_slim_path = str(output_dir / f"{stem}_CAMPI_SLIM.json")
        slim_rows = [
            {
                "field_id": f"field:{i}",
                "label": str(item.get("campo", "")).strip(),
                "page": item.get("page"),
                "bbox": item.get("bbox"),
            }
            for i, item in enumerate(fields)
        ]
        with open(fields_slim_path, "w", encoding="utf-8") as f:
            json.dump({"base_name": stem, "item_count": len(slim_rows), "rows": slim_rows}, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Campi slim salvati in: {fields_slim_path}")

    # --- Estrazione checkbox (solo per file Word) ---
    if file_type == "word":
        checkboxes = extract_checkboxes(blocks)
        if not checkboxes:
            checkboxes = extract_checkboxes_from_docx(docx_path)
        if checkboxes:
            checkbox_path = str(output_dir / f"{stem}_CHECKBOX.json")
            with open(checkbox_path, "w", encoding="utf-8") as f:
                json.dump(checkboxes, f, ensure_ascii=False, indent=2)
            print(f"[INFO] Checkbox salvate in: {checkbox_path} ({len(checkboxes)} trovate)")
        else:
            print("[INFO] Nessuna checkbox trovata nel documento.")

    # --- Estrazione tabelle (Word + PDF nativo) ---
    tables_path = None
    if file_type in ("word", "pdf_native"):
        tables_input = docx_path if file_type == "word" else file_path
        tables = extract_tables(tables_input, blocks)
        if tables and render_pdf_path:
            enrich_tables_with_pdf_layout(render_pdf_path, tables)
        if tables:
            tables_path = str(output_dir / f"{stem}_TABELLE.json")
            with open(tables_path, "w", encoding="utf-8") as f:
                json.dump(tables, f, ensure_ascii=False, indent=2)
            print(f"[INFO] Tabelle salvate in: {tables_path} ({len(tables)} tabelle trovate)")
        else:
            print(f"[INFO] Nessuna tabella trovata nel documento.")


    # --- Render immagini pagina con box rossi (NUOVO) ---
    if render_pdf_path:
        try:
            post_dir = Path(__file__).resolve().parent / "postprocessing"
            script_preview = post_dir / "render_boxes_preview.py"
            annot_dir = str(output_dir / f"{stem}_ANNOTAZIONI")

            preview_cmd = [sys.executable, str(script_preview), "--out-dir", annot_dir]
            if tables_path and Path(tables_path).exists():
                preview_cmd.extend(["--tables-json", tables_path])

            subprocess.run(preview_cmd, check=True)
            print(f"[INFO] Immagini annotate salvate in: {annot_dir}")
        except Exception as exc:
            print(f"[WARN] Render annotazioni M1 fallito: {exc}")


    # --- Pulizia cartella temporanea .doc ---
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if render_cleanup_dir:
        shutil.rmtree(render_cleanup_dir, ignore_errors=True)

    return blocks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline di estrazione blocchi testuali da documenti Word/PDF."
    )
    parser.add_argument("file", nargs="?", help="Percorso al documento da elaborare (PDF, DOCX o DOC).")
    parser.add_argument(
        "--output", "-o",
        help="Percorso del file JSON di output. Default: output/<nome_file>.json",
        default=None,
    )
    parser.add_argument(
        "--merge", "-m",
        action="store_true",
        help="Unisce blocchi testuali vicini sulla stessa riga.",
    )
    return parser


if __name__ == "__main__":
    # --- Risolvi sorgente: configurazione diretta oppure argomenti CLI ---
    if INPUT_FILE.strip():
        # Avvio diretto dall'editor: usa le variabili configurate in cima al file
        target_file  = INPUT_FILE.strip()
        target_output = OUTPUT_PATH
        target_merge  = MERGE_NEARBY
    else:
        # Avvio da terminale: leggi gli argomenti
        parser = _build_parser()
        args = parser.parse_args()

        if not args.file:
            print("[ERRORE] Nessun file specificato.")
            print("  → Imposta INPUT_FILE in questo script, oppure passa il file come argomento:")
            print("     python main.py <percorso_file>")
            sys.exit(1)

        target_file   = args.file
        target_output = args.output
        target_merge  = args.merge

    result = process(target_file, output_path=target_output, merge_nearby=target_merge)

    if isinstance(result, list) and result and isinstance(result[0], dict):
        print(f"\n--- Anteprima primi 5 blocchi ---")
        for block in result[:5]:
            print(f"  [p.{block['page']}] {block['text'][:80]!r}")
