from pathlib import Path
import json

from .step11_writer_docx_fields import compile_overlay_docx
from .step11_writer_docx_tables import compile_tables_only


def _count_answers(mapping_json: Path) -> int:
    data = json.loads(mapping_json.read_text(encoding="utf-8"))
    return sum(
        1 for r in data.get("rows", [])
        if str(r.get("answer", "")).strip() not in {"", "N/D"}
    )


def write_docx_from_mapping(source_docx, mapping_json, out_docx):
    src = Path(source_docx)
    js = Path(mapping_json)
    out = Path(out_docx)

    tmp_fields = out.with_name(out.stem + ".__fields.docx")

    compile_overlay_docx(
        src_docx=src,
        json_path=js,
        out_docx=tmp_fields,
        coords="auto",
        strategy_override="auto",
    )

    compile_tables_only(
        src_docx=tmp_fields,
        json_path=js,
        out_docx=out,
    )

    return {
        "output_path": str(out),
        "replaced_count": _count_answers(js),
    }


def write_docx_preview_from_answers_json(source_docx, mapping_json, out_docx, *, color_hex="0000FF"):
    return write_docx_from_mapping(source_docx, mapping_json, out_docx)
