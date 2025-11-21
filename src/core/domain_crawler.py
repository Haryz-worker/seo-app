# src/core/domain_crawler.py
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from .fetcher import fetch
from .extractorV import html_bytes_to_json
from .utils import ensure_dir, save_json, load_json, is_http
from .util_crawler import (
    CrawlConfig,
    DomainInput,
    get_cache_dir,
    get_reports_dir,
    is_url_allowed_for_domain,
)


# -------------------------------------------------------------------
# Data Models
# -------------------------------------------------------------------

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

    # index-related fields
    meta_robots: Optional[str] = None
    index_status: Optional[str] = None  # "index" / "noindex" / None

    # date-related fields
    publish_date: Optional[str] = None
    modified_date: Optional[str] = None

    # link lists for this page (list of {raw, abs, status})
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


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

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
        extracted_path=str(extracted_path),
    )


def _extract_data_from_json(json_path: str, domain: DomainInput) -> Dict[str, Any]:
    """Parse extractorV output and return useful info for crawler."""
    try:
        data = load_json(json_path)
    except Exception:
        return {
            "internal": [],
            "external": [],
            "meta_robots": None,
            "index_status": None,
            "publish_date": None,
            "modified_date": None,
        }

    page = data.get("page", {}) or {}
    links = data.get("links", {}) or {}

    robots = page.get("meta_robots")
    index_status = None
    if isinstance(robots, str):
        low = robots.lower()
        index_status = "noindex" if "noindex" in low else "index"

    all_internal: List[Dict[str, Any]] = []
    all_external: List[Dict[str, Any]] = []

    for obj in links.get("internal", []):
        raw = obj.get("raw")
        absu = obj.get("abs")
        if absu and is_url_allowed_for_domain(absu, domain):
            all_internal.append({"raw": raw, "abs": absu})

    for obj in links.get("external", []):
        raw = obj.get("raw")
        absu = obj.get("abs")
        all_external.append({"raw": raw, "abs": absu})

    return {
        "internal": all_internal,
        "external": all_external,
        "meta_robots": robots,
        "index_status": index_status,
        "publish_date": page.get("publish_date"),
        "modified_date": page.get("modified_date"),
    }


def _probe_status(url: str, cfg: CrawlConfig, cache: Dict[str, Optional[int]]) -> Optional[int]:
    """Fetch the URL once and cache its HTTP status."""
    if url in cache:
        return cache[url]

    ok, meta, _ = fetch(
        url=url,
        ua=cfg.http.user_agent or "",
        engine=cfg.http.engine,
        timeout=cfg.http.timeout,
        http2=cfg.http.http2,
        retries=cfg.http.retries,
        proxy=cfg.http.proxy,
    )
    status = meta.get("status")
    cache[url] = status
    return status


# -------------------------------------------------------------------
# Crawler Core Logic
# -------------------------------------------------------------------

def crawl_domain(domain: DomainInput, cfg: CrawlConfig) -> DomainCrawlReport:
    started = time.time()
    pages: List[PageCrawlResult] = []

    max_pages = domain.max_pages or cfg.limits.max_pages_per_domain

    queue: List[str] = []
    seen: set[str] = set()

    # status cache for speed
    link_status_cache: Dict[str, Optional[int]] = {}

    # seed queue
    for u in domain.start_urls:
        if is_url_allowed_for_domain(u, domain):
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

        # cache page status
        link_status_cache[res.final_url] = res.status
        link_status_cache[res.url] = res.status

        if cfg.limits.delay_ms_between_requests > 0:
            time.sleep(cfg.limits.delay_ms_between_requests / 1000.0)

        if res.ok and res.extracted_path:
            info = _extract_data_from_json(res.extracted_path, domain)

            res.meta_robots = info["meta_robots"]
            res.index_status = info["index_status"]
            res.publish_date = info["publish_date"]
            res.modified_date = info["modified_date"]

            # internal links
            res.internal_links = []
            for obj in info["internal"]:
                absu = obj.get("abs")
                raw = obj.get("raw")
                status_int = _probe_status(absu, cfg, link_status_cache)
                res.internal_links.append({"raw": raw, "abs": absu, "status": status_int})

                # BFS enqueue
                if absu not in seen and absu not in queue and len(pages) < max_pages:
                    queue.append(absu)

            # external links
            res.external_links = []
            for obj in info["external"]:
                absu = obj.get("abs")
                raw = obj.get("raw")
                status_ext = _probe_status(absu, cfg, link_status_cache)
                res.external_links.append({"raw": raw, "abs": absu, "status": status_ext})

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

    reports_root = get_reports_dir()
    fname = f"{domain.slug}_report.json"
    path = reports_root / fname
    save_json(str(path), report.to_dict(), pretty=True, compact=False)

    return report
