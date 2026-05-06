import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib import request

from .step00_config import FIELD_MAPPING_FILENAME, MISTRAL_API_KEY, MISTRAL_MODEL, MISTRAL_TIMEOUT_SEC, SUMMARY_FILENAME
from .step02_openai_json_utils import parse_llm_json_payload
from .step01_path_utils import coerce_path
from .step03_source_documents import extract_source_text


def _require_llm() -> None:
    if not MISTRAL_API_KEY:
        raise RuntimeError("LLM validator non disponibile: MISTRAL_API_KEY mancante.")


VALIDATOR_SYSTEM_PROMPT = """Sei il validatore finale, di una pipeline di compilazione documentale.

Compito specifico:
- ricevi il JSON campo->valore prodotto dal mapper;
- ricevi il JSON dei dati aziendali disponibili;
- ricevi anche il testo del documento originale sorgente;
- ricevi il testo estratto dal DOCX finale;
- devi segnalare solo gli `item_id` che risultano compilati in modo scorretto, non supportato o semanticamente incompatibile.

Criteri di invalidazione:
1 devi vedere campi e valori associati, un campo è invalido nel caso ci sia esempio:
  nome città al posto della sezione il sottoscritto, numero telefono al posto della data o luogo di nascita, questi sono solo alcuni esempi.
2 in poche parole se non combaciano i 2 campi allora eliminalo, ma senno lo lasci. solo quando è evidente che non è il campo giusto puo faro senno de defaul lo lasci cosi.
{"invalid_item_ids":["field:1","table:0:1:2"]}
"""


def _flatten_values(node: Any) -> Set[str]:
    values: Set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif obj is not None:
            text = str(obj).strip()
            if text:
                values.add(text)

    walk(node)
    values.add("N/D")
    return values


def _extract_docx_text(docx_path: Path) -> str:
    if not docx_path.exists():
        return ""
    try:
        with zipfile.ZipFile(docx_path) as archive:
            data = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        text = data.replace("</w:p>", "\n")
        import re

        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()
    except Exception:
        return ""


def _deterministic_invalid_ids(rows: List[Dict[str, Any]], xml_data: Dict[str, Any], docx_text: str) -> List[str]:
    allowed = _flatten_values(xml_data)
    invalid: List[str] = []
    for row in rows:
        answer = str(row.get("answer", "")).strip()
        if not answer or answer == "N/D":
            continue
        if answer not in allowed:
            invalid.append(str(row.get("item_id")))
            continue
        if docx_text and answer not in docx_text:
            invalid.append(str(row.get("item_id")))
    return invalid


def _llm_invalid_ids(rows: List[Dict[str, Any]], xml_data: Dict[str, Any], docx_text: str, source_text: str) -> List[str]:
    _require_llm()

    candidates = [
        {
            "item_id": row.get("item_id"),
            "item_type": row.get("item_type"),
            "label": row.get("label"),
            "context": row.get("context"),
            "answer": row.get("answer"),
            "confidence": row.get("confidence"),
            "reason": row.get("reason"),
        }
        for row in rows
        if str(row.get("answer", "")).strip() not in {"", "N/D"}
    ]

    body = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "top_p": 1,
        "messages": [
            {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "data_json": xml_data,
                        "source_document_text_excerpt": source_text[:12000],
                        "compiled_docx_text_excerpt": docx_text[:12000],
                        "candidate_rows": candidates,
                    },
                    ensure_ascii=False,
                ),
            },
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
        values = parsed.get("invalid_item_ids", [])
        return [str(value) for value in values]
    except Exception as exc:
        raise RuntimeError(f"LLM validator fallito: {exc}") from exc


def validate_and_prune(
    output_dir: str | Path,
    compiled_docx_path: str | Path,
) -> Dict[str, Any]:
    llm_available = bool(MISTRAL_API_KEY)
    mode = "mistral" if llm_available else "deterministic"
    print(f"[LLM][validator] start - mode={mode}")
    out_dir = Path(output_dir).resolve()
    mapping_path = out_dir / FIELD_MAPPING_FILENAME
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_path}")

    with open(mapping_path, "r", encoding="utf-8") as handle:
        mapping_payload = json.load(handle)

    xml_json_path = coerce_path(mapping_payload["xml_json_path"]).resolve()
    with open(xml_json_path, "r", encoding="utf-8") as handle:
        xml_data = json.load(handle)

    docx_text = _extract_docx_text(coerce_path(compiled_docx_path).resolve())
    source_document_path = mapping_payload.get("source_document_path")
    source_text = extract_source_text(coerce_path(source_document_path)) if source_document_path else ""
    rows = mapping_payload.get("rows") or []
    before_compiled = sum(1 for row in rows if str(row.get("answer", "")).strip() not in {"", "N/D"})
    total_fields = len(rows)

    invalid_ids = set(_deterministic_invalid_ids(rows, xml_data, docx_text))
    if llm_available:
        try:
            invalid_ids.update(_llm_invalid_ids(rows, xml_data, docx_text, source_text))
        except Exception as exc:
            print(f"[LLM][validator] fallback - reason={exc}")

    for row in rows:
        if str(row.get("item_id")) in invalid_ids:
            row["answer"] = "N/D"
            row["confidence"] = 0.0
            row["reason"] = f"{row.get('reason', 'unknown')}|validator_removed"
            row["validator_status"] = "removed"
        else:
            row["validator_status"] = "ok"

    with open(mapping_path, "w", encoding="utf-8") as handle:
        json.dump(mapping_payload, handle, ensure_ascii=False, indent=2)

    after_compiled = sum(1 for row in rows if str(row.get("answer", "")).strip() not in {"", "N/D"})
    llm_used_in_mapping = any(bool(row.get("llm_enabled")) for row in rows)
    summary_path = out_dir / SUMMARY_FILENAME
    summary_payload = {
        "totale_campi": total_fields,
        "compilati_prima_validatore": before_compiled,
        "compilati_dopo_validatore": after_compiled,
        "rimossi_dal_validatore": len(invalid_ids),
        "llm_attivo_mapping": llm_used_in_mapping,
        "llm_attivo_validator": bool(MISTRAL_API_KEY),
    }
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, ensure_ascii=False, indent=2)

    print(f"[LLM][validator] end - mode={mode} - removed={len(invalid_ids)} before={before_compiled} after={after_compiled}")
    return {
        "mapping_path": str(mapping_path),
        "summary_path": str(summary_path),
        "invalid_item_ids": sorted(invalid_ids),
        "removed_count": len(invalid_ids),
    }


def rerender_docx_with_venv(venv_python: str | Path, main_py: str | Path, bundle_name: str) -> None:
    subprocess.run(
        [str(Path(venv_python)), str(Path(main_py).resolve()), "--step", "write_docx", "--bundle-name", bundle_name],
        check=True,
    )
