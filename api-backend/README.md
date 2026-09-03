# ⚙️ api-backend — Legal Case Search & Citator API

> Read-only, DB-only FastAPI service for case search, dynamic filters, and citator lookups. No Playwright/scraping dependencies — this is a fully standalone service with its own Dockerfile, requirements, environment, and Postgres instance. It does not share any code with `../scraper-backend`.

---

## ⚡ Quickstart with Docker (Recommended)

`docker-compose.yml` in this folder brings up **its own** PostgreSQL instance plus the API service — no other folder is required.

```bash
cd api-backend

# 1. Setup your environment variables
cp .env.example .env

# 2. Start Postgres + api-backend
docker-compose up -d
```

* **API Backend**: [http://localhost:8080](http://localhost:8080) · Swagger: [http://localhost:8080/docs](http://localhost:8080/docs)
* **PostgreSQL**: `localhost:5432` (`legal_db`)

---

## ⚙️ Environment Configuration (`.env`)

```env
# PostgreSQL Database Configuration
POSTGRES_USER=postgres
POSTGRES_PASSWORD=password123
POSTGRES_DB=legal_db
DB_CONNECTION=postgresql://postgres:password123@db:5432/legal_db
```

---

## 💻 Manual Setup & Local Execution (Without Docker)

### 📋 Prerequisites:
* **Python 3.10 or 3.11** installed
* **PostgreSQL Database** running locally or remotely (e.g. `localhost:5432` or cloud PostgreSQL)

```bash
cd api-backend
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn api:app --reload --port 8000
```

### 🗄️ Initialize Database Tables & Schema (run once):
```bash
cd api-backend
python db_manager.py init
```

### 🌐 Access Without Docker:
* **API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 🔍 Case Search REST API Reference (port 8000)

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

```env
# legal-ui/.env.local
NEXT_PUBLIC_API_URL=http://localhost:8080
```

---

## 📂 Project Structure

```text
api-backend/
├── api.py                  # FastAPI app entrypoint
├── db_manager.py           # Connection, schema, scraper-job read helpers
├── normalizer.py           # Name/citation normalization helpers (used by db_manager)
├── routers/
│   ├── filter_router.py
│   └── search_router.py
├── static/index.html       # "Backend is up and running" landing page
├── requirements.txt
├── Dockerfile
├── docker-compose.yml       # Own Postgres + this service
├── .env.example
└── .dockerignore
```

---

## 🔗 Related Service

`../scraper-backend` is a separate, fully standalone service (own Dockerfile, requirements, env, and Postgres instance) that scrapes court judgments and writes case data. It shares no code with this service. If you need this API to serve data ingested by the scraper, point this service's `DB_CONNECTION` at the same PostgreSQL database the scraper writes to (instead of running each service's bundled `db` container).

---

## 👥 Contributors & Maintainers
* **Backend & Scraper Engineer**: Abdeali ([@Abdey21](https://github.com/Abdey21))
* **Organization**: [Liznr Labs Org](https://github.com/LiznrLabsOrg)
