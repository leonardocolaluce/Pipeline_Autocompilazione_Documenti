"""
test_pipeline.py
Test funzionali della pipeline (loaders, preprocessing, postprocessing).
"""

import sys
import os
import numpy as np
from PIL import Image
from pathlib import Path

# Aggiunge la root della pipeline al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders.pdf_loader import load_pdf
from loaders.scan_loader import load_scanned_pdf
from loaders.word_loader import load_word
from preprocessing.image_processor import preprocess_image
from postprocessing.block_parser import parse_blocks

# ---------------------------------------------------------------------------
# Percorsi file di test (file reali dal progetto)
# ---------------------------------------------------------------------------
SAMPLE_DIR = Path(__file__).parent.parent.parent / "Sample" / "extracted" / "Gara Camerino"
PDF_NATIVE  = SAMPLE_DIR / "Bando di gara_Disciplinare_all_Cimitero Cap (1).pdf"
PDF_SCAN    = SAMPLE_DIR / "CHIARIMENTI NN 10-11_02-10-24.PDF (1).pdf"
WORD_FILE   = SAMPLE_DIR / "All A - Domanda partecipazione.docx"
DOC_FILE    = SAMPLE_DIR / "All A - Domanda partecipazione.doc"  # opzionale


def _separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def test_pdf_native():
    _separator("TEST: PDF NATIVO")
    assert PDF_NATIVE.exists(), f"File non trovato: {PDF_NATIVE}"
    blocks = load_pdf(str(PDF_NATIVE))
    assert len(blocks) > 0, "Nessun blocco estratto dal PDF nativo"
    b = blocks[0]
    assert "text" in b and "bbox" in b and "page" in b and "confidence" in b
    assert len(b["bbox"]) == 4
    print(f"  [OK] {len(blocks)} blocchi estratti")
    print(f"  Primo blocco: {b['text'][:60]!r}")
    return blocks


def test_pdf_native_postprocessing(blocks):
    _separator("TEST: POST-PROCESSING SU PDF NATIVO")
    parsed = parse_blocks(blocks)
    assert len(parsed) > 0
    # Verifica ordinamento per pagina e y
    pages = [b["page"] for b in parsed]
    assert pages == sorted(pages), "I blocchi non sono ordinati per pagina"
    print(f"  [OK] {len(parsed)} blocchi dopo post-processing")
    print(f"  Primo blocco pulito: {parsed[0]['text'][:60]!r}")
    return parsed


def test_word():
    _separator("TEST: WORD (.docx)")
    assert WORD_FILE.exists(), f"File non trovato: {WORD_FILE}"
    blocks = load_word(str(WORD_FILE))
    assert len(blocks) > 0, "Nessun blocco estratto dal file Word"
    b = blocks[0]
    assert "text" in b and "bbox" in b and "style" in b
    print(f"  [OK] {len(blocks)} blocchi estratti")
    print(f"  Primo blocco: {b['text'][:60]!r}  (stile: {b['style']})")
    return blocks


def test_word_postprocessing(blocks):
    _separator("TEST: POST-PROCESSING SU WORD")
    parsed = parse_blocks(blocks, merge_nearby=True)
    assert len(parsed) > 0
    print(f"  [OK] {len(parsed)} blocchi dopo post-processing con merge")
    return parsed


def test_preprocessing_synthetic():
    _separator("TEST: PRE-PROCESSING (immagine sintetica)")
    # Crea immagine sintetica: sfondo bianco con testo simulato
    img_array = np.ones((400, 600, 3), dtype=np.uint8) * 255
    # Aggiunge rumore
    noise = np.random.randint(0, 30, img_array.shape, dtype=np.uint8)
    img_array = np.clip(img_array.astype(np.int16) - noise, 0, 255).astype(np.uint8)
    # Aggiunge rettangoli neri (simulano testo)
    img_array[50:70, 50:300] = 0
    img_array[100:120, 50:250] = 0
    img_array[150:170, 50:200] = 0

    pil_img = Image.fromarray(img_array)
    result = preprocess_image(pil_img)

    assert result is not None
    assert len(result.shape) == 2, "Output deve essere grayscale (2D)"
    assert result.dtype == np.uint8
    # Verifica che sia binarizzata (solo 0 e 255)
    unique_vals = set(np.unique(result))
    assert unique_vals.issubset({0, 255}), f"Valori non binari trovati: {unique_vals}"
    print(f"  [OK] Pre-processing completato. Shape: {result.shape}, valori unici: {unique_vals}")


def test_preprocessing_numpy():
    _separator("TEST: PRE-PROCESSING (array numpy diretto)")
    img_array = np.random.randint(180, 255, (300, 400, 3), dtype=np.uint8)
    result = preprocess_image(img_array)
    assert result is not None
    assert len(result.shape) == 2
    print(f"  [OK] Pre-processing su array numpy. Shape: {result.shape}")


