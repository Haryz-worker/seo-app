# backend/app/service.py
from __future__ import annotations

import os
import re
import sys
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse

# --- make project root importable so "src.core" works when running from /backend ---
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# -----------------------------------------------------------------------------------

from src.core import fetcher, extractor, analyzer
from src.core.utils import ensure_dir, load_json, save_json


# Shared data directories (use top-level /data)
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
CONFIG_PATH = os.path.join(ROOT, "src", "config.json")

ensure_dir(CACHE_DIR)
ensure_dir(REPORTS_DIR)


def _slugify_path(path: str) -> str:
    if not path or path == "/":
        return "index"
    s = re.sub(r"[^\w\-]+", "-", path.strip("/").lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "index"


def _json_filename_from_url(final_url: str, save_dir: str) -> str:
    """
    Example: https://site.com/a/b -> <save_dir>/site.com_a-b.json
    """
    p = urlparse(final_url)
    host = p.netloc
    slug = _slugify_path(p.path)
    ensure_dir(save_dir)
    return os.path.join(save_dir, f"{host}_{slug}.json").replace("\\", "/")


def _load_thresholds() -> Dict:
    """
    Load config from src/config.json; fall back to sane defaults.
    """
    defaults: Dict = {
        "title_chars": [30, 70],
        "title_px": [285, 580],
        "meta_chars": [80, 160],
        "meta_px": [430, 920],
        "min_words": 800,
        "min_internal_links": 3,
        "http": {
            "engine": "httpx",
            "timeout": 25,
            "retries": 2,
            "http2": False,
            "proxy": None,
        },
    }
    try:
        cfg = load_json(CONFIG_PATH) or {}
    except Exception:
        return defaults

    # merge shallow keys
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    # merge http nested keys
    http = cfg.get("http", {}) or {}
    for k, v in defaults["http"].items():
        http.setdefault(k, v)
    cfg["http"] = http

    return cfg


def run_pipeline(url: str, keyword: Optional[str] = "") -> Tuple[str, Dict]:
    """
    fetch -> extract -> analyze
    Returns (report_path, report_dict)
    """
    cfg = _load_thresholds()
    http = cfg["http"]

    # 1) Fetch
    ok, meta, content = fetcher.fetch(
        url=str(url),  # enforce plain string for httpx
        engine=http.get("engine", "httpx"),
        timeout=int(http.get("timeout", 25)),
        http2=bool(http.get("http2", False)),
        retries=int(http.get("retries", 2)),
        proxy=http.get("proxy"),
        ua=fetcher.DESKTOP_UA,
    )
    if not ok or not content:
        raise RuntimeError(f"Fetch failed for URL: {url}")

    final_url = meta.get("final_url") or str(url)

    # 2) Extract -> JSON (keep extractor intact)
    json_out = _json_filename_from_url(final_url, CACHE_DIR)
    extractor.html_bytes_to_json(
        html_bytes=content,
        final_url=final_url,
        save_path=json_out,
        base_override=None,
        pretty=True,
        compact=False,
    )

    # 3) Analyze (single file)
    thresholds = {
        "title_chars": tuple(cfg["title_chars"]),
        "title_px": tuple(cfg["title_px"]),
        "meta_chars": tuple(cfg["meta_chars"]),
        "meta_px": tuple(cfg["meta_px"]),
        "min_words": int(cfg["min_words"]),
        "min_internal_links": int(cfg["min_internal_links"]),
    }
    focus_terms: List[str] = [keyword.strip()] if keyword else []

    report = analyzer.analyze_one(json_out, focus_terms, thresholds)

    # 4) Save report under /data/reports
    base = os.path.splitext(os.path.basename(json_out))[0]
    report_path = os.path.join(REPORTS_DIR, f"{base}_report.json").replace("\\", "/")
    save_json(report_path, report, pretty=True)

    return report_path, report
