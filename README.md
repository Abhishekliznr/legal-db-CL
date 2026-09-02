# 🏛️ Liznr Legal — Automated Court Scraper & Citator Pipeline

> High-throughput legal judgment scraper, AI metadata extraction engine (Bronze & Silver layers), Azure Blob streaming, Citator precedence graph, and normalized PostgreSQL relational database — split into two independently deployable services sharing one database.

---

## 🏗️ System Architecture

```mermaid
graph TD
    UI["🖥️ Legal UI (Next.js)<br>/admin/scraper & /case-research"] -->|REST API| API["⚙️ api-backend<br>Case Search & Filters (Port 8000)"]
    UI -->|REST API| SCR["🕷️ scraper-backend<br>Court Scraper Engine (Port 8001)"]

    subgraph Pipeline [4-Phase Automated Scraper Pipeline — inside scraper-backend]
        P1["Phase 1: Supreme Court Scraper<br>(Playwright / Date Filter / In-Memory Stream)"]
        P2["Phase 2: Bronze Layer Sync<br>(Raw Case Archives)"]
        P3["Phase 3: Silver AI Metadata<br>(CaseNotes, Acts, Sections, Citations)"]
        P4["Phase 4: Database Ingestion<br>(UUIDs, Master Judges, Citator Graph)"]

        P1 --> P2 --> P3 --> P4
    end

    SCR -->|Spawns Background Job| Pipeline

    subgraph Storage [Cloud & Relational Storage]
        AZURE_PDF["☁️ Azure Blob (PDFs)<br>liznr-legal-dev-judgment-pdfs"]
        AZURE_RAW["☁️ Azure Blob (JSON)<br>liznr-legal-dev-raw-judgments"]
        PG[("🐘 PostgreSQL (legal_db)<br>Port 5432 — shared by both services")]
    end

    P1 -->|Live Stream (Zero Disk)| AZURE_PDF
    P2 -->|Accumulative Merge| AZURE_RAW
    P3 -->|Silver Metadata Sync| AZURE_RAW
    P4 -->|Auto-Ingest| PG
    API -->|Read-only queries| PG
```

`api-backend` and `scraper-backend` are two separate services with their own Dockerfiles, requirements, and containers — the only thing they share is the `shared/` Python package (DB connection, schema, scraper-job tracking) and the one PostgreSQL database. `api-backend` never installs Playwright/PyMuPDF/ddddocr, so its image is small and its build is fast.

---

## ⚡ Quickstart with Docker (Recommended)

Start PostgreSQL and both backend services with **one single command**:

```bash
# 1. Clone the repository
git clone https://github.com/LiznrLabsOrg/legal-db.git
cd legal-db

# 2. Setup your environment variables
cp .env.example .env

# 3. Start PostgreSQL, api-backend, and scraper-backend
docker-compose up -d
```

