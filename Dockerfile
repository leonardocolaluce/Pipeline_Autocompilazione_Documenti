FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN echo "deb http://deb.debian.org/debian trixie main contrib" >> /etc/apt/sources.list \
    && apt-get update \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-common \
        poppler-utils \
        libgl1 \
        libgl1-mesa-dri \
        fontconfig \
        fonts-dejavu \
        fonts-dejavu-extra \
        fonts-liberation \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-noto-core \
        fonts-noto-extra \
        fonts-noto-ui-core \
        fonts-noto-ui-extra \
        fonts-noto-color-emoji \
        fonts-texgyre \
        fonts-texgyre-math \
        fonts-freefont-ttf \
        fonts-opensymbol \
        fonts-symbola \
        fonts-urw-base35 \
        ttf-mscorefonts-installer \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*




COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt
    
COPY fonts/ /usr/share/fonts/truetype/custom/
RUN fc-cache -f -v

COPY . .



# Crea venv per OCR con PaddleOCR
RUN python -m venv paddle311
RUN paddle311/bin/pip install --upgrade pip \
    && paddle311/bin/pip install --no-cache-dir -r m1_pipeline/ocr/requirements_ocr.txt

EXPOSE 7860
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "7860"]
