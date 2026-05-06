import json
from pathlib import Path
from typing import Any, Dict, List

from .step00_config import M1_OUTPUT_DIR
from .step03_source_documents import extract_source_text, resolve_source_document


def _base_name_from_file_name(name: str) -> str:
    for suffix in ("_DA_COMPILARE.json", "_CAMPI.json", "_CAMPI_SLIM.json", "_TABELLE.json", "_CHECKBOX.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem



def _load_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def discover_m1_bundles(input_dir: Path | None = None) -> List[Dict[str, Any]]:
    root = Path(input_dir or M1_OUTPUT_DIR).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Cartella M1 non trovata: {root}")

    grouped: Dict[str, Dict[str, Path]] = {}
    for path in sorted(root.glob("*.json")):
        base = _base_name_from_file_name(path.name)
        bucket = grouped.setdefault(base, {})
        bucket[path.name] = path

    bundles: List[Dict[str, Any]] = []
    for base, paths in sorted(grouped.items()):
        blocks_path = root / f"{base}_DA_COMPILARE.json"
        fields_path = root / f"{base}_CAMPI.json"
        fields_slim_path = root / f"{base}_CAMPI_SLIM.json"
        effective_fields_path = fields_path if fields_path.exists() else fields_slim_path

        tables_path = root / f"{base}_TABELLE.json"
        checkbox_path = root / f"{base}_CHECKBOX.json"
        source_document = resolve_source_document(base)

        bundles.append(
            {
                "base_name": base,
                "blocks_path": str(blocks_path) if blocks_path.exists() else None,
                "fields_path": str(effective_fields_path) if effective_fields_path.exists() else None,
                "tables_path": str(tables_path) if tables_path.exists() else None,
                "checkbox_path": str(checkbox_path) if checkbox_path.exists() else None,
                "has_blocks": blocks_path.exists(),
                "has_fields": effective_fields_path.exists(),
                "has_tables": tables_path.exists(),
                "has_checkboxes": checkbox_path.exists(),
                "blocks": _load_json_if_exists(blocks_path) if blocks_path.exists() else None,
                "fields": (
                    _load_json_if_exists(fields_path)
                    if fields_path.exists()
                    else ((_load_json_if_exists(fields_slim_path) or {}).get("rows") if fields_slim_path.exists() else None)
                ),
                "tables": _load_json_if_exists(tables_path) if tables_path.exists() else None,
                "checkboxes": _load_json_if_exists(checkbox_path) if checkbox_path.exists() else None,
                "source_document_path": str(source_document) if source_document else None,
                "source_document_type": source_document.suffix.lower() if source_document else None,
                "source_text_excerpt": extract_source_text(source_document),
            }
        )

    return bundles
