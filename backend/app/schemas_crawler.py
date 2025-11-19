# backend/app/schemas_crawler.py
from typing import Optional, List

from pydantic import BaseModel, Field


class CrawlBody(BaseModel):
    """
    Request body for /crawl endpoint.
    """
    domain: str = Field(
        ...,
        example="https://fitnovahealth.com",
        description="Domain to crawl (with or without scheme)",
    )
    max_pages: int = Field(
        10,
        example=50,
        description="Maximum number of pages to crawl for this domain",
    )


class LinkItem(BaseModel):
    """
    Represents a single link (internal or external) found on a page.
    """
    url: str
    status: Optional[int] = Field(
        None,
        description="HTTP status code for this link, if probed",
    )


class PageResult(BaseModel):
    """
    Result for a single crawled page.
    Mirrors src/core/domain_crawler.PageCrawlResult.
    """
    url: str
    final_url: str
    status: Optional[int]
    duration_ms: int
    size_bytes: int
    encoding_guess: Optional[str]
    ok: bool
    error: Optional[str] = None
    extracted_path: Optional[str] = None

    # index / robots info
    meta_robots: Optional[str] = Field(
        None,
        description="Raw content of <meta name='robots'> if present.",
    )
    index_status: Optional[str] = Field(
        None,
        description="Derived from meta_robots: 'index', 'noindex', or None.",
    )

    # links on this page
    internal_links: List[LinkItem] = Field(
        default_factory=list,
        description="Internal links found on this page (url + status).",
    )
    external_links: List[LinkItem] = Field(
        default_factory=list,
        description="External links found on this page (url + status).",
    )


class DomainReport(BaseModel):
    """
    High-level crawl report for a single domain.
    """
    domain: str
    slug: Optional[str] = Field(
        None,
        description="Slug used for file naming (e.g. fitnovahealth_com).",
    )
    duration_ms: int = Field(
        ...,
        description="Total crawl duration in milliseconds.",
    )
    report_path: str = Field(
        ...,
        description="Path to the JSON report on disk.",
    )
    pages: List[PageResult]


class CrawlStatus(BaseModel):
    """
    Status object returned by /crawl and /crawl/status endpoints.
    """
    job_id: str
    status: str = Field(
        ...,
        description="Job status: pending, running, done, failed, idle, or error.",
    )
    message: Optional[str] = Field(
        None,
        description="Optional human-readable message.",
    )
    reports: Optional[List[DomainReport]] = Field(
        None,
        description="Optional list of domain reports when the job is done.",
    )
