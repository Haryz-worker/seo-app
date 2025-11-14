# analyzer.py — single-file on-page analyzer, enriched with keyword checks, and compatible with new links schema
import os, re
from datetime import datetime
from typing import List, Dict, Any

from .utils import (
    load_json, save_json, px_estimate, count_words, ensure_dir,
    load_input_urls, clean_space
)

# ---------------- helpers ---------------- #
def sentences(text: str):
    parts = re.split(r'(?<=[\.\?\!])\s+', (text or "").strip())
    return [p.strip() for p in parts if p.strip()]

def flesch_reading_ease(text: str) -> float:
    text = text or ""
    sents = sentences(text)
    words = re.findall(r"\b[\w'-]+\b", text)
    if not sents or not words:
        return 0.0

    def est_syll(w):
        w = w.lower()
        vowels = re.findall(r"[aeiouy]+", w)
        syl = max(1, len(vowels))
        if w.endswith("e") and syl > 1:
            syl -= 1
        return syl

    syll = sum(est_syll(w) for w in words)
    w_count = len(words)
    s_count = max(1, len(sents))
    return round(206.835 - 1.015 * (w_count / s_count) - 84.6 * (syll / w_count), 2)

def jaccard(a: str, b: str) -> float:
    A = set(re.findall(r"\w+", (a or "").lower()))
    B = set(re.findall(r"\w+", (b or "").lower()))
    if not A or not B:
        return 0.0
    return round(len(A & B) / len(A | B), 3)

def status_from_range(value: int, lo: int, hi: int) -> str:
    if value < lo:
        return "short"
    if value > hi:
        return "long"
    return "ok"

def uniq(seq):
    seen = set()
    out = []
    for x in (seq or []):
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out

# ---------------- keyword stats ---------------- #
def keyword_stats(text: str, heads: Dict, page: Dict, h1_text: str, url: str, term: str) -> Dict:
    t = (term or "").strip()
    if not t:
        return {"term": "", "occurrences": 0}

    low = text.lower()
    occ = len(re.findall(rf"\b{re.escape(t.lower())}\b", low))
    words = count_words(text)
    density = round((occ / max(1, words)) * 100, 3)

    title = (page.get("title") or "")
    desc = (page.get("meta_description") or "")
    h2s = heads.get("h2") or []
    h3s = heads.get("h3") or []

    def contain(s: str) -> bool:
        return bool(re.search(rf"\b{re.escape(t.lower())}\b", (s or "").lower()))

    return {
        "term": t,
        "occurrences": occ,
        "density_pct": density,
        "in_title": contain(title),
        "in_h1": contain(h1_text or ""),
        "in_description": contain(desc),
        "in_h2_count": sum(1 for h in h2s if contain(h)),
        "in_h3_count": sum(1 for h in h3s if contain(h)),
        "in_url": contain(url or "")
    }

# ---------------- links normalization (new & legacy) ---------------- #
def _normalize_links(links_obj: Dict) -> Dict:
    if not isinstance(links_obj, dict):
        return {
            "counts": {"total": 0, "internal": 0, "external": 0, "internal_ratio": 0.0, "external_ratio": 0.0},
            "internal_links": [],
            "external_links": []
        }

    if "internal_links" in links_obj or "external_links" in links_obj:
        internal_items = links_obj.get("internal_links") or []
        external_items = links_obj.get("external_links") or []

        internal_urls = [
            {"url": it.get("url"), "anchor": it.get("anchor")}
            for it in internal_items if it and it.get("url")
        ]
        external_urls = [
            {"url": it.get("url"), "anchor": it.get("anchor")}
            for it in external_items if it and it.get("url")
        ]

        if "counts" in links_obj and isinstance(links_obj["counts"], dict):
            counts = links_obj["counts"]
            total = counts.get("total") or (len(internal_urls) + len(external_urls))
            internal = counts.get("internal") or len(internal_urls)
            external = counts.get("external") or len(external_urls)
            internal_ratio = round((internal / total), 3) if total else 0.0
            external_ratio = round((external / total), 3) if total else 0.0
        else:
            total = len(internal_urls) + len(external_urls)
            internal = len(internal_urls)
            external = len(external_urls)
            internal_ratio = round((internal / total), 3) if total else 0.0
            external_ratio = round((external / total), 3) if total else 0.0

        return {
            "counts": {
                "total": total,
                "internal": internal,
                "external": external,
                "internal_ratio": internal_ratio,
                "external_ratio": external_ratio
            },
            "internal_links": internal_urls,
            "external_links": external_urls
        }

    internal_list = links_obj.get("internal") or []
    external_list = links_obj.get("external") or []
    all_list = links_obj.get("all") or (internal_list + external_list)

    total = len(all_list) if all_list else (len(internal_list) + len(external_list))
    internal = len(internal_list)
    external = len(external_list)
    internal_ratio = round((internal / total), 3) if total else 0.0
    external_ratio = round((external / total), 3) if total else 0.0

    return {
        "counts": {
            "total": total,
            "internal": internal,
            "external": external,
            "internal_ratio": internal_ratio,
            "external_ratio": external_ratio
        },
        "internal_links": [{"url": u, "anchor": None} for u in internal_list],
        "external_links": [{"url": u, "anchor": None} for u in external_list]
    }

