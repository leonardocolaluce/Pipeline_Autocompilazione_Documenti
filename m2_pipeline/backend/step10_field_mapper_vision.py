from pathlib import Path
from typing import Any, Dict

import json

from .step08_mapping_builder import build_mapping_file
from .step10_merge_vision_into_mapping import merge_vision_matches_into_mapping
from .vision_summery import run_vision_mapping
from .vision_summary_tabelle import run_vision_tables


VISION_MATCH_FILENAME = "campo_valore_vision.json"

def _filled_counts(mapping_path: Path) -> dict[str, int]:
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    filled_fields = 0
    filled_tables = 0
    total_fields = 0
    total_tables = 0

    for row in rows:
        item_type = str(row.get("item_type", "")).strip()
        if item_type == "field":
            total_fields += 1
        elif item_type == "table_cell":
            total_tables += 1
        else:
            continue

        answer = str(row.get("answer", "") or "").strip()
        if answer in {"", "N/D"}:
            continue
        if item_type == "field":
            filled_fields += 1
        elif item_type == "table_cell":
            filled_tables += 1

    return {
        "total_fields": total_fields,
        "total_tables": total_tables,
        "filled_fields": filled_fields,
        "filled_tables": filled_tables,
    }


def map_bundle_fields_vision(
    bundle: Dict[str, Any],
    xml_json_path: str | Path,
    output_dir: str | Path,
    *,
    m1_dir: str | Path,
) -> Dict[str, Any]:
    print(f"[LLM][vision-mapping] start - bundle={bundle['base_name']}")
    build_res = build_mapping_file(bundle, xml_json_path, output_dir)

    base_name = str(bundle.get("base_name") or "").strip()
    if not base_name:
        raise ValueError("bundle.base_name mancante.")

    image_dir = Path(m1_dir) / f"{base_name}_ANNOTAZIONI"
    vision_out = Path(output_dir) / VISION_MATCH_FILENAME
    vision_payload = run_vision_mapping(
        image_dir=image_dir,
        data_json_path=xml_json_path,
        out_json_path=vision_out,
    )
    stats = (vision_payload or {}).get("stats") or {}
    print(f"[LLM][vision-mapping] vision_found={stats.get('filled')}/{stats.get('total')}")
    tables_out = Path(output_dir) / "campi_tabelle.json"
    tables_detect_out = Path(output_dir) / "tables_output.json"
    tables_filled_out = Path(output_dir) / "tables_filled_output.json"

    tables_payload = run_vision_tables(
        image_dir=image_dir,
        data_json_path=xml_json_path,
        out_json_path=tables_out,
        out_json_detect_path=tables_detect_out,
        out_json_filled_path=tables_filled_out,
    )

    tstats = (tables_payload or {}).get("stats") or {}
    print(f"[LLM][vision-tables] vision_found={tstats.get('filled')}/{tstats.get('total')}")

    mapping_path = Path(output_dir) / "campo_valore.json"
    before = _filled_counts(mapping_path) if mapping_path.exists() else {"total_fields": 0, "total_tables": 0, "filled_fields": 0, "filled_tables": 0}
    merge_res = merge_vision_matches_into_mapping(
        mapping_path=mapping_path,
        vision_match_path=vision_out,
    )
    after = _filled_counts(mapping_path) if mapping_path.exists() else before
    delta_fields = after["filled_fields"] - before["filled_fields"]
    delta_tables = after["filled_tables"] - before["filled_tables"]
    print(
        f"[LLM][vision-mapping] merged_into_mapping={merge_res['filled_count']}/{build_res['item_count']} "
        f"(fields +{delta_fields}/{after['total_fields']}, tables +{delta_tables}/{after['total_tables']})"
    )

    return {
        "base_name": build_res["base_name"],
        "output_path": build_res["output_path"],
        "item_count": build_res["item_count"],
        "vision_match_output": str(vision_out),
        "filled_count": merge_res["filled_count"],
    }
