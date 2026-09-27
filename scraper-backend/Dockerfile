# Heavy image: Playwright + Chromium, PyMuPDF, ddddocr, Tesseract, Azure Blob SDK, spaCy + Legal NER models.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    tesseract-ocr \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY install_ner_models.sh ./install_ner_models.sh
RUN sh ./install_ner_models.sh

# Install Playwright browser binaries and system dependencies
RUN playwright install --with-deps chromium

COPY . .
RUN chmod +x docker-entrypoint.sh

# One batch per container: the K8s Job passes --batch-id (see worker.py).
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "-m", "worker", "--help"]
