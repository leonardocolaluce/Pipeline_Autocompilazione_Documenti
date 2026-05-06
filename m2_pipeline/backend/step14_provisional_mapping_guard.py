import json
import re
from pathlib import Path
from typing import Any, Dict, List


_FORCE_EMPTY_LABEL_PATTERNS: list[re.Pattern[str]] = [
    # Example requested: fields like "luogo e data" must stay empty.
    re.compile(r"\bluogo\b.*\bdata\b", re.IGNORECASE),
    re.compile(r"\bdata\b.*\bluogo\b", re.IGNORECASE),
]


def should_force_empty_row(row: Dict[str, Any]) -> bool:
    label = str(row.get("label", "") or "").strip()
    if not label:
        return False
    for pattern in _FORCE_EMPTY_LABEL_PATTERNS:
        if pattern.search(label):
            return True
    return False


def apply_provisional_guard(mapping_path: str | Path) -> Dict[str, Any]:
    """
    Enforces additional post-processing rules on a provisional mapping file.
    The intent is to keep specific fields always empty (N/D), even if upstream mapping fills them.
    """
    path = Path(mapping_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = list(payload.get("rows") or [])

    forced = 0
    for row in rows:
        if not should_force_empty_row(row):
            continue
        if str(row.get("answer", "")).strip() in {"", "N/D"}:
            continue
        row["answer"] = "N/D"
        row["confidence"] = 0.0
        row["reason"] = f"{row.get('reason', '')}|provisional_guard_forced_empty".strip("|")
        forced += 1

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mapping_path": str(path), "forced_empty_count": forced}

