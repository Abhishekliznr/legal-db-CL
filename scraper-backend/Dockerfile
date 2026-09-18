# Heavy image: Playwright + Chromium, PyMuPDF, ddddocr, Tesseract, Azure Blob SDK.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries and system dependencies
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 3000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "3000"]
