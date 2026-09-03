# 🏛️ Liznr Legal — Automated Court Scraper & Citator Pipeline

This repository holds two **fully standalone** services. Each owns its own Dockerfile, `docker-compose.yml`, requirements, environment files, `.gitignore`, `.dockerignore`, and README — there is no shared code or shared configuration between them.

* **[`api-backend/`](api-backend/README.md)** — read-only case search, dynamic filters & citator API (lightweight, no Playwright). See [api-backend/README.md](api-backend/README.md) for setup, env vars, and API reference.
* **[`scraper-backend/`](scraper-backend/README.md)** — court judgment scraper, AI metadata extraction, Azure Blob sync & DB ingestion (heavy: Playwright, PyMuPDF, ddddocr). See [scraper-backend/README.md](scraper-backend/README.md) for setup, env vars, and API reference.

Each service ships with its own bundled PostgreSQL container (`docker-compose.yml` inside each folder) so it can run entirely on its own. If you want `api-backend` to serve the cases `scraper-backend` ingests, point both services' `DB_CONNECTION` at the same external PostgreSQL database instead of running each one's bundled `db` container.

## 👥 Contributors & Maintainers
* **Backend & Scraper Engineer**: Abdeali ([@Abdey21](https://github.com/Abdey21))
* **Organization**: [Liznr Labs Org](https://github.com/LiznrLabsOrg)
