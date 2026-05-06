from typing import List, Tuple
from PIL import Image
import fitz  # pymupdf


TARGET_DPI = 300
_SCALE = TARGET_DPI / 72  


def load_scanned_pdf(file_path: str) -> List[Tuple[int, Image.Image]]:
    """
    Estrae ogni pagina del PDF come immagine PIL a 300 DPI.

    Args:
        file_path: Percorso al file PDF scansionato.

    Returns:
        Lista di tuple (page_number, PIL.Image) — una per pagina.
    """
    pages: List[Tuple[int, Image.Image]] = []
    matrix = fitz.Matrix(_SCALE, _SCALE)

    doc = fitz.open(file_path)
    try:
        for page_num, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            pages.append((page_num, img))
    finally:
        doc.close()

    return pages
