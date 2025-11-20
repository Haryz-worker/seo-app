# src/core/domain_crawler.py
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# ----------------------------------------------------------------------
# Tunable limits (Render-friendly)
# ----------------------------------------------------------------------

# How many internal links per page to probe status for
MAX_INTERNAL_LINKS_PER_PAGE = 30

# How many external links per page to probe status for
MAX_EXTERNAL_LINKS_PER_PAGE = 10

# Timeout used when probing link status (seconds)
LINK_PROBE_TIMEOUT_SEC = 2.0

# Max worker threads for link probes
LINK_PROBE_WORKERS = 5


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
    Fetch and extract a single URL.

    legacy_kwargs is used to support old callers that pass cache=...
    """
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

    # Limit how many links we keep per page (Render safety)
    internal_links_urls = internal_links_urls[:MAX_INTERNAL_LINKS_PER_PAGE]
    external_links_urls = external_links_urls[:MAX_EXTERNAL_LINKS_PER_PAGE]
    internal_urls_for_bfs = internal_urls_for_bfs[:MAX_INTERNAL_LINKS_PER_PAGE]

    return {
        "internal_urls_for_bfs": internal_urls_for_bfs,
        "internal_links_urls": internal_links_urls,
        "external_links_urls": external_links_urls,
        "meta_robots": robots,
        "index_status": index_status,
    }


def _probe_link_status_single(
    url: str,
    cfg: CrawlConfig,
    cache: Dict[str, Optional[int]],
) -> Optional[int]:
    """
    Return HTTP status for a single link URL, with cache.
    Uses a shorter timeout than main page fetch.
    """
    if url in cache:
        return cache[url]

    # Use a shorter timeout for link probes
    timeout = min(cfg.http.timeout, LINK_PROBE_TIMEOUT_SEC)

    ok, meta, _ = fetch(
        url=url,
        ua=cfg.http.user_agent or "",
        engine=cfg.http.engine,
        timeout=timeout,
        http2=cfg.http.http2,
        retries=cfg.http.retries,
        proxy=cfg.http.proxy,
    )
    status = meta.get("status")
    cache[url] = status
    return status


def _probe_link_status_bulk(
    urls: List[str],
    cfg: CrawlConfig,
    cache: Dict[str, Optional[int]],
) -> Dict[str, Optional[int]]:
    """
    Probe multiple link URLs in parallel using a small thread pool.
    Returns dict {url: status}.
    """
    results: Dict[str, Optional[int]] = {}

    # First fill from cache
    to_fetch: List[str] = []
    for u in urls:
        if u in cache:
            results[u] = cache[u]
        else:
            to_fetch.append(u)

    if not to_fetch:
        return results

    with ThreadPoolExecutor(max_workers=LINK_PROBE_WORKERS) as executor:
        future_to_url = {
            executor.submit(_probe_link_status_single, u, cfg, cache): u
            for u in to_fetch
        }
        for future in as_completed(future_to_url):
            u = future_to_url[future]
            try:
                status = future.result()
            except Exception:
                status = None
                cache[u] = None
            results[u] = status

    return results


def crawl_domain(domain: DomainInput, cfg: CrawlConfig) -> DomainCrawlReport:
    """
    Crawl a single domain using the existing fetcher + extractor stack.

    Strategy:
    - Start from domain.start_urls.
    - For each crawled page:
        * fetch + extract (via extractorV)
        * read meta_robots and infer index/noindex
        * read internal/external links from JSON
        * probe HTTP status for a limited number of internal/external links
        * enqueue internal links only (BFS)
    - Continue until max_pages is reached or queue is empty.
    """
    started = time.time()
    pages: List[PageCrawlResult] = []

    max_pages = domain.max_pages or cfg.limits.max_pages_per_domain

    queue: List[str] = []
    seen: set[str] = set()

    link_status_cache: Dict[str, Optional[int]] = {}

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
            cache=cache_root,
            index=index,
        )
        pages.append(res)

        if res.final_url:
            link_status_cache.setdefault(res.final_url, res.status)
        link_status_cache.setdefault(res.url, res.status)

        if cfg.limits.delay_ms_between_requests > 0:
            time.sleep(cfg.limits.delay_ms_between_requests / 1000.0)

        if res.ok and res.extracted_path:
            info = _extract_links_and_index_info(res.extracted_path, domain)

            res.meta_robots = info["meta_robots"]
            res.index_status = info["index_status"]

            internal_urls = info["internal_links_urls"]
            external_urls = info["external_links_urls"]

            internal_statuses = _probe_link_status_bulk(
                internal_urls, cfg, link_status_cache
            )
            external_statuses = _probe_link_status_bulk(
                external_urls, cfg, link_status_cache
            )

            res.internal_links = [
                {"url": u, "status": internal_statuses.get(u)}
                for u in internal_urls
            ]
            res.external_links = [
                {"url": u, "status": external_statuses.get(u)}
                for u in external_urls
            ]

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
