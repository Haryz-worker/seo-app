# backend/app/service_crawler.py

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any

from src.core.domain_crawler import crawl_domain
from src.core.util_crawler import (
    CrawlConfig,
    DomainInput,
    get_cache_dir,
    get_reports_dir,
    load_crawl_config,
    load_domain_inputs,
)

from .schemas_crawler import (
    CrawlBody,
    CrawlStatus,
    DomainReport,
    PageResult,
)

STATUS_FILENAME = "crawl_status.json"


# ---------------------------------------------------------------------------
# Status file helpers
# ---------------------------------------------------------------------------

def _status_path() -> Path:
    return get_cache_dir() / STATUS_FILENAME


def _read_status_raw() -> Dict[str, Any]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_status_raw(data: Dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers to build DomainInput from request body
# ---------------------------------------------------------------------------

def _slug_from_domain(dom: str) -> str:
    dom = dom.strip().lower()
    dom = dom.replace("https://", "").replace("http://", "").strip("/")
    return dom.replace(".", "_").replace("/", "_") or "domain"


def _make_root_url(domain: str) -> str:
    """
    Normalize domain → proper full root URL.
    """
    d = domain.strip()
    if not d.startswith("http"):
        d = "https://" + d
    if not d.endswith("/"):
        d += "/"
    return d


def _domain_from_body(body: CrawlBody) -> DomainInput:
    """
    Convert simple CrawlBody → DomainInput used by crawler engine.
    """
    root_url = _make_root_url(body.domain)
    slug = _slug_from_domain(body.domain)

    return DomainInput(
        domain=body.domain,
        slug=slug,
        start_urls=[root_url],
        max_pages=body.max_pages,
        allowed_paths=[],
        blocked_paths=[],
    )


def _load_domains_from_request(body: Optional[CrawlBody]) -> List[DomainInput]:
    """
    If body is provided, crawl that domain only.
    Otherwise fallback to Input_domain.json.
    """
    if body is not None:
        return [_domain_from_body(body)]
    return load_domain_inputs()


# ---------------------------------------------------------------------------
# Public API used by FastAPI endpoints
# ---------------------------------------------------------------------------

def start_crawl_job(body: Optional[CrawlBody]) -> CrawlStatus:
    """
    Create a new crawl job, write initial status file,
    and return the initial CrawlStatus object.
    """
    job_id = str(uuid.uuid4())
    status = CrawlStatus(
        job_id=job_id,
        status="pending",
        message="scheduled",
        reports=None,
    )
    _write_status_raw(status.dict())
    return status


def run_crawl_job_sync(job_id: str, body: Optional[CrawlBody]) -> None:
    """
    Synchronous worker entry point. This is called in a background task
    from FastAPI, so it can block safely.

    It:
      - loads crawl config
      - resolves domains to crawl
      - runs crawl_domain() per domain
      - writes a simplified report into crawl_status.json
    """
    cfg: CrawlConfig = load_crawl_config()
    domains: List[DomainInput] = _load_domains_from_request(body)

    _write_status_raw(
        {
            "job_id": job_id,
            "status": "running",
            "message": f"Running crawl for {len(domains)} domain(s)",
            "reports": [],
        }
    )

    reports_out: List[Dict[str, Any]] = []

    try:
        for domain in domains:
            report = crawl_domain(domain, cfg)
            report_path = str(get_reports_dir() / f"{domain.slug}_report.json")

            pages_models: List[Dict[str, Any]] = []
            for p in report.pages:
                pages_models.append(
                    {
                        "url": p.url,
                        "final_url": p.final_url,
                        "status": p.status,
                        "duration_ms": p.duration_ms,
                        "size_bytes": p.size_bytes,
                        "encoding_guess": p.encoding_guess,
                        "ok": p.ok,
                        "error": p.error,
                        "extracted_path": p.extracted_path,
                        "meta_robots": getattr(p, "meta_robots", None),
                        "index_status": getattr(p, "index_status", None),
                        # internal/external links are already list[dict]
                        "internal_links": getattr(p, "internal_links", []),
                        "external_links": getattr(p, "external_links", []),
                    }
                )

            reports_out.append(
                {
                    "domain": report.domain,
                    "slug": report.slug,
                    "duration_ms": report.duration_ms,
                    "report_path": report_path,
                    "pages": pages_models,
                }
            )

        _write_status_raw(
            {
                "job_id": job_id,
                "status": "done",
                "message": f"Completed crawl for {len(domains)} domain(s)",
                "reports": reports_out,
            }
        )

    except Exception as e:
        _write_status_raw(
            {
                "job_id": job_id,
                "status": "failed",
                "message": f"Error: {e}",
                "reports": reports_out,
            }
        )


def get_crawl_status() -> CrawlStatus:
    """
    Load crawl_status.json, map it into CrawlStatus with nested
    DomainReport + PageResult models.
    """
    raw = _read_status_raw()
    if not raw:
        return CrawlStatus(
            job_id="none",
            status="idle",
            message="No crawl job has been started yet",
            reports=None,
        )

    try:
        reports_raw = raw.get("reports") or []
        reports_models: List[DomainReport] = []

        for r in reports_raw:
            pages_models = [PageResult(**p) for p in r.get("pages", [])]
            reports_models.append(
                DomainReport(
                    domain=r.get("domain", ""),
                    slug=r.get("slug"),
                    duration_ms=int(r.get("duration_ms") or 0),
                    report_path=r.get("report_path", ""),
                    pages=pages_models,
                )
            )

        return CrawlStatus(
            job_id=str(raw.get("job_id", "")),
            status=str(raw.get("status", "")),
            message=raw.get("message"),
            reports=reports_models or None,
        )

    except Exception:
        return CrawlStatus(
            job_id="invalid",
            status="error",
            message="Failed to parse crawl status file",
            reports=None,
        )
