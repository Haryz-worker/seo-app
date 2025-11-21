# src/core/extractorV.py
from __future__ import annotations

import os
import re
import json
from urllib.parse import urljoin, urlparse

from datetime import datetime
from typing import List, Dict, Optional, Any

from lxml import html as LH
from readability import Document

from .utils import (
    to_unicode,
    clean_space,
    is_http,
    norm_url,
    dedup,
    ensure_dir,
    save_json,
    host_key,
)


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

def make_abs(base_url: Optional[str], raw: str) -> Optional[str]:
    """Convert raw link to absolute. Return None if cannot."""
    if not raw:
        return None

    raw = raw.strip()

    # skip javascript / mailto / tel
    low = raw.lower()
    if low.startswith("javascript:") or low.startswith("mailto:") or low.startswith("tel:"):
        return None

    # protocol-relative: //domain.com/file.js
    if raw.startswith("//") and base_url:
        base = urlparse(base_url)
        return f"{base.scheme}:{raw}"

    # absolute already
    if is_http(raw):
        return norm_url(raw)

    # relative
    if base_url:
        try:
            return norm_url(urljoin(base_url, raw))
        except Exception:
            return None

    return None


def extract_page_meta(doc: LH.HtmlElement) -> Dict:
    """Extract title, description, robots, canonical, language."""
    title = None
    tnode = doc.find(".//title")
    if tnode is not None and tnode.text:
        title = clean_space(tnode.text)

    meta_description = None
    meta_robots = None
    for m in doc.xpath("//meta[@name or @property]"):
        name = (m.get("name") or m.get("property") or "").lower()
        content = clean_space(m.get("content"))
        if not name:
            continue
        if name == "description":
            meta_description = content
        elif name == "robots":
            meta_robots = content

    canonical = None
    for l in doc.xpath("//link[@rel='canonical'][@href]"):
        canonical = clean_space(l.get("href"))
        break

    lang = clean_space(doc.get("lang") or doc.get("xml:lang"))

    return {
        "title": title,
        "meta_description": meta_description,
        "meta_robots": meta_robots,
        "canonical": canonical,
        "lang": lang or None,
    }


def extract_headings(doc: LH.HtmlElement) -> Dict[str, List[str]]:
    out = {"h1": [], "h2": [], "h3": []}

    for tag in ("h1", "h2", "h3"):
        nodes = doc.xpath(f"//{tag}")
        for h in nodes:
            t = clean_space(" ".join(h.itertext()))
            if t:
                out[tag].append(t)

    return out


def extract_text(html_text: str) -> str:
    doc = Document(html_text)
    try:
        summary_html = doc.summary(html_partial=True)
    except Exception:
        return ""

    if not summary_html:
        return ""

    try:
        frag = LH.fromstring(summary_html)
    except Exception:
        return ""

    lines = []
    for node in frag.iter():
        if node.tag in ("p", "li", "h2", "h3", "blockquote"):
            t = clean_space(" ".join(node.itertext()))
            if t:
                lines.append(t)

    cleaned, prev = [], None
    for ln in lines:
        if ln != prev:
            cleaned.append(ln)
        prev = ln

    return "\n".join(cleaned).strip()


def extract_images(doc: LH.HtmlElement, base_url: Optional[str]) -> List[Dict]:
    out = []
    for im in doc.xpath("//img[@src]"):
        raw = im.get("src")
        absu = make_abs(base_url, raw)
        out.append({
            "raw": raw,
            "abs": absu,
            "alt": clean_space(im.get("alt")),
            "width": im.get("width"),
            "height": im.get("height"),
        })
    return out


# -----------------------------------------------------------
# JSON-LD / dates extraction
# -----------------------------------------------------------

