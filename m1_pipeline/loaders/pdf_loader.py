from typing import List, Dict, Any
import pdfplumber


def load_pdf(file_path: str) -> List[Dict[str, Any]]:
    """
    Estrae testo e coordinate da un PDF nativo.

    Args:
        file_path: Percorso al file PDF.

    Returns:
        Lista di blocchi nel formato standard:
        {"text": str, "bbox": [x, y, w, h], "page": int, "confidence": float}
    """
    blocks: List[Dict[str, Any]] = []

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["fontname", "size"],
            )
            for word in words:
                x = float(word["x0"])
                y = float(word["top"])
                w = float(word["x1"]) - x
                h = float(word["bottom"]) - y
                blocks.append(
                    {
                        "text": word["text"],
                        "bbox": [round(x, 2), round(y, 2), round(w, 2), round(h, 2)],
                        "page": page_num,
                        "confidence": 1.0,
                        "source": "pdf_native",
                    }
                )

    return blocks
