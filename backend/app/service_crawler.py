# src/core/domain_crawler.py
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from .fetcher import fetch
from .extractor import html_bytes_to_json
from .utils import ensure_dir, save_json, load_json
from .util_crawler import (
    CrawlConfig,
    DomainInput,
    get_cache_dir,
    get_reports_dir,
    is_url_allowed_for_domain,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PageCrawlResult:
    url: str
    final_url: str
    status: Optional[int]
    duration_ms: int
    size_bytes: int
    encoding_guess: Optional[str]
    ok: bool
    error: Optional[str] = None
    extracted_path: Optional[str] = None

    # index / robots info (filled after reading extracted JSON)
    meta_robots: Optional[str] = None
    index_status: Optional[str] = None  # "index", "noindex", or None

    # links on this page (filled after reading extracted JSON)
    internal_links: List[Dict[str, Any]] = field(default_factory=list)
    external_links: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DomainCrawlReport:
    domain: str
    slug: str
    started_at: float
    finished_at: float
    config: Dict[str, Any]
    pages: List[PageCrawlResult] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "slug": self.slug,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "config": self.config,
            "pages": [asdict(p) for p in self.pages],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_save_path(cache_root: Path, slug: str, index: int, status: Optional[int]) -> Path:
    status_part = status if status is not None else "unknown"
    fname = f"{index:04d}_{status_part}.json"
    d = cache_root / slug
    ensure_dir(str(d))
    return d / fname


def crawl_single_url(
    url: str,
    domain: DomainInput,
    cfg: CrawlConfig,
    cache_root: Optional[Path] = None,
    index: int = 0,
    **legacy_kwargs: Any,
) -> PageCrawlResult:
    """
    Crawl a single URL and extract it to JSON.

    legacy_kwargs is used to support old callers that pass cache=...
    """
    # Support old keyword name: cache
    if cache_root is None and "cache" in legacy_kwargs:
        cache_root = legacy_kwargs.get("cache")

    if cache_root is None:
        cache_root = get_cache_dir()

    ok, meta, content = fetch(
        url=url,
        ua=cfg.http.user_agent or "",
        engine=cfg.http.engine,
        timeout=cfg.http.timeout,
        http2=cfg.http.http2,
        retries=cfg.http.retries,
        proxy=cfg.http.proxy,
    )

    status = meta.get("status")
    final_url = meta.get("final_url") or url
    duration_ms = int(meta.get("duration_ms") or 0)
    size_bytes = int(meta.get("size_bytes") or 0)
    enc = meta.get("encoding_guess")

    if not ok or not content:
        return PageCrawlResult(
            url=url,
            final_url=final_url,
            status=status,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            encoding_guess=enc,
            ok=False,
            error="fetch_failed",
            extracted_path=None,
        )

    save_path = _page_save_path(cache_root, domain.slug, index=index, status=status)
    extracted_path = html_bytes_to_json(
        html_bytes=content,
        final_url=final_url,
        save_path=str(save_path),
        base_override=None,
        pretty=True,
        compact=False,
    )

    return PageCrawlResult(
        url=url,
        final_url=final_url,
        status=status,
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        encoding_guess=enc,
        ok=True,
        error=None,
        extracted_path=str(extracted_path),
    )


def _extract_internal_links(json_path: str, domain: DomainInput) -> List[str]:
    """
    Old helper: read the extracted JSON and return a list of allowed internal URLs.
    Kept for backward compatibility. New code uses _extract_links_and_index_info.
    """
    try:
        data = load_json(json_path)
    except Exception:
        return []

    links = data.get("links", {})
    internal = links.get("internal_links") or []
    urls: List[str] = []
    for item in internal:
        if not isinstance(item, dict):
            continue
        u = item.get("url")
        if isinstance(u, str) and is_url_allowed_for_domain(u, domain):
            urls.append(u)
    # simple unique while preserving order
    seen = set()
    out: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_links_and_index_info(
    json_path: str,
    domain: DomainInput,
) -> Dict[str, Any]:
    """
    Read extracted JSON and return:

      - meta_robots (raw string)
      - index_status: "index" / "noindex" / None
      - internal_urls: list of URLs allowed for BFS
      - internal_links: list of {url, status} for reporting
      - external_links: list of {url, status} for reporting
    """
    try:
        data = load_json(json_path)
    except Exception:
        return {
            "meta_robots": None,
            "index_status": None,
            "internal_urls": [],
            "internal_links": [],
            "external_links": [],
        }

    page = data.get("page", {}) or {}
    links = data.get("links", {}) or {}

    meta_robots = page.get("meta_robots")
    index_status: Optional[str] = None

    if isinstance(meta_robots, str):
        low = meta_robots.lower()
        if "noindex" in low:
            index_status = "noindex"
        else:
            index_status = "index"

    internal_raw = links.get("internal_links") or []
    external_raw = links.get("external_links") or []

    # Internal URLs for BFS + link objects for reporting
    internal_urls: List[str] = []
    internal_links: List[Dict[str, Any]] = []
    for item in internal_raw:
        if not isinstance(item, dict):
            continue
        u = item.get("url")
        if not isinstance(u, str):
            continue
        if not is_url_allowed_for_domain(u, domain):
            continue
        internal_urls.append(u)
        internal_links.append({"url": u, "status": None})

    # External links (only for reporting; we do not filter by domain)
    external_links: List[Dict[str, Any]] = []
    for item in external_raw:
        if not isinstance(item, dict):
            continue
        u = item.get("url")
        if not isinstance(u, str):
            continue
        external_links.append({"url": u, "status": None})

    # Deduplicate internal_urls while preserving order
    seen = set()
    uniq_internal_urls: List[str] = []
    for u in internal_urls:
        if u not in seen:
            seen.add(u)
            uniq_internal_urls.append(u)

    return {
        "meta_robots": meta_robots,
        "index_status": index_status,
        "internal_urls": uniq_internal_urls,
        "internal_links": internal_links,
        "external_links": external_links,
    }


# ---------------------------------------------------------------------------
# Main crawl entrypoint
# ---------------------------------------------------------------------------

def crawl_domain(domain: DomainInput, cfg: CrawlConfig) -> DomainCrawlReport:
    """
    Crawl a single domain using the existing fetcher + extractor stack.

    Strategy:
    - Start from domain.start_urls (usually the root URL derived from `domain`).
    - For each crawled page, read its JSON and enqueue internal links (BFS).
    - For each page, also extract:
        * meta_robots
        * index_status (index / noindex)
        * internal_links (url + status)
        * external_links (url + status)
    - Continue until `max_pages` is reached for that domain.
    """
    started = time.time()
    pages: List[PageCrawlResult] = []

    max_pages = domain.max_pages or cfg.limits.max_pages_per_domain

    # BFS queue
    queue: List[str] = []
    seen: set[str] = set()

    # seed queue with start_urls
    for u in domain.start_urls:
        if is_url_allowed_for_domain(u, domain):
            if u not in seen:
                queue.append(u)

    cache_root = get_cache_dir()

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        index = len(pages)
        res = crawl_single_url(
            url=url,
            domain=domain,
            cfg=cfg,
            cache_root=cache_root,
            index=index,
        )
        pages.append(res)

        # Optional delay between requests
        if cfg.limits.delay_ms_between_requests > 0:
            time.sleep(cfg.limits.delay_ms_between_requests / 1000.0)

        # If extraction succeeded, read links + robots info and enqueue internal links
        if res.ok and res.extracted_path:
            info = _extract_links_and_index_info(res.extracted_path, domain)

            # Attach extra info to this page result
            res.meta_robots = info["meta_robots"]
            res.index_status = info["index_status"]
            res.internal_links = info["internal_links"]
            res.external_links = info["external_links"]

            # Enqueue internal URLs for BFS
            for nu in info["internal_urls"]:
                if nu not in seen and nu not in queue:
                    queue.append(nu)
                    if len(pages) + len(queue) >= max_pages:
                        # enough scheduled URLs, no need to keep adding
                        break

    finished = time.time()
    report = DomainCrawlReport(
        domain=domain.domain,
        slug=domain.slug,
        started_at=started,
        finished_at=finished,
        config={
            "http": {
                "engine": cfg.http.engine,
                "timeout": cfg.http.timeout,
                "retries": cfg.http.retries,
                "http2": cfg.http.http2,
                "proxy": cfg.http.proxy,
            },
            "limits": {
                "max_pages_per_domain": cfg.limits.max_pages_per_domain,
                "delay_ms_between_requests": cfg.limits.delay_ms_between_requests,
            },
            "max_pages_effective": max_pages,
        },
        pages=pages,
    )

    # save one JSON per domain under data/reports
    reports_root = get_reports_dir()
    fname = f"{domain.slug}_report.json"
    path = reports_root / fname
    save_json(str(path), report.to_dict(), pretty=True, compact=False)

    return report
