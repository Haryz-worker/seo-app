# backend/app/main.py

from __future__ import annotations

import os
import time
import logging
from typing import Optional

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    status,
    Request,
    Query,
    BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import AnalyzeRequest, AnalyzeResponse, HealthResponse
from .service import run_pipeline
from .rate_limit import RateLimiter

from .schemas_crawler import CrawlBody, CrawlStatus
from .service_crawler import start_crawl_job, run_crawl_job_sync, get_crawl_status


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

API_TOKEN = os.getenv("API_TOKEN", "").strip()
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

log = logging.getLogger("onpage_api")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# FastAPI setup + CORS
# ---------------------------------------------------------------------------

app = FastAPI(title="OnPage SEO API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = RateLimiter(RATE_LIMIT_PER_MIN, window_seconds=60)


# ---------------------------------------------------------------------------
# Dependencies: authentication + rate limiting
# ---------------------------------------------------------------------------

def auth_dep(request: Request) -> None:
    """Simple bearer-token authentication."""
    if not API_TOKEN:
        return

    header_value = request.headers.get("Authorization", "")
    if not header_value.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = header_value.replace("Bearer ", "", 1).strip()
    if token != API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid token",
        )


def rate_limit_dep(request: Request) -> None:
    """Simple per-IP rate limiting."""
    ip = request.client.host if request.client else "unknown"
    if not limiter.hit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True, ts=int(time.time()))


# ---------------------------------------------------------------------------
# Analyze single page
# ---------------------------------------------------------------------------

@app.post(
    "/analyze-page",
    response_model=AnalyzeResponse,
    dependencies=[Depends(auth_dep), Depends(rate_limit_dep)],
)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    try:
        url_str = str(req.url)
        keyword_str: Optional[str] = (req.keyword or "").strip() or None

        log.info("[/analyze-page] url=%s keyword=%s", url_str, keyword_str)

        report_path, report = run_pipeline(url=url_str, keyword=keyword_str)

        log.info(
            "[/analyze-page] OK (report_path=%s, score=%s)",
            report_path,
            getattr(report, "score", None),
        )

        return AnalyzeResponse(
            ok=True,
            url=url_str,
            keyword=keyword_str,
            report_path=report_path,
            report=report,
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("[/analyze-page] Unhandled error")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Convenience endpoint to re-run and return raw JSON report
# ---------------------------------------------------------------------------

@app.get("/report")
def get_report(
    u: str = Query(..., description="Target URL (e.g. https://example.com)"),
    k: str = Query("", description="Focus keyword (optional)"),
):
    try:
        keyword = k.strip() or None
        log.info("[/report] url=%s keyword=%s", u, keyword)

        report_path, report = run_pipeline(url=u, keyword=keyword)

        log.info("[/report] OK (report_path=%s)", report_path)
        return JSONResponse(report, status_code=200)
    except Exception as e:
        log.exception("[/report] Unhandled error")
        return JSONResponse({"detail": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Crawler: start
# ---------------------------------------------------------------------------

@app.post("/crawl/", response_model=CrawlStatus)
async def start_crawl(
    background_tasks: BackgroundTasks,
    body: CrawlBody | None = None,
) -> CrawlStatus:
    import sys
    print(f"[*] /crawl/ endpoint called - body={body}", file=sys.stderr)

    status_obj = start_crawl_job(body)

    def _background_with_debug(*args, **kwargs) -> None:
        print(
            f"[*] [BG] run_crawl_job_sync called with args={args} kwargs={kwargs}",
            file=sys.stderr,
        )
        try:
            run_crawl_job_sync(*args, **kwargs)
        except Exception as ex:
            print(f"[!] [BG] ERROR: {ex}", file=sys.stderr)

    background_tasks.add_task(_background_with_debug, status_obj.job_id, body)
    print("[*] Background crawl job scheduled!", file=sys.stderr)

    return status_obj


# ---------------------------------------------------------------------------
# Crawler: status
# ---------------------------------------------------------------------------

@app.get("/crawl/status", response_model=CrawlStatus)
async def crawl_status() -> CrawlStatus:
    import sys
    print("[*] /crawl/status endpoint called", file=sys.stderr)

    status_obj = get_crawl_status()
    log.info(
        "[/crawl/status] job_id=%s status=%s",
        status_obj.job_id,
        status_obj.status,
    )
    return status_obj


# ---------------------------------------------------------------------------
# Simple crawl report
# ---------------------------------------------------------------------------

@app.get("/crawl/report/simple")
def simple_crawl_report():
    """
    Return simplified crawl results (from crawl_status.json).

    Each page entry includes:
      - url, final_url, status
      - index_status, meta_robots
      - internal_links: list of {raw, abs, status}
      - external_links: list of {raw, abs, status}
    """
    import sys
    print("[*] /crawl/report/simple endpoint called", file=sys.stderr)

    status_obj = get_crawl_status()
    log.info(
        "[/crawl/report/simple] job_id=%s status=%s",
        status_obj.job_id,
        status_obj.status,
    )

    if not status_obj.reports or len(status_obj.reports) == 0:
        return JSONResponse(
            content={
                "error": "No crawl report available yet",
                "job_id": status_obj.job_id,
                "status": status_obj.status,
            },
            status_code=404,
        )

    report = status_obj.reports[0]
    simple = []

    for page in report.pages:
        simple.append({
            "url": page.url,
            "final_url": page.final_url,
            "status": page.status,
            "index_status": page.index_status,
            "meta_robots": page.meta_robots,

            "internal_links": [
                {"raw": link.raw, "abs": link.abs, "status": link.status}
                for link in page.internal_links
            ],

            "external_links": [
                {"raw": link.raw, "abs": link.abs, "status": link.status}
                for link in page.external_links
            ],
        })

    return JSONResponse(content=simple, status_code=200)
