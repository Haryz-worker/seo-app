# src/core/util_crawler.py

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

from .utils import ensure_dir, host_key, norm_url, dedup, is_http


def get_project_root() -> Path:
    """
    Return the absolute project root (folder that contains `src` and `backend`).
    Example: /.../onpage-seo-app
    """
    here = Path(__file__).resolve()
    # .../src/core/util_crawler.py -> project root is parents[2]
    return here.parents[2]


def get_src_root() -> Path:
    return get_project_root() / "src"


def get_data_root() -> Path:
    return get_project_root() / "data"


def get_cache_dir() -> Path:
    d = get_data_root() / "cache"
    ensure_dir(str(d))
    return d


def get_reports_dir() -> Path:
    d = get_data_root() / "reports"
    ensure_dir(str(d))
    return d


def load_json_safe(path: Path, default: Any) -> Any:
    """
    Load JSON file if it exists, otherwise return default.
    Any parse error also returns default.
    """
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Crawl config
# ---------------------------------------------------------------------------

@dataclass
class CrawlHttpSettings:
    engine: str = "httpx"
    timeout: int = 25
    retries: int = 2
    http2: bool = True
    proxy: Optional[str] = None
    user_agent: Optional[str] = None


@dataclass
class CrawlLimits:
    max_pages_per_domain: int = 20
    delay_ms_between_requests: int = 0


@dataclass
class CrawlConfig:
    http: CrawlHttpSettings = field(default_factory=CrawlHttpSettings)
    limits: CrawlLimits = field(default_factory=CrawlLimits)


def load_crawl_config() -> CrawlConfig:
    """
    Load crawl config from src/config_crawl.json if it exists.

    Supports two shapes:

    1) Nested (recommended):
       {
         "http": {
           "engine": "httpx",
           "timeout": 15,
           "retries": 2,
           "http2": true,
           "proxy": null,
           "user_agent": "..."
         },
         "limits": {
           "max_pages_per_domain": 300,
           "delay_ms_between_requests": 0
         }
       }

    2) Flat (backward compatible):
       {
         "user_agent": "...",
         "timeout": 15,
         "retries": 2,
         "http2": true,
         "proxy": null,
         "max_pages_per_domain": 300,
         "delay_ms_between_requests": 0
       }
    """
    cfg_path = get_src_root() / "config_crawl.json"
    raw = load_json_safe(cfg_path, default={})

    # If nested keys exist, use them.
    if isinstance(raw, dict) and ("http" in raw or "limits" in raw):
        http_raw = raw.get("http", {})
        limits_raw = raw.get("limits", {})
    else:
        # Flat shape, treat whole object as http, and limits from same root
        http_raw = raw if isinstance(raw, dict) else {}
        limits_raw = raw.get("limits", {}) if isinstance(raw, dict) else {}

    http = CrawlHttpSettings(
        engine=http_raw.get("engine", "httpx"),
        timeout=int(http_raw.get("timeout", 25)),
        retries=int(http_raw.get("retries", 2)),
        http2=bool(http_raw.get("http2", True)),
        proxy=http_raw.get("proxy"),
        user_agent=http_raw.get("user_agent"),
    )

    limits = CrawlLimits(
        max_pages_per_domain=int(
            limits_raw.get(
                "max_pages_per_domain",
                raw.get("max_pages_per_domain", 20) if isinstance(raw, dict) else 20,
            )
        ),
        delay_ms_between_requests=int(
            limits_raw.get(
                "delay_ms_between_requests",
                raw.get("delay_ms_between_requests", 0) if isinstance(raw, dict) else 0,
            )
        ),
    )

    return CrawlConfig(http=http, limits=limits)


# ---------------------------------------------------------------------------
# Domain input
# ---------------------------------------------------------------------------

@dataclass
class DomainInput:
    """
    A normalized description of a domain to crawl.
    """
    domain: str
    slug: str
    start_urls: List[str]
    max_pages: Optional[int] = None
    allowed_paths: List[str] = field(default_factory=list)
    blocked_paths: List[str] = field(default_factory=list)

    @property
    def host_key(self) -> str:
        return host_key(self.domain)


