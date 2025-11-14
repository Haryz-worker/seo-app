import os
import time
from typing import List

from fastapi import FastAPI, Depends, HTTPException, status, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

from .schemas import AnalyzeRequest, AnalyzeResponse, HealthResponse
from .service import run_pipeline
from .rate_limit import RateLimiter

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

# --------- NEW: GET /report (for WordPress form or iframe) ----------
@app.get("/report")
def get_report(
    u: str = Query(..., description="Target URL (e.g. https://example.com)"),
    k: str = Query("", description="Focus keyword (optional)"),
    view: str = Query("json", description="View mode: json or html")
):
    try:
        report_path, report = run_pipeline(url=u, keyword=k.strip() or None)
    except Exception as e:
        if view == "html":
            return HTMLResponse(
                f"<pre style='color:#b91c1c;font-weight:bold'>Error: {str(e)}</pre>", status_code=500
            )
        return JSONResponse({"detail": str(e)}, status_code=500)

    if view == "html":
        html = f"""
        <div style="max-width:720px;margin:0 auto;font-family:sans-serif;">
            <h2 style="color:#2563eb;">SEO Analysis for<br><span style='font-size:17px;color:#333'>{u}</span></h2>
            <hr>
            <h3>SEO Score: <span style='color:#16a34a'>{report['overview']['score']}</span></h3>
            <h4>SEO Title: <span style='color:#0e7490'>{report['seo_title']['text']}</span></h4>
            <h4>Meta Description: <span style='color:#0e7490'>{report['meta_description']['text']}</span></h4>
            <h4>Word Count: <span style='color:#333'>{report['overview']['word_count']}</span></h4>
            <h4>Warnings:</h4>
            <ul>
            {''.join(f'<li style="color:#b91c1c">{w}</li>' for w in report['overview'].get('warnings', []))}
            </ul>
            <hr>
            <h4>Headings:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{str(report['headings'])}</pre>
            <h4>Keywords:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{str(report['keywords'])}</pre>
            <h4>Links:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{str(report['links'])}</pre>
            <h4>Readability:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{str(report['readability'])}</pre>
            <h4>Technical:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{str(report['technical'])}</pre>
            <h4>Source File:</h4>
            <pre style="white-space:pre-wrap;background:#f1f5f9;padding:7px 10px;border-radius:8px">{report['source_file']}</pre>
        </div>
        """
        return HTMLResponse(html, status_code=200)

    return JSONResponse(report, status_code=200)
