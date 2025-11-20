# src/core/domain_crawler.py
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from .fetcher import fetch
from .extractorV import html_bytes_to_json  # variant extractor for crawler
from .utils import ensure_dir, save_json, load_json, is_http
from .util_crawler import (
    CrawlConfig,
    DomainInput,
    get_cache_dir,
    get_reports_dir,
    is_url_allowed_for_domain,
)


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

    # link lists for this page (list of {url, status})
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
    # Backwards compatibility: accept "cache=" keyword
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


def _extract_links_and_index_info(
    json_path: str,
    domain: DomainInput,
) -> Dict[str, Any]:
    """
    Read extracted JSON and return:
      - internal_urls_for_bfs: list of internal URLs to enqueue in BFS
      - internal_links_urls: all internal link URLs on this page
      - external_links_urls: all external link URLs on this page
      - meta_robots and index_status
    """
    try:
        data = load_json(json_path)
    except Exception:
        return {
            "internal_urls_for_bfs": [],
            "internal_links_urls": [],
            "external_links_urls": [],
            "meta_robots": None,
            "index_status": None,
        }

    page = data.get("page", {}) or {}
    robots = page.get("meta_robots")
    index_status: Optional[str] = None
    if isinstance(robots, str) and robots.strip():
        low = robots.lower()
        if "noindex" in low:
            index_status = "noindex"
        else:
            index_status = "index"

    links = data.get("links", {}) or {}
    internal_items = links.get("internal_links") or []
    external_items = links.get("external_links") or []

    internal_urls_for_bfs: List[str] = []
    internal_links_urls: List[str] = []
    external_links_urls: List[str] = []

    # internal links (URLs only, filtered by domain rules)
    tmp_internal: List[str] = []
    for item in internal_items:
        if not isinstance(item, dict):
            continue
        u = item.get("url")
        if not isinstance(u, str):
            continue
        if not is_http(u):
            continue
        if is_url_allowed_for_domain(u, domain):
            tmp_internal.append(u)

    seen_int = set()
    for u in tmp_internal:
        if u not in seen_int:
            seen_int.add(u)
            internal_links_urls.append(u)
            internal_urls_for_bfs.append(u)

    # external links (URLs only, never crawled)
    tmp_external: List[str] = []
    for item in external_items:
        if not isinstance(item, dict):
            continue
        u = item.get("url")
        if not isinstance(u, str):
            continue
        if not is_http(u):
            continue
        tmp_external.append(u)

    seen_ext = set()
    for u in tmp_external:
        if u not in seen_ext:
            seen_ext.add(u)
            external_links_urls.append(u)

    return {
        "internal_urls_for_bfs": internal_urls_for_bfs,
        "internal_links_urls": internal_links_urls,
        "external_links_urls": external_links_urls,
        "meta_robots": robots,
        "index_status": index_status,
    }


def _probe_link_status(
    url: str,
    cfg: CrawlConfig,
    cache: Dict[str, Optional[int]],
) -> Optional[int]:
    """
    Return HTTP status for a link URL, with simple in-memory cache.
    Does not follow BFS, only a single fetch for status/meta.
    """
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


def crawl_domain(domain: DomainInput, cfg: CrawlConfig) -> DomainCrawlReport:
    """
    Crawl a single domain using the existing fetcher + extractor stack.

    Strategy:
    - Start from domain.start_urls.
    - For each crawled page:
        * fetch + extract (via extractorV)
        * read meta_robots and infer index/noindex
        * read internal/external links from JSON
        * for each internal/external link: probe HTTP status (with cache)
        * enqueue internal links only (BFS)
    - Continue until max_pages is reached or queue is empty.
    """
    started = time.time()
    pages: List[PageCrawlResult] = []

    max_pages = domain.max_pages or cfg.limits.max_pages_per_domain

    queue: List[str] = []
    seen: set[str] = set()

    # cache for link status (internal + external)
    link_status_cache: Dict[str, Optional[int]] = {}

    # seed queue
    for u in domain.start_urls:
        if is_url_allowed_for_domain(u, domain):
            if u not in seen and u not in queue:
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
            cache=cache_root,  # legacy keyword handled inside crawl_single_url
            index=index,
        )
        pages.append(res)

        # cache status for this page URL
        if res.final_url:
            link_status_cache.setdefault(res.final_url, res.status)
        link_status_cache.setdefault(res.url, res.status)

        if cfg.limits.delay_ms_between_requests > 0:
            time.sleep(cfg.limits.delay_ms_between_requests / 1000.0)

        if res.ok and res.extracted_path:
            info = _extract_links_and_index_info(res.extracted_path, domain)

            # index info for this page
            res.meta_robots = info["meta_robots"]
            res.index_status = info["index_status"]

            # internal links for this page with status
            res.internal_links = []
            for u_int in info["internal_links_urls"]:
                status_int = _probe_link_status(u_int, cfg, link_status_cache)
                res.internal_links.append({"url": u_int, "status": status_int})

            # external links for this page with status
            res.external_links = []
            for u_ext in info["external_links_urls"]:
                status_ext = _probe_link_status(u_ext, cfg, link_status_cache)
                res.external_links.append({"url": u_ext, "status": status_ext})

            # BFS queue: internal URLs only
            for nu in info["internal_urls_for_bfs"]:
                if nu not in seen and nu not in queue:
                    queue.append(nu)
                    if len(pages) + len(queue) >= max_pages:
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

    reports_root = get_reports_dir()
    fname = f"{domain.slug}_report.json"
    path = reports_root / fname
    save_json(str(path), report.to_dict(), pretty=True, compact=False)

    return report
