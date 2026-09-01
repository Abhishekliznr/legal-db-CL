# 🏛️ Liznr Legal — Automated Court Scraper & Citator Pipeline

> High-throughput legal judgment scraper, AI metadata extraction engine (Bronze & Silver layers), Azure Blob streaming, Citator precedence graph, and normalized PostgreSQL relational database.

---

## 🏗️ System Architecture

```mermaid
graph TD
    UI["🖥️ Legal UI (Next.js)<br>/admin/scraper"] -->|REST API| API["⚙️ FastAPI Backend Service<br>(Port 8000)"]
    
    subgraph Pipeline [4-Phase Automated Scraper Pipeline]
        P1["Phase 1: Supreme Court Scraper<br>(Playwright / Date Filter / In-Memory Stream)"]
        P2["Phase 2: Bronze Layer Sync<br>(Raw Case Archives)"]
        P3["Phase 3: Silver AI Metadata<br>(CaseNotes, Acts, Sections, Citations)"]
        P4["Phase 4: Database Ingestion<br>(UUIDs, Master Judges, Citator Graph)"]
        
        P1 --> P2 --> P3 --> P4
    end

    API -->|Spawns Background Job| Pipeline
    
    subgraph Storage [Cloud & Relational Storage]
        AZURE_PDF["☁️ Azure Blob (PDFs)<br>liznr-legal-dev-judgment-pdfs"]
        AZURE_RAW["☁️ Azure Blob (JSON)<br>liznr-legal-dev-raw-judgments"]
        PG[("🐘 PostgreSQL (legal_db)<br>Port 5432")]
    end

    P1 -->|Live Stream (Zero Disk)| AZURE_PDF
    P2 -->|Accumulative Merge| AZURE_RAW
    P3 -->|Silver Metadata Sync| AZURE_RAW
    P4 -->|Auto-Ingest| PG
```

---

## ⚡ Quickstart with Docker (Recommended)

Start the entire backend and PostgreSQL database with **one single command**:

```bash
# 1. Clone the repository
git clone https://github.com/LiznrLabsOrg/legal-db.git
cd legal-db

# 2. Setup your environment variables
cp .env.example .env

# 3. Start PostgreSQL and FastAPI Backend
docker-compose up -d
```

