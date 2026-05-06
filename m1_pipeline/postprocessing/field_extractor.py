import re
from typing import List, Dict, Any, Optional


# Placeholder: underscore (3+), punti/ellissi (3+), trattini separati o continui (3+)
_PLACEHOLDER_RE = re.compile(
    r"_{3,}"           # _______________
    r"|\.{3,}"         # ...............
    r"|…{2,}"          # ………………………… (carattere ellissi unicode)
    r"|(?:-\s*){3,}"   # - - - - - - -
)

_MAX_LABEL_WORDS = 8
_MAX_CONTEXT_LABEL_WORDS = 14
_WEAK_LABELS = {
    "a",
    "al",
    "alla",
    "da",
    "di",
    "n",
    "unico",
    "motivazioni",
    "elettronico",
    "<indicare",
    "indicare",
}

# Parole chiave "stabili" per derivare etichette robuste vicino ai placeholder
_STABLE_LABEL_RE = re.compile(
    r"\b("
    r"il/la\s+sottoscritto/a|"
    r"nato/a\s+a|nata/o\s+a|nato/a|nata/o|"
    r"residente\s+a|"
    r"residente|"
    r"codice\s+fiscale|"
    r"partita\s+iva|p\.?\s*iva|"
    r"cap|"
    r"prov\.?|provincia|"
    r"via|piazza|indirizzo|"
    r"tel\.?|telefono|"
    r"e-?mail|pec|"
    r"dell['’]impresa/?societ[aà]|impresa/?societ[aà]|"
    r"sede\s+legale"
    r")\b",
    flags=re.IGNORECASE,
)

# Tolleranza x per considerare due celle nella stessa colonna (punti)
_COLUMN_X_TOLERANCE = 5.0
_LINE_Y_TOLERANCE = 4.0


