# Pinned to Python 3.10: en_legal_ner_sm requires spacy<3.3.0, which has no
# Python 3.11 wheel -- kept as its own service (rather than downgrading
# scraper-backend) so this old pin never touches the rest of the stack.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:3000", "--timeout", "120", "main:app"]
