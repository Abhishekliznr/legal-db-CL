"""
Admin CRUD for `courts` + `court_scrape_config`
-------------------------------------------------
- GET  /api/courts                    : list courts + their scrape config
- PUT  /api/courts/{court_id}/config  : create/update a court's scrape config
                                         (adapter, free-form config, active flag)

No scraping happens through this router — it only manages the config rows
that routers/scraper_router.py's `_ADAPTER_REGISTRY` reads once the
adapters exist.
"""

import logging
from typing import Any, Dict, Optional

import psycopg2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import court_config

logger = logging.getLogger("scraper_backend_v2.court_config")

router = APIRouter(prefix="/api/courts", tags=["Court Scrape Config"])


class CourtScrapeConfigUpdate(BaseModel):
    adapter: str = Field(..., description="Adapter name, e.g. 'supreme_court' — must match a key in routers/scraper_router.py's _ADAPTER_REGISTRY")
    config: Optional[Dict[str, Any]] = Field(None, description="That adapter's own free-form settings (stored as JSONB), e.g. whatever a High Court's own adapter needs beyond headless")
    is_active: bool = True
    notes: Optional[str] = None


@router.get("")
def list_courts(active_only: bool = False):
    try:
        return {"courts": court_config.list_courts(active_only=active_only)}
    except RuntimeError:
        # Connection pool not initialized — startup couldn't reach Postgres.
        logger.exception("DB connection pool unavailable while listing courts")
        raise HTTPException(status_code=503, detail="Database connection is not available. Check /health.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing courts")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing courts")
        raise HTTPException(status_code=500, detail="Failed to list courts.")


@router.put("/{court_id}/config")
def upsert_court_config(court_id: int, payload: CourtScrapeConfigUpdate):
    try:
        court_config.upsert_court_scrape_config(
            court_id=court_id,
            adapter=payload.adapter,
            config=payload.config,
            is_active=payload.is_active,
            notes=payload.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError:
        logger.exception("DB connection pool unavailable while updating court %s config", court_id)
        raise HTTPException(status_code=503, detail="Database connection is not available. Check /health.")
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(status_code=404, detail=f"No court with court_id={court_id}.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while updating court %s config", court_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while updating court %s config", court_id)
        raise HTTPException(status_code=500, detail="Failed to update court scrape config.")
    return {"status": "success", "court_id": court_id}
