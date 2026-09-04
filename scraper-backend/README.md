# 🕷️ scraper-backend — Court Scraper & Ingestion Engine

> High-throughput legal judgment scraper, AI metadata extraction engine (Bronze & Silver layers), Azure Blob streaming, and Citator precedence graph ingestion. Heavy service (Playwright, PyMuPDF, ddddocr, Azure Blob SDK). This is a fully standalone service with its own Dockerfile, requirements, environment, and Postgres instance. It does not share any code with `../api-backend`.

---

## 🏗️ Pipeline Architecture

```mermaid
graph TD
    SCR["🕷️ scraper-backend API<br>/api/scraper/start|status|jobs|cancel"] -->|Spawns Background Job| Pipeline

    subgraph Pipeline [4-Phase Automated Scraper Pipeline]
        P1["Phase 1: Court Scraper<br>(Playwright / Date Filter / In-Memory Stream)"]
        P2["Phase 2: Bronze Layer Sync<br>(Raw Case Archives)"]
        P3["Phase 3: Silver AI Metadata<br>(CaseNotes, Acts, Sections, Citations)"]
        P4["Phase 4: Database Ingestion<br>(UUIDs, Master Judges, Citator Graph)"]

        P1 --> P2 --> P3 --> P4
    end

    subgraph Storage [Cloud & Relational Storage]
        AZURE_PDF["☁️ Azure Blob (PDFs)<br>liznr-legal-dev-judgment-pdfs"]
        AZURE_RAW["☁️ Azure Blob (JSON)<br>liznr-legal-dev-raw-judgments"]
        PG[("🐘 PostgreSQL (legal_db)")]
    end

    P1 -->|Live Stream (Zero Disk)| AZURE_PDF
    P2 -->|Accumulative Merge| AZURE_RAW
    P3 -->|Silver Metadata Sync| AZURE_RAW
    P4 -->|Auto-Ingest| PG
```

---

## ⚡ Quickstart with Docker (Recommended)

`docker-compose.yml` in this folder brings up **its own** PostgreSQL instance plus the scraper service — no other folder is required.

```bash
cd scraper-backend

# 1. Setup your environment variables
cp .env.example .env

# 2. Build and start Postgres + scraper-backend
docker-compose up --build -d

# 3. Initialize the database schema
docker-compose exec scraper-backend python db_manager.py init
```

* **Scraper Backend**: [http://localhost:8081](http://localhost:8081) · Swagger: [http://localhost:8081/docs](http://localhost:8081/docs)
* **PostgreSQL**: `localhost:5433` (`legal_db`) — mapped to a different host port than api-backend's own Postgres so both can run side by side without a conflict.

---

## ⚙️ Environment Configuration (`.env`)

```env
# PostgreSQL Database Configuration
POSTGRES_USER=postgres
POSTGRES_PASSWORD=password123
POSTGRES_DB=legal_db
DB_CONNECTION=postgresql://postgres:password123@db:5432/legal_db

# Optional AI API Key (Groq) — used for PDF metadata extraction
GROQ_API_KEY=

# Azure Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING=
AZURE_CONTAINER_NAME=liznr-legal-dev-raw-judgments
AZURE_PDF_CONTAINER_NAME=liznr-legal-dev-judgment-pdfs
```

---

## 💻 Manual Setup & Local Execution (Without Docker)

### 📋 Prerequisites:
* **Python 3.10 or 3.11** installed
* **PostgreSQL Database** running locally or remotely (e.g. `localhost:5432` or cloud PostgreSQL)

```bash
cd scraper-backend
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

uvicorn api:app --reload --port 8001
```

### 🗄️ Initialize Database Tables & Schema (run once):
```bash
cd scraper-backend
python db_manager.py init
```

### 🌐 Access Without Docker:
* **Scraper Backend Docs**: [http://localhost:8001/docs](http://localhost:8001/docs)

---

## 📡 Admin Scraper REST API Reference (port 8001)

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

## 🖥️ Frontend Integration (`legal-ui`)

```env
# legal-ui/.env.local
NEXT_PUBLIC_SCRAPER_API_URL=http://localhost:8081
```

---

## 📂 Project Structure

```text
scraper-backend/
├── api.py                               # FastAPI app entrypoint
├── main.py                              # Interactive CLI scraper entrypoint
├── db_manager.py                        # Connection, schema, scraper-job tracking
├── normalizer.py                        # Name/citation normalization helpers
├── ingestion.py                         # Case ingestion pipeline (writes cases/parties/citations)
├── court_manager.py / scraper_pipeline.py / citator.py / azure_blob.py
├── routers/scraper_router.py
├── static/index.html                    # "Backend is up and running" landing page
├── app/
│   └── SUPREME_COURT_OF_INDIA_SCRAPER/
│       ├── supreme_court.py             # Playwright Scraper Engine
│       └── pdf_metadata_extractor.py    # AI CaseNote & Statutory Extractor
├── requirements.txt
├── Dockerfile
├── docker-compose.yml                    # Own Postgres + this service
├── .env.example
└── .dockerignore
```

---

## 🔗 Related Service

`../api-backend` is a separate, fully standalone service (own Dockerfile, requirements, env, and Postgres instance) that serves read-only case search/filters. It shares no code with this service. If you need api-backend to serve the cases this scraper ingests, point both services' `DB_CONNECTION` at the same PostgreSQL database (instead of each running its own bundled `db` container).

---

## 👥 Contributors & Maintainers
* **Backend & Scraper Engineer**: Abdeali ([@Abdey21](https://github.com/Abdey21))
* **Organization**: [Liznr Labs Org](https://github.com/LiznrLabsOrg)