def _slug_from_domain(dom: str) -> str:
    dom = dom.strip().lower()
    dom = dom.replace("https://", "").replace("http://", "").strip("/")
    return dom.replace(".", "_").replace("/", "_") or "domain"


def _normalize_domain_item(item: Dict[str, Any]) -> Optional[DomainInput]:
    """
    Normalize a raw JSON domain item into a DomainInput.

    Supported shapes:

    1) Simple:
       { "domain": "https://example.com", "max_pages": 10 }

    2) Domain only, no scheme:
       { "domain": "example.com", "max_pages": 10 }

    3) Advanced:
       {
         "domain": "https://example.com",
         "slug": "example",
         "start_urls": [...],
         "max_pages": 20,
         "allowed_paths": ["/blog/"],
         "blocked_paths": ["/wp-admin/"]
       }
    """
    domain = item.get("domain") or item.get("host") or ""
    domain = str(domain).strip()

    urls: List[str] = []

    for key in ("start_urls", "urls"):
        raw_urls = item.get(key)
        if isinstance(raw_urls, list):
            urls.extend([str(u) for u in raw_urls if isinstance(u, str)])

    if not urls and domain:
        if is_http(domain):
            root = domain
        else:
            root = f"https://{domain}"
        if not root.endswith("/"):
            root += "/"
        urls = [root]

    urls = [norm_url(u) for u in urls if u]
    urls = dedup(urls)

    if not domain and urls:
        domain = urls[0]
    if not domain:
        return None

    slug = item.get("slug")
    if not slug:
        slug = _slug_from_domain(domain)

    max_pages = item.get("max_pages")
    if isinstance(max_pages, str) and max_pages.isdigit():
        max_pages = int(max_pages)
    elif isinstance(max_pages, (int, float)):
        max_pages = int(max_pages)
    else:
        max_pages = None

    allowed_paths = item.get("allowed_paths") or item.get("include_paths") or []
    blocked_paths = item.get("blocked_paths") or item.get("exclude_paths") or []

    if not isinstance(allowed_paths, list):
        allowed_paths = []
    if not isinstance(blocked_paths, list):
        blocked_paths = []

    allowed_paths = [str(p) for p in allowed_paths if isinstance(p, str)]
    blocked_paths = [str(p) for p in blocked_paths if isinstance(p, str)]

    return DomainInput(
        domain=domain,
        slug=slug,
        start_urls=urls,
        max_pages=max_pages,
        allowed_paths=allowed_paths,
        blocked_paths=blocked_paths,
    )


def load_domain_inputs() -> List[DomainInput]:
    """
    Load domain inputs from src/Input_domain.json.

    Minimal example:

    [
      { "domain": "https://fitnovahealth.com", "max_pages": 300 },
      { "domain": "dailyboom.com", "max_pages": 50 }
    ]

    You can also use:
    { "domains": [ ... ] }
    """
    path = get_src_root() / "Input_domain.json"
    raw = load_json_safe(path, default=[])
    out: List[DomainInput] = []

    if isinstance(raw, dict):
        if "domains" in raw and isinstance(raw["domains"], list):
            items = raw["domains"]
        else:
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        dom = _normalize_domain_item(item)
        if dom and dom.start_urls:
            out.append(dom)

    return out


def is_url_allowed_for_domain(url: str, domain: DomainInput) -> bool:
    """
    Basic domain-level filtering for a URL.

    - Must match same host_key.
    - Must not contain any blocked_paths (if provided).
    - If allowed_paths is not empty, must contain at least one of them.
    """
    try:
        if host_key(url) != domain.host_key:
            return False
    except Exception:
        return False

    path = "/" + str(url).split("/", 3)[3] if "/" in str(url)[8:] else "/"

    for bp in domain.blocked_paths:
        if bp and bp in path:
            return False

    if domain.allowed_paths:
        for ap in domain.allowed_paths:
            if ap and ap in path:
                return True
        return False

    return True
