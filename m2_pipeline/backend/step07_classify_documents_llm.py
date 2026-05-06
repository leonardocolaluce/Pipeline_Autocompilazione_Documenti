import json
from typing import Any, Dict, List
from pathlib import Path
import os
import zipfile
import re
import base64
from urllib import request

from .step00_config import MISTRAL_API_KEY, MISTRAL_MODEL, MISTRAL_TIMEOUT_SEC
from .step02_openai_json_utils import parse_llm_json_payload


CLASSIFIER_SYSTEM_PROMPT = """Sei un classificatore documentale rigoroso per una pipeline di compilazione automatica di documenti amministrativi italiani.

Compito specifico:
- ricevi metadati, frammenti M1 e un estratto del documento sorgente originale;
- devi decidere se il documento deve entrare nel flusso di compilazione automatica oppure no.

Definizione di documento compilabile:
- contiene campi vuoti, placeholder, puntini, underscore, celle tabellari da riempire, righe con dati anagrafici da inserire, campi firma, opzioni da selezionare;
- oppure è chiaramente un modello di domanda, dichiarazione, istanza, allegato dichiarativo, modulo amministrativo, patto da sottoscrivere.

Definizione di documento NON compilabile:
- è descrittivo, normativo o informativo;
- esempi tipici: bando, disciplinare, capitolato, informativa privacy, avviso, protocollo descrittivo, relazione;
- anche se contiene parole amministrative o riferimenti alla gara, non va classificato come compilabile se non mostra veri campi da riempire.

Istruzioni severe:
1. Sii conservativo: se hai dubbio, scegli NON compilabile.
2. Non classificare come compilabile per il solo nome del file.
3. Se `has_fields_json=true` o `has_tables_json=true`, questo è un segnale molto forte di compilabilità.
4. L'estratto del documento originale ha più valore del solo nome file: usalo per capire se è un modulo da compilare o un testo informativo.
5. Se il documento originale è un modulo con puntini, campi anagrafici, caselle o righe da completare, classificalo come compilabile.
6. Non aggiungere testo fuori schema.
7. Restituisci solo JSON valido.

Schema obbligatorio:
{
  "is_compilable": true,
  "confidence": 0.0,
  "reason": "motivazione breve e concreta"
}
"""



def _require_llm() -> None:
    if not MISTRAL_API_KEY:
        raise RuntimeError("LLM classify non disponibile: MISTRAL_API_KEY mancante.")


def _sample_text(blocks: Any, limit: int = 12) -> List[str]:
    if not isinstance(blocks, list):
        return []
    snippets: List[str] = []
    for item in blocks[:limit]:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("testo") or item.get("content") or "").strip()
        else:
            text = str(item).strip()
        if text:
            snippets.append(text[:400])
    return snippets

def _docx_head_text(docx_path: str, max_chars: int = 8000) -> str:
    p = Path(docx_path)
    if not p.exists():
        return ""
    if p.suffix.lower() != ".docx":
        return ""
    try:
        with zipfile.ZipFile(str(p), "r") as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", xml)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def _pdf_first_pages_as_images_excerpt(pdf_path: str, pages: int = 3, dpi: int = 160) -> str:
    # Rende immagini (prime N pagine) e restituisce una stringa “riassunto tecnico”
    # (in step07 restiamo TEXT-only: non facciamo OCR qui).
    try:
        import fitz  # PyMuPDF
    except Exception:
        return ""

    p = Path(pdf_path)
    if not p.exists():
        return ""

    doc = fitz.open(str(p))
    n = min(pages, doc.page_count)
    parts: list[str] = []
    for i in range(n):
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
        parts.append(f"page_{i+1}:image/png;base64,{b64}")
    # ATTENZIONE: questo può diventare grande; teniamo solo un prefisso.
    joined = "\n".join(parts)
    return joined[:8000]

def _fallback_classification(bundle: Dict[str, Any]) -> Dict[str, Any]:
    base_name = str(bundle.get("base_name", "")).lower()
    has_fields = bool(bundle.get("has_fields"))
    has_tables = bool(bundle.get("has_tables"))
    snippets = " ".join(_sample_text(bundle.get("blocks"))).lower()

    strong_negative_terms = [
        "disciplinare",
        "capitolato",
        "bando",
        "informativa",
        "privacy",
        "avviso",
    ]
    strong_positive_terms = [
        "domanda di partecipazione",
        "dichiara",
        "allega",
        "firma",
        "barrare",
        "si invita a barrare",
    ]

    if has_fields or has_tables:
        return {
            "is_compilable": True,
            "confidence": 0.94,
            "reason": "M1 ha già estratto campi o tabelle compilabili",
            "classifier": "fallback",
            "llm_enabled": False,
        }

    if any(term in base_name for term in strong_negative_terms):
        return {
            "is_compilable": False,
            "confidence": 0.9,
            "reason": "nome file tipico di documento informativo o descrittivo",
            "classifier": "fallback",
            "llm_enabled": False,
        }

    if any(term in snippets for term in strong_positive_terms):
        return {
            "is_compilable": True,
            "confidence": 0.72,
            "reason": "frammenti compatibili con modulistica dichiarativa",
            "classifier": "fallback",
            "llm_enabled": False,
        }

    return {
        "is_compilable": False,
        "confidence": 0.6,
        "reason": "assenza di segnali forti di compilazione",
        "classifier": "fallback",
        "llm_enabled": False,
    }


def _llm_classification(bundle: Dict[str, Any]) -> Dict[str, Any]:
    _require_llm()

    payload = {
        "file_name": bundle.get("base_name"),
        "source_document_path": bundle.get("source_document_path"),
        "source_document_type": bundle.get("source_document_type"),
        "has_fields_json": bundle.get("has_fields"),
        "has_tables_json": bundle.get("has_tables"),
        "sample_blocks": _sample_text(bundle.get("blocks")),
        "source_text_excerpt": str(bundle.get("source_text_excerpt") or "")[:8000],
    }

    body = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "top_p": 1,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }

    req = request.Request(
        "https://api.mistral.ai/v1/chat/completions",
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=MISTRAL_TIMEOUT_SEC) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        parsed = parse_llm_json_payload(raw)
        return {
            "is_compilable": bool(parsed.get("is_compilable")),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "reason": str(parsed.get("reason", "")).strip() or "no_reason",
            "classifier": "llm",
            "llm_enabled": True,
        }
    except Exception as exc:
        raise RuntimeError(f"LLM classify fallito per '{bundle.get('base_name')}': {exc}") from exc


def classify_bundles(bundles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    llm_available = bool(MISTRAL_API_KEY)
    mode = "mistral" if llm_available else "fallback"
    print(f"[LLM][classify] start - mode={mode} - documents={len(bundles)}")
    results: List[Dict[str, Any]] = []
    for bundle in bundles:
        if llm_available:
            try:
                verdict = _llm_classification(bundle)
            except Exception as exc:
                print(f"[LLM][classify] fallback - bundle={bundle.get('base_name')} - reason={exc}")
                verdict = _fallback_classification(bundle)
        else:
            verdict = _fallback_classification(bundle)
        results.append(
            {
                "base_name": bundle.get("base_name"),
                "has_fields": bundle.get("has_fields"),
                "has_tables": bundle.get("has_tables"),
                "is_compilable": verdict["is_compilable"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "classifier": verdict["classifier"],
                "llm_enabled": verdict["llm_enabled"],
            }
        )
    compilable = sum(1 for item in results if item["is_compilable"])
    print(f"[LLM][classify] end - mode={mode} - compilable={compilable}/{len(results)}")
    return results