def extract_json_ld_dates(doc: LH.HtmlElement) -> Dict[str, Optional[str]]:
    published = None
    modified = None

    for node in doc.xpath("//script[@type='application/ld+json']"):
        try:
            data = json.loads(node.text or "")
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]

        for it in items:
            if not isinstance(it, dict):
                continue

            if not published:
                published = it.get("datePublished")
            if not modified:
                modified = it.get("dateModified")

            if published and modified:
                break

    return {
        "publish_date": published,
        "modified_date": modified,
    }


# -----------------------------------------------------------
# ALL LINKS extractor
# -----------------------------------------------------------

def extract_all_links(doc: LH.HtmlElement, base_url: Optional[str]) -> Dict[str, List[Dict[str, Optional[str]]]]:
    raw_links: List[str] = []

    # <a>
    for a in doc.xpath("//a[@href]"):
        raw_links.append(a.get("href"))

    # <link>
    for l in doc.xpath("//link[@href]"):
        raw_links.append(l.get("href"))

    # <img>
    for i in doc.xpath("//img[@src]"):
        raw_links.append(i.get("src"))

    # <script>
    for s in doc.xpath("//script[@src]"):
        raw_links.append(s.get("src"))

    # <iframe>
    for f in doc.xpath("//iframe[@src]"):
        raw_links.append(f.get("src"))

    # <source srcset>
    for srcset in doc.xpath("//source[@srcset]"):
        raw_links.append(srcset.get("srcset"))

    # OpenGraph
    raw_links += doc.xpath("//meta[@property='og:url']/@content")
    raw_links += doc.xpath("//meta[@property='og:image']/@content")

    # Meta refresh
    for m in doc.xpath("//meta[@http-equiv='refresh']"):
        content = m.get("content")
        if content and "url=" in content.lower():
            part = content.split("url=", 1)[1].strip()
            raw_links.append(part)

    # JSON-LD links
    for node in doc.xpath("//script[@type='application/ld+json']"):
        try:
            data = json.loads(node.text or "")
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]

        for it in items:
            if not isinstance(it, dict):
                continue
            for key in ("url", "@id", "contentUrl", "mainEntityOfPage", "image"):
                v = it.get(key)
                if isinstance(v, str):
                    raw_links.append(v)
                if isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            raw_links.append(x)

    # Build objects: raw + abs
    out_all = []
    for raw in raw_links:
        absu = make_abs(base_url, raw)
        out_all.append({"raw": raw, "abs": absu})

    # Classification (abs-only)
    internal = []
    external = []
    host = host_key(base_url) if base_url else None

    for obj in out_all:
        absu = obj.get("abs")
        if not absu or not host:
            external.append(obj)
            continue
        if host_key(absu) == host:
            internal.append(obj)
        else:
            external.append(obj)

    return {
        "all": out_all,
        "internal": internal,
        "external": external,
    }


# -----------------------------------------------------------
# Main function
# -----------------------------------------------------------

def html_bytes_to_json(
    html_bytes: bytes,
    final_url: str,
    save_path: str,
    base_override: Optional[str] = None,
    pretty: bool = True,
    compact: bool = False,
) -> str:
    uhtml = to_unicode(html_bytes)
    full_doc = LH.fromstring(uhtml)

    # Base URL
    base_url = base_override or final_url

    # Metadata
    meta = extract_page_meta(full_doc)
    headings = extract_headings(full_doc)
    text = extract_text(uhtml)
    images = extract_images(full_doc, base_url)
    dates = extract_json_ld_dates(full_doc)

    # ALL LINKS
    links = extract_all_links(full_doc, base_url)

    # Final JSON
    data = {
        "input": {
            "final_url": final_url,
            "base_url": base_url,
        },
        "page": {
            **meta,
            "headings": headings,
            "publish_date": dates["publish_date"],
            "modified_date": dates["modified_date"],
        },
        "article": {
            "text": text,
            "images": images,
        },
        "links": links,
        "meta": {
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "extractor": "extractorV",
        },
    }

    ensure_dir(os.path.dirname(save_path) or ".")
    save_json(save_path, data, pretty=pretty, compact=compact)
    return save_path
