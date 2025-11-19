# src/core/extractorV.py
import os
import itertools
import re
from typing import Optional, List, Dict
from urllib.parse import urlparse, urljoin
from datetime import datetime

from lxml import html as LH
from readability import Document

from .utils import (
    to_unicode,
    clean_space,
    is_http,
    norm_url,
    host_key,
    dedup,
    detect_lang_fallback,
    count_words,
    ensure_dir,
    save_json,
)

SOCIAL_HOSTS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "pinterest.com",
    "wa.me",
    "api.whatsapp.com",
    "t.me",
}

SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "#")

# NOTE: `/feed` removed here compared to the original extractor
SKIP_PATTERNS = ("/share", "/print", "/wp-json", "/amp", "/?replytocom=")


# ------------------------- base url helpers ------------------------- #
def guess_base_url_from_doc_or_url(doc: LH.HtmlElement, file_hint_url: Optional[str]) -> Optional[str]:
    base = doc.cssselect("base[href]")
    if base:
        href = base[0].get("href")
        if href and is_http(href):
            return href
    can = doc.cssselect("link[rel~='canonical'][href]")
    if can:
        href = can[0].get("href")
        if href and is_http(href):
            return href
    og = doc.cssselect("meta[property='og:url'][content]")
    if og:
        href = og[0].get("content")
        if href and is_http(href):
            return href
    if file_hint_url and is_http(file_hint_url):
        p = urlparse(file_hint_url)
        return f"{p.scheme}://{p.netloc}/"
    return None


# ------------------------- page meta ------------------------- #
def extract_page_meta(doc: LH.HtmlElement) -> Dict:
    title = None
    tnode = doc.find(".//title")
    if tnode is not None and tnode.text:
        title = clean_space(tnode.text)

    mdesc = None
    mrobots = None
    for m in doc.xpath("//meta[@name or @property]"):
        name = (m.get("name") or m.get("property") or "").lower()
        content = clean_space(m.get("content"))
        if not name:
            continue
        if name == "description":
            mdesc = content
        elif name == "robots":
            mrobots = content

    lang = clean_space(doc.get("lang") or doc.get("xml:lang"))
    canonical = None
    for l in doc.xpath("//link[contains(@rel,'canonical')][@href]"):
        canonical = l.get("href")
        break

    return {
        "title": title,
        "meta_description": mdesc,
        "meta_robots": mrobots,
        "canonical": canonical,
        "lang": lang or None,
    }


def first_h1(doc: LH.HtmlElement) -> Optional[str]:
    for h in doc.xpath("//h1"):
        txt = clean_space(" ".join(h.itertext()))
        if txt:
            return txt
    return None


# ------------------------- article text & headings ------------------------- #
def readability_fragment(html_text: str):
    doc = Document(html_text)
    try:
        summary_html = doc.summary(html_partial=True)
    except Exception:
        summary_html = None
    if not summary_html:
        return "", None
    try:
        frag = LH.fromstring(summary_html)
    except Exception:
        frag = None

    lines = []
    if frag is not None:
        for node in frag.iter():
            if node.tag in ("p", "li", "h2", "h3", "blockquote"):
                t = clean_space(" ".join(node.itertext()))
                if not t:
                    continue
                if t.lower().startswith(("table of contents", "toc")):
                    continue
                lines.append(t)

    cleaned, prev = [], None
    for ln in lines:
        if ln != prev:
            cleaned.append(ln)
        prev = ln

    return "\n".join(cleaned).strip(), frag


def extract_headings(frag: Optional[LH.HtmlElement]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"h2": [], "h3": []}
    if frag is None:
        return out
    for tag in ("h2", "h3"):
        for h in frag.xpath(f".//{tag}"):
            t = clean_space(" ".join(h.itertext()))
            if t:
                out[tag].append(t)

    def _clean(lst: List[str]) -> List[str]:
        res: List[str] = []
        seen = set()
        for t in lst:
            if not t:
                continue
            if t.lower().startswith("table of contents"):
                continue
            if t in seen:
                continue
            seen.add(t)
            res.append(t)
        return res

    out["h2"] = _clean(out["h2"])
    out["h3"] = _clean(out["h3"])
    return out


# ------------------------- links + anchors ------------------------- #
def _filter_link(u: str) -> bool:
    if not is_http(u):
        return False
    low = u.lower()
    if any(low.startswith(s) for s in SKIP_SCHEMES):
        return False
    if any(p in low for p in SKIP_PATTERNS):
        return False
    netloc = urlparse(u).netloc.lower().split(":")[0]
    if any(h in netloc for h in SOCIAL_HOSTS):
        return False
    return True


def _clean_anchor_text(txt: str) -> str:
    t = re.sub(r"\s+", " ", (txt or "").strip())
    return t[:160]


def _anchor_of(a_node) -> str:
    txt = _clean_anchor_text(" ".join(a_node.itertext()))
    if not txt:
        txt = _clean_anchor_text(a_node.get("title") or "")
    return txt