# ---------------- main analysis ---------------- #
def analyze_one(json_path: str, focus_terms: List[str], thresholds: Dict) -> Dict:
    data = load_json(json_path)
    page = data.get("page", {}) or {}
    article = data.get("article", {}) or {}
    links_obj = data.get("links", {}) or {}

    title = (page.get("title") or "").strip()
    meta = (page.get("meta_description") or "").strip()
    h1 = (article.get("h1") or "").strip()
    text = (article.get("text") or "").strip()
    heads = article.get("headings", {}) or {}
    h2s = heads.get("h2", []) or []
    h3s = heads.get("h3", []) or []
    final_url = (data.get("input", {}) or {}).get("final_url") or ""

    words = re.findall(r"\b[\w'-]+\b", text)
    word_count = len(words)

    T = {
        "title_chars": thresholds.get("title_chars", (30, 70)),
        "title_px": thresholds.get("title_px", (285, 580)),
        "meta_chars": thresholds.get("meta_chars", (80, 160)),
        "meta_px": thresholds.get("meta_px", (430, 920)),
        "min_words": thresholds.get("min_words", 800),
        "min_internal_links": thresholds.get("min_internal_links", 3),
    }

    title_px = px_estimate(title, is_title=True)
    meta_px = px_estimate(meta, is_title=False)

    title_status_chars = status_from_range(len(title), *T["title_chars"])
    title_status_px = status_from_range(title_px, *T["title_px"])
    meta_status_chars = status_from_range(len(meta), *T["meta_chars"])
    meta_status_px = status_from_range(meta_px, *T["meta_px"])

    total_headings = len(h2s) + len(h3s)
    heading_density_per_1k = round((total_headings / max(1, word_count)) * 1000, 2)
    words_per_h2 = round(word_count / max(1, len(h2s)), 1) if h2s else float(word_count)

    h1_title_sim = jaccard(h1, title)

    flesch = flesch_reading_ease(text)
    sent_list = sentences(text)
    avg_sentence_len = round(word_count / max(1, len(sent_list)), 2)

    warnings = []
    links_norm = _normalize_links(links_obj)
    counts = links_norm["counts"]
    internal_links = links_norm["internal_links"]
    external_links = links_norm["external_links"]

    total_links = counts.get("total", 0)
    if total_links == 0:
        warnings.append("no links detected in article body")
    if len(internal_links) < T["min_internal_links"]:
        warnings.append(f"few internal links: {len(internal_links)} (< {T['min_internal_links']})")

    score = 0

    def add(ok, w):
        nonlocal score
        if ok:
            score += w

    add(T["title_chars"][0] <= len(title) <= T["title_chars"][1], 10)
    add(T["title_px"][0] <= title_px <= T["title_px"][1], 6)
    add(T["meta_chars"][0] <= len(meta) <= T["meta_chars"][1], 8)
    add(T["meta_px"][0] <= meta_px <= T["meta_px"][1], 4)
    add(word_count >= T["min_words"], 12)
    add(len(internal_links) >= T["min_internal_links"], 10)
    add(bool(page.get("canonical")), 5)
    add(bool(page.get("meta_robots")), 3)
    add(bool(page.get("lang")), 3)
    add(flesch >= 50, 6)
    add(avg_sentence_len <= 22, 5)
    add(h1_title_sim >= 0.3, 4)
    add(len(h2s) >= 3, 6)
    add(len(h3s) >= 2, 4)

    score = round(min(100, score), 2)

    fk_stats = [
        keyword_stats(text, heads, page, h1, final_url, k)
        for k in (focus_terms or [])
    ]

    report = {
        "source_file": os.path.basename(json_path),
        "generated_at": datetime.utcnow().isoformat() + "Z",

        "overview": {
            "score": score,
            "word_count": word_count,
            "language": page.get("lang") or "undetected",
            "warnings": warnings
        },

        "seo_title": {
            "text": title,
            "length_chars": len(title),
            "pixels_est": title_px,
            "status_chars": title_status_chars,
            "status_pixels": title_status_px
        },

        "meta_description": {
            "text": meta,
            "length_chars": len(meta),
            "pixels_est": meta_px,
            "status_chars": meta_status_chars,
            "status_pixels": meta_status_px
        },

        "headings": {
            "h1": {"text": h1, "similarity_to_title": h1_title_sim},
            "h2_count": len(h2s),
            "h3_count": len(h3s),
            "heading_density_per_1000_words": heading_density_per_1k,
            "words_per_h2_avg": words_per_h2
        },

        "readability": {
            "flesch_reading_ease": flesch,
            "avg_sentence_length": avg_sentence_len,
            "sentences": len(sent_list)
        },

        "links": {
            "counts": {
                "total": counts.get("total", 0),
                "internal": counts.get("internal", 0),
                "external": counts.get("external", 0),
                "internal_ratio": counts.get("internal_ratio", 0.0),
                "external_ratio": counts.get("external_ratio", 0.0)
            },
            "internal_links": internal_links,
            "external_links": external_links
        },

        "keywords": fk_stats,

        "technical": {
            "canonical_present": bool(page.get("canonical")),
            "robots_present": bool(page.get("meta_robots")),
            "lang_present": bool(page.get("lang"))
        }
    }

    return report


def run(dir_path: str, input_file: str, thresholds: Dict, outfile: str = "report.json", pretty: bool = True) -> str:
    files = sorted([f for f in os.listdir(dir_path) if f.lower().endswith(".json")])
    if not files:
        raise FileNotFoundError(f"No JSON files in {dir_path}")

    src = os.path.join(dir_path, files[0])
    urls, fks = load_input_urls(input_file)

    report = analyze_one(src, fks, thresholds)
    out_path = os.path.join(dir_path, outfile)
    save_json(out_path, report, pretty=pretty)

    return out_path