def test_block_parser_filtering():
    _separator("TEST: BLOCK PARSER — filtraggio rumore")
    dirty_blocks = [
        {"text": "  ", "bbox": [0, 0, 10, 10], "page": 1, "confidence": 0.9},
        {"text": "!!!", "bbox": [0, 10, 10, 10], "page": 1, "confidence": 0.5},
        {"text": "a", "bbox": [0, 20, 10, 10], "page": 1, "confidence": 0.8},
        {"text": "Documento valido", "bbox": [0, 30, 100, 10], "page": 1, "confidence": 1.0},
        {"text": "Altra riga", "bbox": [0, 50, 100, 10], "page": 1, "confidence": 1.0},
    ]
    parsed = parse_blocks(dirty_blocks)
    texts = [b["text"] for b in parsed]
    assert "  " not in texts
    assert "!!!" not in texts
    assert "a" not in texts
    assert "Documento valido" in texts
    print(f"  [OK] Blocchi rumorosi rimossi. Rimasti: {texts}")


def test_block_parser_merge():
    _separator("TEST: BLOCK PARSER — merge blocchi vicini")
    blocks = [
        {"text": "Ciao", "bbox": [0, 10, 30, 12], "page": 1, "confidence": 0.95},
        {"text": "mondo", "bbox": [35, 11, 40, 12], "page": 1, "confidence": 0.90},
        {"text": "Riga due", "bbox": [0, 40, 80, 12], "page": 1, "confidence": 1.0},
    ]
    merged = parse_blocks(blocks, merge_nearby=True)
    assert len(merged) == 2
    assert "Ciao mondo" in merged[0]["text"]
    print(f"  [OK] Merge riuscito. Blocchi: {[b['text'] for b in merged]}")


def test_doc_legacy():
    _separator("TEST: WORD LEGACY (.doc) — conversione automatica")
    if not DOC_FILE.exists():
        print(f"  [SKIP] File .doc non trovato: {DOC_FILE}")
        return

    blocks = load_word(str(DOC_FILE))
    assert len(blocks) > 0, "Nessun blocco estratto dal file .doc"
    b = blocks[0]
    assert "text" in b and "bbox" in b and "style" in b
    print(f"  [OK] {len(blocks)} blocchi estratti dal .doc")
    print(f"  Primo blocco: {b['text'][:60]!r}  (stile: {b['style']})")


def test_scan_pdf():
    _separator("TEST: PDF SCANSIONATO — estrazione immagini")
    if not PDF_SCAN.exists():
        print(f"  [SKIP] File non trovato: {PDF_SCAN}")
        return

    pages = load_scanned_pdf(str(PDF_SCAN))
    assert len(pages) > 0
    page_num, img = pages[0]
    assert isinstance(img, Image.Image)
    assert page_num == 1
    w, h = img.size
    print(f"  [OK] {len(pages)} pagine estratte. Prima pagina: {w}x{h}px")

    # Pre-elabora la prima pagina
    processed = preprocess_image(img)
    assert processed is not None
    print(f"  [OK] Pre-processing prima pagina: shape {processed.shape}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    errors = []

    tests = [
        ("pdf_native", test_pdf_native),
        ("preprocessing_synthetic", test_preprocessing_synthetic),
        ("preprocessing_numpy", test_preprocessing_numpy),
        ("block_parser_filtering", test_block_parser_filtering),
        ("block_parser_merge", test_block_parser_merge),
        ("word", test_word),
        ("doc_legacy", test_doc_legacy),
        ("scan_pdf", test_scan_pdf),
    ]

    pdf_blocks = None
    word_blocks = None

    for name, fn in tests:
        try:
            result = fn()
            if name == "pdf_native":
                pdf_blocks = result
            elif name == "word":
                word_blocks = result
        except Exception as e:
            print(f"  [ERRORE] {e}")
            errors.append(name)

    # Test post-processing concatenati
    if pdf_blocks:
        try:
            test_pdf_native_postprocessing(pdf_blocks)
        except Exception as e:
            print(f"  [ERRORE] post-processing PDF: {e}")
            errors.append("pdf_postprocessing")

    if word_blocks:
        try:
            test_word_postprocessing(word_blocks)
        except Exception as e:
            print(f"  [ERRORE] post-processing Word: {e}")
            errors.append("word_postprocessing")

    print(f"\n{'='*60}")
    if errors:
        print(f"  FALLITI: {errors}")
        sys.exit(1)
    else:
        print("  TUTTI I TEST PASSATI")
    print('='*60)
