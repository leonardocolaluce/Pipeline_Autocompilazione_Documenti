FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-common \
        poppler-utils \
        libgl1 \
        libgl1-mesa-dri \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Crea venv per OCR con PaddleOCR
RUN python -m venv paddle311
RUN paddle311/bin/pip install --upgrade pip \
    && paddle311/bin/pip install --no-cache-dir -r m1_pipeline/ocr/requirements_ocr.txt

EXPOSE 7860
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "7860"]
