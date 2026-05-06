import json
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List
from xml.sax.saxutils import escape


EXCEL_PROVISIONAL_FILENAME = "campi_compilati_confronto.xlsx"


def _column_name(index: int) -> str:
    result = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _display_field_name(row: Dict[str, object]) -> str:
    label = str(row.get("label") or "").strip()
    if label:
        return label

    row_labels = row.get("row_labels")
    if isinstance(row_labels, list):
        labels = [str(item.get("testo", item)).strip() for item in row_labels if str(item.get("testo", item)).strip()]
        if labels:
            return " | ".join(labels)

    context = str(row.get("context") or "").strip()
    if context:
        return context[:240]

    return str(row.get("item_id") or "")


def _display_location(row: Dict[str, object]) -> str:
    parts: List[str] = []
    page = row.get("page")
    if page not in (None, ""):
        parts.append(f"pag. {page}")
    bbox = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        parts.append(f"bbox=({bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]})")

    table_index = row.get("table_index")
    row_index = row.get("row_index")
    col_index = row.get("col_index")
    if table_index not in (None, ""):
        table_part = f"tabella {table_index}"
        if row_index not in (None, ""):
            table_part += f", riga {row_index}"
        if col_index not in (None, ""):
            table_part += f", colonna {col_index}"
        parts.append(table_part)

    return " | ".join(parts) if parts else ""


def _sheet_xml(rows: Iterable[List[str]], col_count: int) -> str:
    rows_xml: List[str] = []
    for row_idx, values in enumerate(rows, start=1):
        cells: List[str] = []
        for col_idx, value in enumerate(values, start=1):
            cell_ref = f"{_column_name(col_idx)}{row_idx}"
            inline = escape(value)
            cells.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">{inline}</t></is></c>'
            )
        rows_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')
    last_col = _column_name(max(1, col_count))
    dimension = f"A1:{last_col}{max(1, len(rows_xml))}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<dimension ref=\"{dimension}\"/>"
        "<sheetViews><sheetView workbookViewId=\"0\"/></sheetViews>"
        "<sheetFormatPr defaultRowHeight=\"15\"/>"
        f"<sheetData>{''.join(rows_xml)}</sheetData>"
        "</worksheet>"
    )


def _write_multi_sheet_xlsx(output_path: Path, sheets: List[tuple[str, List[List[str]]]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index, _sheet in enumerate(sheets, start=1)
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _rows) in enumerate(sheets, start=1)
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index, _sheet in enumerate(sheets, start=1)
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{sheet_overrides}"
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:title>Confronto campi compilati</dc:title>"
            "</cp:coreProperties>",
        )
        archive.writestr(
            "docProps/app.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>OpenAI Codex</Application>"
            "</Properties>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{workbook_sheets}</sheets>"
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}"
            "</Relationships>",
        )
        for index, (_name, rows) in enumerate(sheets, start=1):
            col_count = max((len(row) for row in rows), default=4)
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows, col_count))


def _rows_from_mapping(mapping_json_path: str | Path) -> List[List[str]]:
    mapping_path = Path(mapping_json_path).resolve()
    with open(mapping_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    rows = payload.get("rows") or []
    export_rows: List[List[str]] = [["Campo", "Risposta", "Dove si trova", "Stato"]]
    for row in rows:
        answer = str(row.get("answer") or "").strip() or "N/D"
        export_rows.append(
            [
                _display_field_name(row),
                answer,
                _display_location(row),
                "compilato" if answer != "N/D" else "non compilato",
            ]
        )
    return export_rows


def export_mapping_comparison_to_xlsx(
    provisional_mapping_json_path: str | Path,
    final_mapping_json_path: str | Path,
    output_path: str | Path,
) -> str:
    xlsx_path = Path(output_path).resolve()
    sheets = [
        ("Prima", _rows_from_mapping(provisional_mapping_json_path)),
        ("Dopo", _rows_from_mapping(final_mapping_json_path)),
    ]
    _write_multi_sheet_xlsx(xlsx_path, sheets)
    return str(xlsx_path)


def export_provisional_mapping_to_xlsx(mapping_json_path: str | Path, output_path: str | Path) -> str:
    return export_mapping_comparison_to_xlsx(mapping_json_path, mapping_json_path, output_path)
