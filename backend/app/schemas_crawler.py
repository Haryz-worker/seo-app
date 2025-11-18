from pydantic import BaseModel, Field
from typing import Optional, List

class CrawlBody(BaseModel):
    """
    Body schema for /crawl endpoint. Only domain and max_pages (required).
    """
    domain: str = Field(
        ...,
        example="https://fitnovahealth.com",
        description="Domain to crawl (with or without https://)"
    )
    max_pages: int = Field(
        10,
        example=10,
        description="Maximum number of pages to crawl for this domain"
    )

class PageResult(BaseModel):
    url: str
    final_url: str
    status: Optional[int]
    duration_ms: int
    size_bytes: int
    encoding_guess: Optional[str]
    ok: bool
    error: Optional[str] = None
    extracted_path: Optional[str] = None

class DomainReport(BaseModel):
    domain: str
    slug: Optional[str] = None  # ممكن ما يستعملش فالباك, ولكن useful
    duration_ms: int
    report_path: str
    pages: List[PageResult]

class CrawlStatus(BaseModel):
    job_id: str
    status: str  # pending, running, done, failed, idle, error
    message: Optional[str] = None
    reports: Optional[List[DomainReport]] = None
