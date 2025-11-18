import os
import time
import json
from typing import List

from fastapi import FastAPI, Depends, HTTPException, status, Request, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import AnalyzeRequest, AnalyzeResponse, HealthResponse
from .service import run_pipeline
from .rate_limit import RateLimiter

from .schemas_crawler import CrawlBody, CrawlStatus
from .service_crawler import start_crawl_job, run_crawl_job_sync, get_crawl_status

API_TOKEN = os.getenv("API_TOKEN", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

app = FastAPI(title="OnPage SEO API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = RateLimiter(RATE_LIMIT_PER_MIN, window_seconds=60)

def auth_dep(request: Request):
    if not API_TOKEN:
        return
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = hdr.replace("Bearer ", "", 1).strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")

def rate_limit_dep(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not limiter.hit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(ok=True, ts=int(time.time()))

@app.post(
    "/analyze-page",
    response_model=AnalyzeResponse,
    dependencies=[Depends(auth_dep), Depends(rate_limit_dep)],
)
def analyze(req: AnalyzeRequest):
    try:
        url_str = str(req.url)
        keyword_str = (req.keyword or "").strip() or None

        report_path, report = run_pipeline(url=url_str, keyword=keyword_str)

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
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/report")
def get_report(
    u: str = Query(..., description="Target URL (e.g. https://example.com)"),
    k: str = Query("", description="Focus keyword (optional)")
):
    try:
        report_path, report = run_pipeline(url=u, keyword=k.strip() or None)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    return JSONResponse(report, status_code=200)

#    ENDPOINTS CRAWLER  

@app.post("/crawl/", response_model=CrawlStatus)
async def start_crawl(
    background_tasks: BackgroundTasks,
    body: CrawlBody | None = None,
):
    import sys
    print(f"[*] /crawl/ endpoint called - body={body}", file=sys.stderr)
    status = start_crawl_job(body)

    def _background_with_debug(*args, **kwargs):
        print(f"[*] [BG] run_crawl_job_sync called with args={args}, kwargs={kwargs}", file=sys.stderr)
        try:
            run_crawl_job_sync(*args, **kwargs)
        except Exception as ex:
            print(f"[!] [BG] ERROR: {ex}", file=sys.stderr)

    background_tasks.add_task(_background_with_debug, status.job_id, body)
    print("[*] Background crawl job scheduled!", file=sys.stderr)
    return status

@app.get("/crawl/status", response_model=CrawlStatus)
async def crawl_status() -> CrawlStatus:
    import sys
    print("[*] /crawl/status endpoint called", file=sys.stderr)
    return get_crawl_status()

# ------ NEW: Simple SaaS-Style Crawl Report (urls + status) --------

@app.get("/crawl/report/simple")
def simple_crawl_report():
    """
    Returns: list of {url, status} for latest crawled domain.
    """
    # هنا بدّل المسار إذا بغيت domain آخر أو dynamic (مثلاً تجبد آخر slug من status)
    REPORT_PATH = "data/reports/fitnovahealth_com_report.json"
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            crawl_data = json.load(f)
    except Exception as ex:
        return JSONResponse(
            content={"error": f"Report not found or invalid: {ex}"},
            status_code=404
        )

    simple = [
        {"url": page["url"], "status": page["status"]}
        for page in crawl_data.get("pages", [])
    ]
    return JSONResponse(content=simple)