### 🌐 Service Endpoints:
* **API Backend (case search/filters)**: [http://localhost:8080](http://localhost:8080) · Swagger: [http://localhost:8080/docs](http://localhost:8080/docs)
* **Scraper Backend (admin scraping)**: [http://localhost:8081](http://localhost:8081) · Swagger: [http://localhost:8081/docs](http://localhost:8081/docs)
* **PostgreSQL Database**: `localhost:5432` (`legal_db`) — shared by both services

---

## 🚀 Steps to Run on DEV Server

```bash
# 1. Clone repository & navigate into directory:
git clone https://github.com/LiznrLabsOrg/legal-db.git
cd legal-db

# 2. Setup your environment configuration:
cp .env.example .env

# 3. Build and start all three services (PostgreSQL + api-backend + scraper-backend):
docker-compose up --build -d

# 4. Initialize the database schema (either service can run this — same shared schema):
docker-compose exec api-backend python -m shared.db_manager init

# 5. Access the services:
# - Case Search API : http://<SERVER_IP>:8080  (Swagger: :8080/docs)
# - Scraper API      : http://<SERVER_IP>:8081  (Swagger: :8081/docs)
```
---

## ⚙️ Environment Configuration (`.env`)

Both services read from the **same** root `.env` file (each container only uses the variables relevant to it):

```env
# ==========================================
# Database Configuration
# ==========================================
POSTGRES_USER=postgres
POSTGRES_PASSWORD=1234
POSTGRES_DB=legal_db
DB_CONNECTION=postgresql://postgres:1234@db:5432/legal_db

# ==========================================
# Azure Blob Storage Configuration (scraper-backend only)
# ==========================================
AZURE_STORAGE_CONNECTION_STRING=your_azure_storage_connection_string_here
AZURE_PDF_CONTAINER_NAME=liznr-legal-dev-judgment-pdfs
AZURE_RAW_CONTAINER_NAME=liznr-legal-dev-raw-judgments

# ==========================================
# AI / OpenAI Configuration (Optional, scraper-backend only)
# ==========================================
OPENAI_API_KEY=your_openai_api_key_here
```

---

## 💻 Manual Setup & Local Execution (Without Docker)

Each service has its own virtual environment and its own requirements file — set them up independently.

### 📋 Prerequisites:
* **Python 3.10 or 3.11** installed
* **PostgreSQL Database** running locally or remotely (e.g. `localhost:5432` or cloud PostgreSQL)

### 🛠️ api-backend (lightweight — no Playwright)

```bash
cd legal-db
python -m venv api-backend/venv
source api-backend/venv/bin/activate     # Windows: api-backend\venv\Scripts\activate
pip install -r api-backend/requirements.txt

# Repo root must be on PYTHONPATH so `shared` resolves:
export PYTHONPATH=$(pwd)                 # Windows (PowerShell): $env:PYTHONPATH = (Get-Location)

cd api-backend
uvicorn api:app --reload --port 8000
```

### 🛠️ scraper-backend (heavy — installs Playwright/Chromium)

```bash
cd legal-db
python -m venv scraper-backend/venv
source scraper-backend/venv/bin/activate
pip install -r scraper-backend/requirements.txt
playwright install chromium

export PYTHONPATH=$(pwd)

cd scraper-backend
uvicorn api:app --reload --port 8001
```

### 🗄️ Initialize Database Tables & Schema (run once, from either service's venv):
```bash
cd legal-db
PYTHONPATH=$(pwd) python -m shared.db_manager init
```

### 🌐 Access Without Docker:
* **API Backend Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Scraper Backend Docs**: [http://localhost:8001/docs](http://localhost:8001/docs)

---

## 📡 Admin Scraper REST API Reference (scraper-backend, port 8001)

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/scraper/start` | Trigger a new background scraping job with date range & court filters. |
| `GET` | `/api/scraper/status/{job_id}` | Poll real-time progress, log stream, cases count, and Azure URLs. |
| `GET` | `/api/scraper/jobs` | Retrieve the execution history of past scraping runs. |
| `POST` | `/api/scraper/cancel/{job_id}` | Cancel/abort an active running scraping job. |

### 📝 Example: Start Scraping Job Payload
`POST /api/scraper/start`
```json
{
  "court_id": "SCIN",
  "from_date": "2025-01-01",
  "to_date": "2025-01-05",
  "upload_azure": true,
  "stream_cloud": true,
  "extract_metadata": true
}
```

---

## 🔍 Case Search REST API Reference (api-backend, port 8000)

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/cases/filters` | Filter sidebar metadata (courts, treatment status, judges, acts, years) with live counts. `?include_inactive=true` also returns disabled filters. |
| `POST` | `/api/cases/filters` | Create a filter definition. `dataSource: "static"` filters accept an inline `options: [{label, value}]` array. |
| `GET` | `/api/cases/filters/{filter_id}` | Get one filter definition with its current options. |
| `PATCH` | `/api/cases/filters/{filter_id}` | Partially update a filter definition; including `options` fully replaces its option rows. |
| `DELETE` | `/api/cases/filters/{filter_id}` | Delete a filter definition (cascades its options). |
| `GET` | `/api/cases/searches` | Advanced boolean search-builder field metadata (all/any/exact/none-of-these-words). `?include_inactive=true` also returns disabled fields. |
| `POST` | `/api/cases/searches` | Create a search-field definition. |
| `GET` | `/api/cases/searches/{field_id}` | Get one search-field definition. |
| `PATCH` | `/api/cases/searches/{field_id}` | Partially update a search-field definition. |
| `DELETE` | `/api/cases/searches/{field_id}` | Delete a search-field definition. |
| `QUERY` | `/api/cases` | Search & list cases — filters, search fields, page, limit, sort in the request body (RFC 10008). Not renderable in Swagger UI yet; see `/openapi.json`. |
| `GET` | `/api/cases/{case_id}` | Full judgment metadata, provisions, and citations. |

> ⚠️ The `filters`/`searches` write endpoints (`POST`/`PATCH`/`DELETE`) have **no authentication** currently — anyone who can reach api-backend can rewrite the filter sidebar and search builder for every user. Gate this at the network/reverse-proxy level until real auth is added.

---

## 🖥️ Frontend Integration (`legal-ui`)

The Next.js frontend (`legal-ui`) now needs **two** API origins:

```env
# legal-ui/.env.local
NEXT_PUBLIC_API_URL=http://localhost:8080          # api-backend — case search/filters
NEXT_PUBLIC_SCRAPER_API_URL=http://localhost:8081  # scraper-backend — admin scraping
```

---

## 📂 Project Structure

```text
├── shared/                              # Imported by BOTH services — connection, schema, job tracking
│   ├── db_manager.py
│   └── normalizer.py
├── api-backend/                         # Case search & filters (lightweight, no Playwright)
│   ├── api.py
│   ├── routers/ (filter_router.py, search_router.py)
│   ├── static/index.html                # "Backend is up and running" landing page
│   ├── requirements.txt
│   └── Dockerfile
├── scraper-backend/                     # Court scraper & ingestion (heavy: Playwright, PyMuPDF, ddddocr)
│   ├── api.py
│   ├── main.py                          # Interactive CLI scraper entrypoint
│   ├── ingestion.py                     # Case ingestion pipeline (writes cases/parties/citations)
│   ├── court_manager.py / scraper_pipeline.py / citator.py / azure_blob.py
│   ├── routers/scraper_router.py
│   ├── static/index.html
│   ├── app/
│   │   └── SUPREME_COURT_OF_INDIA_SCRAPER/
│   │       ├── supreme_court.py            # Playwright Scraper Engine
│   │       └── pdf_metadata_extractor.py   # AI CaseNote & Statutory Extractor
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml                   # Orchestrates db + api-backend + scraper-backend
└── README.md
```

---

## 👥 Contributors & Maintainers
* **Backend & Scraper Engineer**: Abdeali ([@Abdey21](https://github.com/Abdey21))
* **Organization**: [Liznr Labs Org](https://github.com/LiznrLabsOrg)
