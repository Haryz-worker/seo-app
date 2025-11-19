# backend/app/service_crawler.py
from __future__ import annotations

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


# ------------------------------------------------------------
# JSON STATUS FILE HELPERS
# ------------------------------------------------------------

def _status_path() -> Path:
    return get_cache_dir() / STATUS_FILENAME


def _read_status_raw() -> Dict[str, Any]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        import json
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_status_raw(data: Dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def _slug_from_domain(dom: str) -> str:
    dom = dom.strip().lower()
    dom = dom.replace("https://", "").replace("http://", "").strip("/")
    return dom.replace(".", "_").replace("/", "_") or "domain"


def _make_root_url(domain: str) -> str:
    """Normalize domain to a proper full root URL."""
    d = domain.strip()
    if not d.startswith("http"):
        d = "https://" + d
    if not d.endswith("/"):
        d += "/"
    return d


def _domain_from_body(body: CrawlBody) -> DomainInput:
    """Convert simple CrawlBody to DomainInput used by the crawler engine."""
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
    if body is not None:
        return [_domain_from_body(body)]
    return load_domain_inputs()


# ------------------------------------------------------------
# PUBLIC FUNCTIONS
# ------------------------------------------------------------

def start_crawl_job(body: Optional[CrawlBody]) -> CrawlStatus:
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
    cfg: CrawlConfig = load_crawl_config()
    domains: List[DomainInput] = _load_domains_from_request(body)

    # write: running status
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
        # loop on domains
        for domain in domains:
            report = crawl_domain(domain, cfg)
            report_path = str(get_reports_dir() / f"{domain.slug}_report.json")

            pages_models: List[Dict[str, Any]] = []

            for p in report.pages:
                # base fields
                page_dict: Dict[str, Any] = {
                    "url": p.url,
                    "final_url": p.final_url,
                    "status": p.status,
                    "duration_ms": p.duration_ms,
                    "size_bytes": p.size_bytes,
                    "encoding_guess": p.encoding_guess,
                    "ok": p.ok,
                    "error": p.error,
                    "extracted_path": p.extracted_path,
                }

                # index and robots info (optional on older reports)
                page_dict["meta_robots"] = getattr(p, "meta_robots", None)
                page_dict["index_status"] = getattr(p, "index_status", None)

                # links: list of {url, status}
                internal_links = getattr(p, "internal_links", []) or []
                external_links = getattr(p, "external_links", []) or []

                # ensure they are serializable lists of dicts
                norm_internal: List[Dict[str, Any]] = []
                for it in internal_links:
                    if isinstance(it, dict):
                        norm_internal.append(
                            {
                                "url": it.get("url"),
                                "status": it.get("status"),
                            }
                        )
                    else:
                        norm_internal.append({"url": str(it), "status": None})

                norm_external: List[Dict[str, Any]] = []
                for it in external_links:
                    if isinstance(it, dict):
                        norm_external.append(
                            {
                                "url": it.get("url"),
                                "status": it.get("status"),
                            }
                        )
                    else:
                        norm_external.append({"url": str(it), "status": None})

                page_dict["internal_links"] = norm_internal
                page_dict["external_links"] = norm_external

                pages_models.append(page_dict)

            reports_out.append(
                {
                    "domain": report.domain,
                    "slug": domain.slug,
                    "duration_ms": report.duration_ms,
                    "report_path": report_path,
                    "pages": pages_models,
                }
            )

        # write: done
        _write_status_raw(
            {
                "job_id": job_id,
                "status": "done",
                "message": f"Completed crawl for {len(domains)} domain(s)",
                "reports": reports_out,
            }
        )

    except Exception as e:
        # write: failure
        _write_status_raw(
            {
                "job_id": job_id,
                "status": "failed",
                "message": f"Error: {e}",
                "reports": reports_out,
            }
        )


def get_crawl_status() -> CrawlStatus:
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
                    slug=r.get("slug", ""),
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
        )        return {}
    try:
        import json
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_status_raw(data: Dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def _slug_from_domain(dom: str) -> str:
    dom = dom.strip().lower()
    dom = dom.replace("https://", "").replace("http://", "").strip("/")
    return dom.replace(".", "_").replace("/", "_") or "domain"


def _make_root_url(domain: str) -> str:
    """Normalize domain → proper full root URL."""
    d = domain.strip()
    if not d.startswith("http"):
        d = "https://" + d
    if not d.endswith("/"):
        d += "/"
    return d


def _domain_from_body(body: CrawlBody) -> DomainInput:
    """Convert simple CrawlBody → DomainInput used by crawler engine."""
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
    if body is not None:
        return [_domain_from_body(body)]
    return load_domain_inputs()


# ------------------------------------------------------------
# PUBLIC FUNCTIONS
# ------------------------------------------------------------

def start_crawl_job(body: Optional[CrawlBody]) -> CrawlStatus:
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
    cfg: CrawlConfig = load_crawl_config()
    domains: List[DomainInput] = _load_domains_from_request(body)

    # WRITE: running status
    _write_status_raw({
        "job_id": job_id,
        "status": "running",
        "message": f"Running crawl for {len(domains)} domain(s)",
        "reports": [],
    })

    reports_out: List[Dict[str, Any]] = []

    try:
        # LOOP ON DOMAINS
        for domain in domains:
            report = crawl_domain(domain, cfg)
            report_path = str(get_reports_dir() / f"{domain.slug}_report.json")

            # Build list of page results
            pages_models = []
            for p in report.pages:
                pages_models.append({
                    "url": p.url,
                    "final_url": p.final_url,
                    "status": p.status,
                    "duration_ms": p.duration_ms,
                    "size_bytes": p.size_bytes,
                    "encoding_guess": p.encoding_guess,
                    "ok": p.ok,
                    "error": p.error,
                    "extracted_path": p.extracted_path,
                })

            reports_out.append({
                "domain": report.domain,
                "slug": domain.slug,
                "duration_ms": report.duration_ms,
                "report_path": report_path,
                "pages": pages_models,
            })

        # WRITE: done
        _write_status_raw({
            "job_id": job_id,
            "status": "done",
            "message": f"Completed crawl for {len(domains)} domain(s)",
            "reports": reports_out,
        })

    except Exception as e:
        # WRITE: failure
        _write_status_raw({
            "job_id": job_id,
            "status": "failed",
            "message": f"Error: {e}",
            "reports": reports_out,
        })


def get_crawl_status() -> CrawlStatus:
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
            reports_models.append(DomainReport(
                domain=r.get("domain", ""),
                slug=r.get("slug", ""),
                duration_ms=int(r.get("duration_ms") or 0),
                report_path=r.get("report_path", ""),
                pages=pages_models,
            ))

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
