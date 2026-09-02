# Lean image: case search/filter API only — no Playwright, no browser runtime.
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

COPY api-backend/requirements.txt ./api-backend/requirements.txt
RUN pip install --no-cache-dir -r api-backend/requirements.txt

COPY api-backend/ ./api-backend/

ENV PYTHONPATH=/app
WORKDIR /app/api-backend

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