def links_from_fragment_with_anchors(frag: Optional[LH.HtmlElement], base_url: Optional[str]) -> List[Dict]:
    if frag is None:
        return []
    out: List[Dict] = []
    for a in frag.xpath(".//a[@href]"):
        href = a.get("href")
        if not href:
            continue
        absu = urljoin(base_url, href) if base_url and not is_http(href) else href
        if not _filter_link(absu):
            continue
        out.append({"url": norm_url(absu), "anchor": _anchor_of(a) or None})
    return out


def links_from_page_with_anchors(full_doc: LH.HtmlElement, base_url: Optional[str]) -> List[Dict]:
    all_as = full_doc.xpath("//a[@href]")
    skip_xpath = " | ".join(
        [
            "//header//a[@href]",
            "//footer//a[@href]",
            "//nav//a[@href]",
            "//aside//a[@href]",
            "//*[@class='site-header']//a[@href]",
            "//*[@class='site-footer']//a[@href]",
            "//*[@class='menu']//a[@href]",
            "//*[@class='sidebar']//a[@href]",
            "//*[@class='breadcrumbs']//a[@href]",
            "//*[@class='share']//a[@href]",
            "//*[@class='social']//a[@href]",
            "//*[@class='tags']//a[@href]",
        ]
    )
    bad_as = set(full_doc.xpath(skip_xpath)) if skip_xpath else set()

    out: List[Dict] = []
    for a in all_as:
        if a in bad_as:
            continue
        href = a.get("href")
        if not href:
            continue
        absu = urljoin(base_url, href) if base_url and not is_http(href) else href
        if not _filter_link(absu):
            continue
        out.append({"url": norm_url(absu), "anchor": _anchor_of(a) or None})
    return out


def _dedup_link_items(items: List[Dict]) -> List[Dict]:
    seen = set()
    out: List[Dict] = []
    for it in items:
        url = it.get("url")
        anc = (_clean_anchor_text(it.get("anchor") or "")).lower()
        key = (url, anc)
        if url and key not in seen:
            seen.add(key)
            out.append({"url": url, "anchor": it.get("anchor") or None})
    return out


def classify_link_items(items: List[Dict], base_url: Optional[str]) -> Dict:
    items = _dedup_link_items(items)
    if not base_url:
        internal: List[Dict] = []
        external = items
    else:
        b = host_key(base_url)
        internal = []
        external = []
        for it in items:
            try:
                if host_key(it["url"]) == b:
                    internal.append(it)
                else:
                    external.append(it)
            except Exception:
                external.append(it)

    total = len(internal) + len(external)
    return {
        "counts": {
            "total": total,
            "internal": len(internal),
            "external": len(external),
            "internal_ratio": round((len(internal) / total), 3) if total else 0.0,
            "external_ratio": round((len(external) / total), 3) if total else 0.0,
        },
        "internal_links": internal,
        "external_links": external,
    }


# ------------------------- main conversion ------------------------- #
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

    page = extract_page_meta(full_doc)
    h1 = first_h1(full_doc)
    base_url = base_override or guess_base_url_from_doc_or_url(full_doc, final_url)

    text, frag = readability_fragment(uhtml)
    headings = extract_headings(frag)

    article_images: List[Dict] = []
    if frag is not None:
        for im in frag.xpath(".//img[@src]"):
            src = im.get("src")
            if not src:
                continue
            absu = urljoin(base_url, src) if base_url and not is_http(src) else src
            if not is_http(absu):
                continue
            article_images.append(
                {
                    "src": norm_url(absu),
                    "alt": clean_space(im.get("alt")),
                    "width": im.get("width"),
                    "height": im.get("height"),
                }
            )

    meta_imgs: List[str] = []
    meta_imgs += full_doc.xpath("//meta[@property='og:image']/@content")
    meta_imgs += full_doc.xpath("//meta[@name='twitter:image']/@content")
    flat_imgs = dedup([u for u in meta_imgs if is_http(u)] + [i["src"] for i in article_images])

    link_items_article = links_from_fragment_with_anchors(frag, base_url)
    link_items_page = links_from_page_with_anchors(full_doc, base_url)
    links = classify_link_items(link_items_article + link_items_page, base_url)

    words = count_words(text)
    stats = {
        "characters": len(text),
        "words": words,
        "reading_time_minutes": max(1, (words + 199) // 200),
    }
    lang_final = detect_lang_fallback(text, page.get("lang"))

    data = {
        "input": {"source": "memory", "final_url": final_url, "base_url": base_url},
        "page": {**page, "lang": lang_final},
        "article": {
            "h1": h1,
            "headings": headings,
            "text": text,
            "stats": stats,
            "images": article_images,
        },
        "images": flat_imgs,
        "links": links,
        "meta": {
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app_version": "AB-2.1.0",
            "schema_version": "onpage-seo-app.article.v1",
            "extractors_used": ["readability", "lxml"],
        },
    }

    ensure_dir(os.path.dirname(save_path) or ".")
    save_json(save_path, data, pretty=pretty, compact=compact)
    return save_path
