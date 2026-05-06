import os
from pathlib import Path
from typing import Any, Dict

from .step08_mapping_builder import build_mapping_file
from .step09_mapping_llm_filler import fill_mapping_file


def map_bundle_fields(bundle: Dict[str, Any], xml_json_path: str | Path, output_dir: str | Path) -> Dict[str, Any]:
    print(f"[LLM][mapping] start - mode=mistral - bundle={bundle['base_name']}")
    build_res = build_mapping_file(bundle, xml_json_path, output_dir)
    if os.getenv("PIPELINE_SKIP_LLM", "").strip():
        return {
            "base_name": build_res["base_name"],
            "output_path": build_res["output_path"],
            "item_count": build_res["item_count"],
            "non_nd_count": 0,
        }
    fill_res = fill_mapping_file(output_dir, xml_json_path)
    return {
        "base_name": build_res["base_name"],
        "output_path": build_res["output_path"],
        "item_count": build_res["item_count"],
        "non_nd_count": fill_res["non_nd_count"],
    }
