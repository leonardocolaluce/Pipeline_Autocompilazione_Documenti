import os
import sys
import json

# Forza stdout in UTF-8 per evitare errori con caratteri non-ASCII su Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"


def to_bbox(points):
    """Converte lista di 4 punti {x, y} in [x, y, w, h]."""
    if not points or len(points) < 2:
        return [0, 0, 0, 0]
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    x = min(xs)
    y = min(ys)
    w = max(xs) - x
    h = max(ys) - y
    return [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]


def to_points_from_box(box):
    """Normalizza il bounding box restituito da PaddleOCR in lista di 4 punti {x, y}."""
    if not isinstance(box, (list, tuple)):
        return None

    # Rettangolo piatto [x1, y1, x2, y2]
    if len(box) == 4 and all(not isinstance(v, (list, tuple)) for v in box):
        try:
            x1, y1, x2, y2 = map(float, box)
            return [
                {"x": x1, "y": y1},
                {"x": x2, "y": y1},
                {"x": x2, "y": y2},
                {"x": x1, "y": y2},
            ]
        except Exception:
            return None

    # Quadrilatero [[x, y], [x, y], [x, y], [x, y]]
    if len(box) == 4 and all(isinstance(v, (list, tuple)) and len(v) == 2 for v in box):
        try:
            return [{"x": float(x), "y": float(y)} for x, y in box]
        except Exception:
            return None

    return None


def get_page_json(res):
    """Estrae il dizionario grezzo dal risultato PaddleOCR."""
    if hasattr(res, "json"):
        try:
            data = res.json
            if callable(data):
                data = data()
            if isinstance(data, str):
                return json.loads(data)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    if hasattr(res, "res") and isinstance(res.res, dict):
        return res.res

    return {"raw": str(res)}


def extract_blocks(page_json, page_number):
    """Estrae i blocchi da una pagina OCR e li converte nel formato standard."""
    blocks = []

    res = page_json.get("res", page_json)

    texts = None
    scores = None
    boxes = None

    for key in ("rec_texts", "texts", "ocr_texts"):
        if isinstance(res.get(key), list):
            texts = res[key]
            break

    for key in ("rec_scores", "scores", "ocr_scores"):
        if isinstance(res.get(key), list):
            scores = res[key]
            break

    for key in ("rec_boxes", "dt_polys", "dt_boxes", "boxes", "polys", "text_boxes"):
        if isinstance(res.get(key), list):
            boxes = res[key]
            break

    if texts is None or boxes is None:
        return blocks

    n = min(len(texts), len(boxes))
    for i in range(n):
        text = str(texts[i]).strip()
        if not text:
            continue

        points = to_points_from_box(boxes[i])
        bbox = to_bbox(points) if points else [0, 0, 0, 0]

        confidence = None
        if isinstance(scores, list) and i < len(scores):
            try:
                confidence = round(float(scores[i]), 4)
            except Exception:
                pass

        blocks.append({
            "text":       text,
            "bbox":       bbox,
            "page":       page_number,
            "confidence": confidence,
            "source":     "ocr",
        })

    return blocks


def run(pdf_path):
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(lang="it")
    results = ocr.predict(pdf_path)

    all_blocks = []
    for page_idx, res in enumerate(results, start=1):
        page_json = get_page_json(res)
        blocks = extract_blocks(page_json, page_idx)
        all_blocks.extend(blocks)

    return all_blocks


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Uso: paddle_worker.py <percorso_pdf>"}))
        sys.exit(1)

    pdf_path = sys.argv[1]

    try:
        blocks = run(pdf_path)
        print(json.dumps(blocks, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
