import re
from typing import List, Dict, Any


# Lunghezza minima testo per considerare un blocco valido
_MIN_TEXT_LENGTH = 2

# Soglia di prossimità verticale per merge (in punti/pixel)
_MERGE_Y_THRESHOLD = 5.0


def parse_blocks(
    blocks: List[Dict[str, Any]],
    merge_nearby: bool = False,
) -> List[Dict[str, Any]]:
    """
    Pulisce, filtra e ordina i blocchi testuali.

    Args:
        blocks: Lista di blocchi grezzi.
        merge_nearby: Se True, unisce blocchi adiacenti sulla stessa riga.

    Returns:
        Lista di blocchi elaborati e ordinati.
    """
    blocks = [b for b in blocks if _is_valid(b)]
    for block in blocks:
        block["text"] = _clean_text(block["text"])
    blocks = [b for b in blocks if _is_valid(b)]  # secondo passaggio dopo pulizia

    if merge_nearby:
        blocks = _merge_nearby_blocks(blocks)

    blocks = _sort_blocks(blocks)
    return blocks


# ---------------------------------------------------------------------------
# Funzioni interne
# ---------------------------------------------------------------------------

def _is_valid(block: Dict[str, Any]) -> bool:
    text = block.get("text", "").strip()
    if len(text) < _MIN_TEXT_LENGTH:
        return False
    # Blocchi composti solo da simboli/spazi
    if re.fullmatch(r"[\s\W]+", text):
        return False
    return True


def _clean_text(text: str) -> str:
    text = text.strip()
    # Normalizza spazi multipli
    text = re.sub(r"[ \t]+", " ", text)
    # Rimuovi caratteri di controllo (tranne newline)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    # Normalizza newline multiple
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _sort_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordina per pagina, poi top→bottom, poi left→right."""
    return sorted(
        blocks,
        key=lambda b: (
            b.get("page", 1),
            b["bbox"][1],   # y
            b["bbox"][0],   # x
        ),
    )


def _merge_nearby_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Unisce blocchi sulla stessa pagina con y molto vicine e x consecutiva.
    Utile per ricomporre parole OCR spezzate sulla stessa riga.
    """
    if not blocks:
        return blocks

    sorted_b = _sort_blocks(blocks)
    merged: List[Dict[str, Any]] = []
    current = sorted_b[0].copy()

    for next_b in sorted_b[1:]:
        same_page = current["page"] == next_b["page"]
        same_line = abs(current["bbox"][1] - next_b["bbox"][1]) <= _MERGE_Y_THRESHOLD

        if same_page and same_line:
            # Espandi la bbox e concatena testo
            cx, cy, cw, ch = current["bbox"]
            nx, ny, nw, nh = next_b["bbox"]
            new_x = min(cx, nx)
            new_y = min(cy, ny)
            new_w = max(cx + cw, nx + nw) - new_x
            new_h = max(ch, nh)
            current["bbox"] = [new_x, new_y, new_w, new_h]
            current["text"] = current["text"] + " " + next_b["text"]
            # Mantieni la confidence più bassa
            current["confidence"] = min(
                current.get("confidence", 1.0),
                next_b.get("confidence", 1.0),
            )
        else:
            merged.append(current)
            current = next_b.copy()

    merged.append(current)
    return merged
