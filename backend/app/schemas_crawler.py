# backend/app/schemas_crawler.py

from __future__ import annotations

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class CrawlBody(BaseModel):
    """
    Body schema for /crawl endpoint.
    """
    domain: str = Field(
        ...,
        example="https://fitnovahealth.com",
        description="Domain to crawl (with or without https://)",
    )
    max_pages: int = Field(
        10,
        example=10,
        description="Maximum number of pages to crawl for this domain",
    )


class PageResult(BaseModel):
    """
    Result for a single crawled page.
    Matches what domain_crawler.PageCrawlResult writes to JSON.
    """
    url: str
    final_url: Optional[str] = None

    status: Optional[int] = None
    duration_ms: int = 0
    size_bytes: int = 0
    encoding_guess: Optional[str] = None

    ok: bool = True
    error: Optional[str] = None
    extracted_path: Optional[str] = None

    # Indexing info
    meta_robots: Optional[str] = None
    index_status: Optional[str] = None  # "index" / "noindex" / None

    # Links: each item is a dict like {"raw": ..., "abs": ..., "status": ...}
    internal_links: List[Dict[str, Any]] = Field(default_factory=list)
    external_links: List[Dict[str, Any]] = Field(default_factory=list)


class DomainReport(BaseModel):
    """
    Report for a single domain.
    Used inside CrawlStatus.reports.
    """
    domain: str
    slug: Optional[str] = None
    duration_ms: int
    report_path: str
    pages: List[PageResult] = Field(default_factory=list)


class CrawlStatus(BaseModel):
    """
    Global crawl status, stored in cache (crawl_status.json)
    and exposed via /crawl/status.
    """
    job_id: str
    status: str  # pending, running, done, failed, idle, error
    message: Optional[str] = None
    reports: Optional[List[DomainReport]] = None