def extract_fields(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Scansiona i blocchi e restituisce la lista dei campi da compilare.

    Strategie di etichettatura (in ordine di priorità):
      1. Testo prima del placeholder nello stesso blocco
      2. Testo dopo il placeholder nello stesso blocco
      3. Per table_cell: intestazione di colonna (stessa x, y minore)
      4. Blocco precedente nella sequenza (etichetta su riga sopra)

    Args:
        blocks: Lista di blocchi nel formato standard (output di parse_blocks).

    Returns:
        Lista di dizionari, uno per ogni placeholder trovato.
    """
    fields = []

    for idx, block in enumerate(blocks):
        text = block.get("text", "")
        if not _PLACEHOLDER_RE.search(text):
            continue

        line_text, previous_line_text = _build_context_windows(idx, block, blocks)

        parts = _PLACEHOLDER_RE.split(text)
        placeholders = _PLACEHOLDER_RE.findall(text)

        for i, placeholder in enumerate(placeholders):
            # Costruisci un contesto locale per questo placeholder (evita contesti identici per campi consecutivi).
            local_line = _local_line_context(parts, placeholders, i)
            combined_context = " ".join(
                part for part in [previous_line_text, local_line] if part
            ).strip() or local_line.strip() or text.strip()

            label = _resolve_label(parts, i, idx, block, blocks, placeholder, line_text, previous_line_text)
            fields.append({
                "campo":       label,
                "valore":      "",
                "placeholder": placeholder,
                "contesto":    combined_context,
                "contesto_riga": local_line,
                "contesto_sopra": previous_line_text,
                "bbox":        block["bbox"],
                "page":        block.get("page", 1),
            })

    return fields


# ---------------------------------------------------------------------------
# Risoluzione etichetta
# ---------------------------------------------------------------------------

def _resolve_label(
    parts: List[str],
    part_index: int,
    block_index: int,
    block: Dict[str, Any],
    all_blocks: List[Dict[str, Any]],
    placeholder: str,
    line_text: str,
    previous_line_text: str,
) -> str:
    """Determina l'etichetta del campo con fallback progressivi."""

    # 0. Se la riga contiene più placeholder, prova a derivare un label "stabile"
    # guardando il contesto vicino al placeholder corrente (non l'intera riga).
    if line_text and len(_PLACEHOLDER_RE.findall(line_text)) > 1:
        stable = _stable_label_from_parts(parts, part_index)
        if stable and not _is_weak_label(stable):
            return stable

    # 1. Testo prima del placeholder nello stesso blocco
    before = _clean_label(parts[part_index]) if part_index < len(parts) else ""
    if before:
        words = before.split()
        candidate = " ".join(words[-_MAX_LABEL_WORDS:])
        if not _is_weak_label(candidate):
            return candidate

    # 2. Testo dopo il placeholder nello stesso blocco
    after_raw = parts[part_index + 1] if part_index + 1 < len(parts) else ""
    after = _clean_label(after_raw)
    if after:
        words = after.split()
        candidate = " ".join(words[:_MAX_LABEL_WORDS])
        if not _is_weak_label(candidate):
            return candidate

    # 3. Riga completa del PDF / blocchi vicini
    line_label = _label_from_line_context(line_text, placeholder)
    if line_label and not _is_weak_label(line_label):
        return line_label

    # 4. Riga sopra + riga corrente
    stacked_label = _label_from_stacked_context(previous_line_text, line_text, placeholder)
    if stacked_label:
        return stacked_label

    # 5. Per celle di tabella: cerca intestazione nella stessa colonna (stessa x, y minore)
    if block.get("style") == "table_cell":
        header = _find_column_header(block, all_blocks)
        if header:
            return header

    # 6. Blocco precedente (etichetta su riga sopra)
    prev_label = _find_previous_block_label(block_index, block, all_blocks)
    if prev_label:
        return prev_label

    return "(campo)"


def _find_column_header(
    block: Dict[str, Any],
    all_blocks: List[Dict[str, Any]],
) -> Optional[str]:
    """
    Cerca l'intestazione di colonna per una cella di tabella.
    Condizioni: stesso source=table_cell, stessa x (±tolleranza), y minore, stessa pagina.
    Restituisce la cella con y massima tra quelle sopra (quella più vicina sopra).
    """
    bx, by = block["bbox"][0], block["bbox"][1]
    page = block.get("page", 1)

    candidates = [
        b for b in all_blocks
        if b.get("style") == "table_cell"
        and b.get("page", 1) == page
        and abs(b["bbox"][0] - bx) <= _COLUMN_X_TOLERANCE
        and b["bbox"][1] < by
        and _PLACEHOLDER_RE.search(b.get("text", "")) is None  # non è a sua volta un campo vuoto
    ]

    if not candidates:
        return None

    # Prendi quella con y più alta (più vicina al blocco corrente)
    closest = max(candidates, key=lambda b: b["bbox"][1])
    return _clean_label(closest["text"]) or None


def _find_previous_block_label(
    block_index: int,
    block: Dict[str, Any],
    all_blocks: List[Dict[str, Any]],
) -> Optional[str]:
    """
    Risale la lista cercando il blocco precedente sulla stessa pagina
    che non sia esso stesso un placeholder e che abbia testo significativo.
    """
    page = block.get("page", 1)

    for i in range(block_index - 1, -1, -1):
        prev = all_blocks[i]
        if prev.get("page", 1) != page:
            break
        prev_text = prev.get("text", "").strip()
        if not prev_text:
            continue
        # Salta se è a sua volta un blocco di soli placeholder
        cleaned = _PLACEHOLDER_RE.sub("", prev_text).strip()
        if not cleaned:
            continue
        label = _clean_label(prev_text)
        if label:
            words = label.split()
            return " ".join(words[-_MAX_LABEL_WORDS:])

    return None


# ---------------------------------------------------------------------------
# Pulizia testo
# ---------------------------------------------------------------------------

def _clean_label(text: str) -> str:
    """Rimuove checkbox, simboli decorativi e punteggiatura ridondante."""
    text = text.strip()
    # Rimuovi checkbox e simboli iniziali
    text = re.sub(r"^[\s□■●○•\-–—]+", "", text)
    # Rimuovi residui di placeholder e parentesi vuote
    text = _PLACEHOLDER_RE.sub(" ", text)
    text = re.sub(r"\(\s*\)", " ", text)
    # Rimuovi punteggiatura finale
    text = text.strip(" :,;./\\()")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _stable_label_from_line(line_text: str, placeholder: str) -> str:
    """
    Estrae un'etichetta robusta da una riga con più placeholder.
    Cerca parole chiave vicine al placeholder (prima o dopo) invece di usare
    direttamente pezzi di underscore/parentesi.
    """
    if not line_text:
        return ""
    # Cerca la porzione di testo immediatamente prima e dopo il placeholder corrente
    before = _text_before_placeholder(line_text, placeholder)
    after = _text_after_placeholder(line_text, placeholder)
    window = " ".join(part for part in [before, after] if part).strip()
    if not window:
        window = line_text
    # Trova l'ultima keyword "stabile" che appare nel before/after window
    hits = list(_STABLE_LABEL_RE.finditer(window))
    if not hits:
        return ""
    hit = hits[-1].group(0).strip()
    # Normalize some short/ambiguous hits into more explicit labels.
    lowered = hit.lower()
    if lowered in {"nato/a", "nata/o"}:
        return "nato/a a"
    if lowered == "residente":
        return "residente a"
    return hit


def _stable_label_from_parts(parts: List[str], part_index: int) -> str:
    """
    Deriva un label stabile per placeholder `part_index` in una riga con più placeholder.

    Regola principale (evita il bug "Il/La sottoscritto/a" -> "nato/a a"):
    - preferisci keyword trovate nel testo *prima* del placeholder (parts[part_index]);
    - se non trovi nulla, usa keyword nel testo *dopo* (parts[part_index+1]).
    """
    before_raw = parts[part_index] if part_index < len(parts) else ""
    after_raw = parts[part_index + 1] if (part_index + 1) < len(parts) else ""

    before = _clean_label(before_raw)
    after = _clean_label(after_raw)

    hits_before = list(_STABLE_LABEL_RE.finditer(before))
    if hits_before:
        hit = hits_before[-1].group(0).strip()
        lowered = hit.lower()
        if lowered in {"nato/a", "nata/o"}:
            return "nato/a a"
        if lowered == "residente":
            return "residente a"
        return hit

    hits_after = list(_STABLE_LABEL_RE.finditer(after))
    if hits_after:
        hit = hits_after[0].group(0).strip()
        lowered = hit.lower()
        if lowered in {"nato/a", "nata/o"}:
            return "nato/a a"
        if lowered == "residente":
            return "residente a"
        return hit

    return ""


def _local_line_context(parts: List[str], placeholders: List[str], index: int) -> str:
    """
    Ritorna una porzione di riga più locale per il placeholder `index`, in modo che
    campi adiacenti non abbiano `contesto_riga` identico.

    Strategia:
    - usa testo tra placeholder precedente e quello corrente (parts[index])
    - usa testo subito dopo (parts[index+1])
    - rimuove residui e normalizza spazi
    """
    before = parts[index] if index < len(parts) else ""
    after = parts[index + 1] if (index + 1) < len(parts) else ""
    snippet = " ".join(part for part in [before, after] if part).strip()
    snippet = _clean_label(snippet)
    if not snippet:
        # fallback: prova a usare un window più ampio
        raw = " ".join(p.strip() for p in parts[max(0, index - 1) : min(len(parts), index + 2)] if p.strip())
        snippet = _clean_label(raw)
    # ultima difesa: disambiguatore minimo per evitare duplicati consecutivi
    if not snippet:
        snippet = "(campo)"
    return snippet


def _build_context_windows(
    block_index: int,
    block: Dict[str, Any],
    all_blocks: List[Dict[str, Any]],
) -> tuple[str, str]:
    page = block.get("page", 1)
    current_y = float(block["bbox"][1])
    current_h = float(block["bbox"][3])

    same_page = [item for item in all_blocks if item.get("page", 1) == page]
    same_line = [
        item for item in same_page
        if abs(float(item["bbox"][1]) - current_y) <= max(_LINE_Y_TOLERANCE, max(current_h, float(item["bbox"][3])) * 0.6)
    ]
    same_line.sort(key=lambda item: (item["bbox"][0], item["bbox"][1]))
    line_text = _join_block_texts(same_line)

    previous_candidates = [
        item for item in same_page
        if float(item["bbox"][1]) < current_y - max(_LINE_Y_TOLERANCE, current_h * 0.6)
    ]
    if not previous_candidates:
        return line_text, ""

    target_y = max(float(item["bbox"][1]) for item in previous_candidates)
    previous_line = [
        item for item in previous_candidates
        if abs(float(item["bbox"][1]) - target_y) <= max(_LINE_Y_TOLERANCE, float(item["bbox"][3]) * 0.6)
    ]
    previous_line.sort(key=lambda item: (item["bbox"][0], item["bbox"][1]))
    previous_line_text = _join_block_texts(previous_line)
    return line_text, previous_line_text


def _join_block_texts(blocks: List[Dict[str, Any]]) -> str:
    tokens = [str(block.get("text", "")).strip() for block in blocks if str(block.get("text", "")).strip()]
    return " ".join(tokens).strip()


def _label_from_line_context(line_text: str, placeholder: str) -> str:
    if not line_text:
        return ""
    before = _text_before_placeholder(line_text, placeholder)
    if before:
        words = before.split()
        return " ".join(words[-_MAX_CONTEXT_LABEL_WORDS:])
    after = _text_after_placeholder(line_text, placeholder)
    if after:
        words = after.split()
        return " ".join(words[:_MAX_CONTEXT_LABEL_WORDS])
    return ""


def _label_from_stacked_context(previous_line_text: str, line_text: str, placeholder: str) -> str:
    current_before = _text_before_placeholder(line_text, placeholder)
    pieces = [part for part in [previous_line_text, current_before] if part]
    if not pieces:
        return ""
    words = " ".join(pieces).split()
    return " ".join(words[-_MAX_CONTEXT_LABEL_WORDS:]).strip()


def _text_before_placeholder(text: str, placeholder: str) -> str:
    if placeholder and placeholder in text:
        return _clean_label(text.split(placeholder, 1)[0])
    parts = _PLACEHOLDER_RE.split(text)
    return _clean_label(parts[0]) if parts else ""


def _text_after_placeholder(text: str, placeholder: str) -> str:
    if placeholder and placeholder in text:
        return _clean_label(text.split(placeholder, 1)[1])
    parts = _PLACEHOLDER_RE.split(text)
    return _clean_label(parts[1]) if len(parts) > 1 else ""


def _is_weak_label(text: str) -> bool:
    normalized = _normalize_label_key(text)
    return (not normalized) or normalized in _WEAK_LABELS or len(normalized) <= 2


def _normalize_label_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())
