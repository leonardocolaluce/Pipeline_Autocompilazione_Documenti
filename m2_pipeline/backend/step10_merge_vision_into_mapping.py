import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PAGE_RE = re.compile(r"page[_-]?0*(\d+)", re.I)

_DROP_WORDS = {
    "di",
    "del",
    "della",
    "delle",
    "dello",
    "degli",
    "dei",
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "un",
    "uno",
    "una",
    "in",
    "a",
    "ad",
    "da",
    "dal",
    "dai",
    "con",
    "per",
    "e",
    "o",
    "obbligatorio",
    "legale",
    "rappresentante",
    "societario",
}

_ALIASES = {
    "cf": "codicefiscale",
    "c.f": "codicefiscale",
    "piva": "partitaiva",
    "p.iva": "partitaiva",
    "pec": "indirizzopec",
    "email": "mail",
    "eemail": "mail",
    "e-mail": "mail",
    "qualita": "incarico",
    "carica": "incarico",
    "nato": "nascita",
    "nata": "nascita",
}

def _normalize_known_terms(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\bc\s*[\.\-]?\s*f\s*\.?\b", " codicefiscale ", text)
    text = re.sub(r"\bcod\.?\s*fiscale\b", " codicefiscale ", text)
    text = re.sub(r"\bcodice\s+fiscale\b", " codicefiscale ", text)
    text = re.sub(r"\bp\s*[\.\-]?\s*iva\b", " partitaiva ", text)
    text = re.sub(r"\bpartita\s+iva\b", " partitaiva ", text)
    text = re.sub(r"\be\s*[\.\-]?\s*mail\b", " email ", text)
    text = re.sub(r"\bmail\s+pec\b", " pec ", text)
    text = re.sub(r"\bcontratto\s+collettivo\b", " ccnl ", text)
    text = re.sub(r"\bc\s*\.?\s*f\s*\.?\s*/\s*p\s*\.?\s*iva\b", " codicefiscale partitaiva ", text)
    text = re.sub(r"\bvia\s*/\s*p\.?zza\b", " indirizzo ", text)
    text = re.sub(r"\be\s*mail\s*/\s*pec\b", " email pec ", text)
    text = re.sub(r"\bn\.?\s*rea\b", " rea ", text)
    text = re.sub(r"\bn\.?\s*di\s*iscrizione\b", " numero iscrizione ", text)
    text = re.sub(r"\bmatricola\s*n\.?r?\.?\b", " matricola ", text)
    text = re.sub(r"\bnumero\s+dipendenti\b", " numerodipendenti ", text)
    return text

def _strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _clean_text(value: str) -> str:
    text = _strip_accents(_normalize_known_terms(str(value or ""))).lower()
    text = text.replace("â€™", "'").replace("’", "'")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[_.,;:!?/\\|+*=<>[\]{}\"'`~^-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokens(value: str) -> List[str]:
    text = _clean_text(value)
    raw_tokens = _TOKEN_RE.findall(text)
    out: List[str] = []
    for token in raw_tokens:
        token = _ALIASES.get(token, token)
        if token in _DROP_WORDS:
            continue
        out.append(token)
    return out


def _compact(value: str) -> str:
    return "".join(_tokens(value))


def _page_from_image(value: Any) -> int | None:
    match = _PAGE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _page_from_row(row: Dict[str, Any]) -> int | None:
    page = row.get("page")
    if page in (None, ""):
        return None
    try:
        return int(page)
    except (TypeError, ValueError):
        return None


def _coerce_answer(value: Any) -> str:
    if value is None:
        return "N/D"
    if isinstance(value, bool):
        return "true" if value else "N/D"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip() or "N/D"


def _is_empty_answer(row: Dict[str, Any]) -> bool:
    answer = str(row.get("answer", "") or "").strip()
    return answer in {"", "N/D"}


def _label_score(vision_label: str, row_label: str) -> float:
    # Very short M1 labels are common: "il" for "nato il", "a" for "nato a".
    # Check this before token cleanup, because those short labels are stopwords
    # and would otherwise disappear.
    raw_row = _clean_text(row_label)
    raw_vision = _clean_text(vision_label)
    if raw_row and len(raw_row) <= 3 and re.search(rf"\b{re.escape(raw_row)}\b", raw_vision):
        return 75.0

    vision_compact = _compact(vision_label)
    row_compact = _compact(row_label)
    if not vision_compact or not row_compact:
        return 0.0

    if vision_compact == row_compact:
        return 100.0

    vision_tokens = set(_tokens(vision_label))
    row_tokens = set(_tokens(row_label))
    if not vision_tokens or not row_tokens:
        return 0.0

    if row_compact in vision_compact or vision_compact in row_compact:
        shorter = min(len(row_compact), len(vision_compact))
        longer = max(len(row_compact), len(vision_compact))
        return 70.0 + (20.0 * shorter / max(longer, 1))

    overlap = len(vision_tokens & row_tokens)
    if overlap:
        precision = overlap / len(row_tokens)
        recall = overlap / len(vision_tokens)
        return 45.0 + (35.0 * ((precision + recall) / 2.0))

    return 0.0

_SOURCE_LABEL_HINTS = {
    "ragione_sociale": [
        "ragione sociale", "denominazione", "impresa", "societa",
        "dell'impresa", "ditta", "operatore economico"
    ],
    "codice_fiscale": [
        "codice fiscale", "cod. fiscale", "cod fiscale", "c.f.", "cf",
        "c.f. / p.iva"
    ],
    "partita_iva": [
        "partita iva", "p.iva", "p iva", "iva", "c.f. / p.iva"
    ],
    "forma_giuridica": [
        "forma giuridica", "tipo societa", "societa", "cooperativa"
    ],
    "codice_attivita": [
        "cod. attivita", "codice attivita", "attivita"
    ],
    "codice_ateco": [
        "ateco", "codice ateco", "cod. attivita", "codice attivita"
    ],
    "settore": [
        "oggetto sociale", "oggetto dell'attivita", "attivita esercitata",
        "esercita l'attivita", "attivita di"
    ],
    "ccnl": [
        "ccnl", "contratto collettivo", "contratto collettivo nazionale",
        "contratto collettivo applicato", "contratto applicato"
    ],
    "numero_dipendenti": [
        "numero dipendenti", "n. dipendenti", "dimensione aziendale",
        "fascia dipendenti", "dipendenti"
    ],
    "sede_legale_operativa": [
        "sede legale", "sede", "con sede", "sede a", "sede in"
    ],
    "sede_legale_operativa.comune": [
        "comune", "sede legale in", "con sede legale in", "sede a"
    ],
    "sede_legale_operativa.provincia": [
        "provincia", "prov.", "prov", "presso la provincia di"
    ],
    "sede_legale_operativa.indirizzo": [
        "indirizzo", "via", "via/piazza", "via/p.zza", "piazza"
    ],
    "sede_legale_operativa.cap": [
        "cap", "c.a.p."
    ],
    "contatti.telefono": [
        "telefono", "tel", "tel.", "recapito telefonico"
    ],
    "contatti.email": [
        "email", "e-mail", "indirizzo email", "posta elettronica"
    ],
    "mail_pec": [
        "pec", "indirizzo pec", "mail pec", "e-mail/pec", "email/pec"
    ],
    "registro_imprese.numero": [
        "registro imprese", "registro delle imprese", "n. registro",
        "numero iscrizione", "n. di iscrizione"
    ],
    "registro_imprese.camera_commercio": [
        "cciaa", "camera di commercio", "registro imprese presso la cciaa"
    ],
    "registro_imprese.data_inizio": [
        "data iscrizione", "dal", "iscritta dal"
    ],
    "rea.numero": [
        "rea", "numero rea", "n. rea"
    ],
    "inps": [
        "inps", "posizione inps", "matricola inps"
    ],
    "inail": [
        "inail", "posizione inail", "codice ditta", "pat", "numero pat"
    ],
    "cassa_edile": [
        "cassa edile", "matricola cassa edile", "codice cassa"
    ],
    "conto_corrente.iban": [
        "iban", "conto corrente", "cc dedicato"
    ],
    "enti_competenti": [
        "tribunale competente", "tribunale", "agenzia delle entrate",
        "centro per l'impiego", "ufficio competente"
    ],
}


def _source_hint_score(match: Dict[str, Any], row: Dict[str, Any]) -> float:
    source_path = str(match.get("source_path") or "").lower()
    row_label = str(row.get("label") or "")

    if not source_path:
        return 0.0

    best = 0.0
    for source_key, hints in _SOURCE_LABEL_HINTS.items():
        if source_key not in source_path:
            continue
        for hint in hints:
            best = max(best, _label_score(hint, row_label))

    return best

def _candidate_score(
    *,
    match: Dict[str, Any],
    row: Dict[str, Any],
    row_index: int,
    last_index_for_page: Dict[int, int],
) -> float:
    vision_page = _page_from_image(match.get("image_page"))
    row_page = _page_from_row(row)

    # If both pages are known, do not cross pages. This prevents matching a
    # generic label like "a" on page 1 to an unrelated field on page 4.
    if vision_page is not None and row_page is not None and vision_page != row_page:
        return 0.0

    label = str(match.get("label") or "")
    row_label = str(row.get("label") or "")
    score = max(_label_score(label, row_label), _source_hint_score(match, row))
    if score <= 0:
        return 0.0

    if vision_page is not None and row_page == vision_page:
        score += 20.0

    # Prefer rows after the last matched row on the same page, preserving visual order.
    order_page = row_page if row_page is not None else vision_page
    if order_page is not None:
        last_index = last_index_for_page.get(order_page, -1)
        if row_index > last_index:
            score += 10.0
            distance = row_index - last_index
            score += max(0.0, 8.0 - min(distance, 8))
        else:
            score -= 15.0

    return score


def _best_row_for_match(
    match: Dict[str, Any],
    rows: List[Dict[str, Any]],
    used_indexes: set[int],
    last_index_for_page: Dict[int, int],
) -> tuple[int | None, float]:
    best_index: int | None = None
    best_score = 0.0

    for index, row in enumerate(rows):
        if index in used_indexes:
            continue
        if not _is_empty_answer(row):
            continue

        score = _candidate_score(
            match=match,
            row=row,
            row_index=index,
            last_index_for_page=last_index_for_page,
        )
        if score > best_score:
            best_index = index
            best_score = score

    return best_index, best_score


def merge_vision_matches_into_mapping(
    mapping_path: str | Path,
    vision_match_path: str | Path,
) -> Dict[str, Any]:
    mapping_file = Path(mapping_path).resolve()
    vision_file = Path(vision_match_path).resolve()

    if not mapping_file.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_file}")
    if not vision_file.exists():
        raise FileNotFoundError(f"Vision match JSON non trovato: {vision_file}")

    mapping_payload = json.loads(mapping_file.read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = list(mapping_payload.get("rows") or [])

    vision_payload = json.loads(vision_file.read_text(encoding="utf-8"))
    matches = [item for item in (vision_payload.get("matches") or []) if isinstance(item, dict)]

    filled = 0
    used_indexes: set[int] = set()
    last_index_for_page: Dict[int, int] = {}

    for match in matches:
        answer = _coerce_answer(match.get("value"))
        if answer in {"", "N/D"}:
            continue

        index, score = _best_row_for_match(match, rows, used_indexes, last_index_for_page)
        if index is None or score < 40.0:
            continue

        row = rows[index]
        source_path = str(match.get("source_path") or "").strip()
        confidence = float(match.get("confidence", 0.0) or 0.0)
        image_page = str(match.get("image_page") or "").strip()

        row["answer"] = answer
        row["confidence"] = max(0.0, min(1.0, confidence))
        row["reason"] = f"vision:{source_path}" if source_path else "vision"
        if image_page:
            row["reason"] = f"{row['reason']}:{image_page}"
        row["llm_enabled"] = True

        used_indexes.add(index)
        row_page = _page_from_row(row)
        if row_page is not None:
            last_index_for_page[row_page] = index
        filled += 1

    mapping_file.write_text(json.dumps(mapping_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mapping_path": str(mapping_file),
        "filled_count": filled,
        "vision_match_path": str(vision_file),
    }
