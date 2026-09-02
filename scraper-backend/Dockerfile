# Heavy image: Playwright + Chromium, PyMuPDF, ddddocr, Azure Blob SDK.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared DB connection/schema package, used by both services.
COPY shared/ ./shared/

COPY scraper-backend/requirements.txt ./scraper-backend/requirements.txt
RUN pip install --no-cache-dir -r scraper-backend/requirements.txt

# Install Playwright browser binaries and system dependencies
RUN playwright install --with-deps chromium

COPY scraper-backend/ ./scraper-backend/

ENV PYTHONPATH=/app
WORKDIR /app/scraper-backend

EXPOSE 8001
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8001"]