### 🌐 Service Endpoints:
* **FastAPI Backend & Scraper API**: [http://localhost:8000](http://localhost:8000)
* **Interactive Swagger API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **PostgreSQL Database**: `localhost:5432` (`legal_db`)

---

## 🚀 Steps to Run on DEV Server

```bash
# 1. Clone repository & navigate into directory:
git clone https://github.com/LiznrLabsOrg/legal-db.git
cd legal-db

# 2. Setup your environment configuration:
cp .env.example .env

# 3. Build and start both services (PostgreSQL Database + FastAPI Backend):
docker-compose up --build -d

# 4. Initialize the database schema:
docker-compose exec app python backend/db_manager.py init

# 5. Access the services:
# - Web UI / API: http://<SERVER_IP>:8000
# - Swagger Docs: http://<SERVER_IP>:8000/docs
```
---

## ⚙️ Environment Configuration (`.env`)

Create a `.env` file in the root directory:

```env
# ==========================================
# Database Configuration
# ==========================================
POSTGRES_USER=postgres
POSTGRES_PASSWORD=1234
POSTGRES_DB=legal_db
POSTGRES_HOST=db
POSTGRES_PORT=5432
DB_CONNECTION=postgresql://postgres:1234@db:5432/legal_db

# ==========================================
# Azure Blob Storage Configuration
# ==========================================
AZURE_STORAGE_CONNECTION_STRING=your_azure_storage_connection_string_here
AZURE_PDF_CONTAINER_NAME=liznr-legal-dev-judgment-pdfs
AZURE_RAW_CONTAINER_NAME=liznr-legal-dev-raw-judgments

# ==========================================
# AI / OpenAI Configuration (Optional)
# ==========================================
OPENAI_API_KEY=your_openai_api_key_here
```

---

## 💻 Manual Setup & Local Execution (Without Docker)

If you prefer to run the project natively using Python on your local machine without Docker:

### 📋 Prerequisites:
* **Python 3.10 or 3.11** installed
* **PostgreSQL Database** running locally or remotely (e.g. `localhost:5432` or cloud PostgreSQL)

---

### 🛠️ Step-by-Step Instructions:

#### 1️⃣ Clone the Repository & Navigate:
```bash
git clone https://github.com/LiznrLabsOrg/legal-db.git
cd legal-db
```

#### 2️⃣ Create & Activate Virtual Environment:
```bash
# Create virtual environment
python -m venv venv

# Activate on Windows (PowerShell):
.\venv\Scripts\Activate.ps1

# Activate on Windows (CMD):
.\venv\Scripts\activate.bat

# Activate on Linux / macOS:
source venv/bin/activate
```

#### 3️⃣ Install Dependencies & Playwright Browsers:
```bash
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

#### 4️⃣ Configure `.env`:
Create `.env` and set `DB_CONNECTION` to your local PostgreSQL instance (`localhost:5432` instead of `db:5432`):
```env
DB_CONNECTION=postgresql://postgres:1234@localhost:5432/legal_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=1234
POSTGRES_DB=legal_db
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

AZURE_STORAGE_CONNECTION_STRING=your_azure_storage_connection_string
AZURE_PDF_CONTAINER_NAME=liznr-legal-dev-judgment-pdfs
AZURE_RAW_CONTAINER_NAME=liznr-legal-dev-raw-judgments
```

#### 5️⃣ Initialize Database Tables & Schema:
```bash
python backend/db_manager.py init
```

#### 6️⃣ Start the FastAPI Backend Server:
```bash
uvicorn backend.api:app --reload --port 8000
```

### 🌐 Access Without Docker:
* **Interactive Swagger API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **API Root Healthcheck**: [http://localhost:8000/api/scraper/courts](http://localhost:8000/api/scraper/courts)

---

## 📡 Admin Scraper REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/scraper/start` | Trigger a new background scraping job with date range & court filters. |
| `GET` | `/api/scraper/status/{job_id}` | Poll real-time progress, log stream, cases count, and Azure URLs. |
| `GET` | `/api/scraper/jobs` | Retrieve the execution history of past scraping runs. |
| `POST` | `/api/scraper/stop/{job_id}` | Cancel/abort an active running scraping job. |
| `GET` | `/api/scraper/courts` | List supported courts and status codes. |
| `GET` | `/api/cases/search` | Search judgments across citations, judges, acts, and full-text summaries. |

---

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

To connect the Next.js Frontend (`legal-ui`) to this Scraper backend:

1. In `legal-ui/.env.local`, set:
   ```env
   NEXT_PUBLIC_API_URL=http://localhost:8000
   ```
2. Start the frontend:
   ```bash
   npm run dev
   ```
3. Open **`http://localhost:3000/admin/scraper`** or click **"Court Scraper"** in the sidebar.

---

## 📂 Project Structure

```text
├── backend/
│   ├── app/
│   │   └── SUPREME_COURT_OF_INDIA_SCRAPER/
│   │       ├── supreme_court.py            # Playwright Scraper Engine
│   │       └── pdf_metadata_extractor.py   # AI CaseNote & Statutory Extractor
│   ├── api.py                             # FastAPI REST Endpoints & Citator API
│   ├── scraper_pipeline.py                # 4-Phase Background Worker Orchestrator
│   ├── azure_blob.py                      # Azure Blob Stream & Accumulative JSON Merge
│   ├── citator.py                         # Precedent Analyzer & Treatment Tagger
│   ├── db_manager.py                      # PostgreSQL Relational Schema & Ingestion
│   └── normalizer.py                      # Party & Citation Data Cleanser
├── docker-compose.yml                     # Multi-container orchestration (App + DB)
├── Dockerfile                             # Python 3.11 + Playwright Production Image
├── requirements.txt                       # Python dependencies
└── README.md                              # Documentation
```

---

## 👥 Contributors & Maintainers
* **Backend & Scraper Engineer**: Abdeali ([@Abdey21](https://github.com/Abdey21))
* **Organization**: [Liznr Labs Org](https://github.com/LiznrLabsOrg)
