# backend/app/main.py
import os
import time
from typing import List

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware

from .schemas import AnalyzeRequest, AnalyzeResponse, HealthResponse
from .service import run_pipeline
from .rate_limit import RateLimiter

API_TOKEN = os.getenv("API_TOKEN", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

app = FastAPI(title="OnPage SEO API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiter (in-memory)
limiter = RateLimiter(RATE_LIMIT_PER_MIN, window_seconds=60)


def auth_dep(request: Request):
    """Bearer token check (disabled if API_TOKEN is empty)."""
    if not API_TOKEN:
        return
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = hdr.replace("Bearer ", "", 1).strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")


def rate_limit_dep(request: Request):
    """Basic per-IP rate limiting."""
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
    """
    Run: fetch → extract → analyze
    """
    try:
        # مهم: حوّل HttpUrl إلى string قبل تمريـره
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
        # لو auth/rate-limit رما Exception خليه يمر كما هو
        raise
    except Exception as e:
        # رجّع خطأ واضح للـ client
        raise HTTPException(status_code=500, detail=str(e))
