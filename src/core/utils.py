# utils.py (shared helpers) — English only
import os, re, json, hashlib
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import chardet
from w3lib.url import canonicalize_url, safe_url_string
import tldextract

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/118.0.0.0 Safari/537.36")

# optional language detect
try:
    from langdetect import detect as lang_detect  # type: ignore
except Exception:
    lang_detect = None  # type: ignore


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def slugify_path(path: str) -> str:
    if not path or path == "/":
        return "index"
    s = re.sub(r"[^\w\-]+", "-", path.strip("/").lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "index"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def detect_encoding(binary: bytes, default: str = "utf-8") -> str:
    guess = chardet.detect(binary)
    return guess.get("encoding") or default


def to_unicode(b: bytes) -> str:
    enc = detect_encoding(b)
    try:
        return b.decode(enc, errors="replace")
    except Exception:
        return b.decode("utf-8", errors="replace")


def clean_space(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def is_http(u: Optional[str]) -> bool:
    if not u:
        return False
    p = urlparse(u)
    return p.scheme in ("http", "https")


def norm_url(u: str) -> str:
    try:
        return canonicalize_url(safe_url_string(u))
    except Exception:
        return u


def host_key(url: str) -> str:
    e = tldextract.extract(url)
    return f"{e.domain}.{e.suffix}".lower() if e.suffix else e.domain.lower()


def dedup(seq: List[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict, pretty: bool = False, compact: bool = False) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    if compact:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    elif pretty:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


def load_input_urls(path: str) -> Tuple[List[str], List[str]]:
    data = load_json(path)
    urls: List[str] = []
    fks: List[str] = []
    if isinstance(data, dict):
        if "urls" in data and isinstance(data["urls"], list):
            urls += [u for u in data["urls"] if isinstance(u, str)]
        if "targets" in data and isinstance(data["targets"], list):
            for t in data["targets"]:
                if isinstance(t, dict) and isinstance(t.get("url"), str):
                    urls.append(t["url"])
        if "focuskeywords" in data and isinstance(data["focuskeywords"], list):
            fks += [k.strip() for k in data["focuskeywords"] if isinstance(k, str)]
    # unique
    urls = dedup(urls)
    fks = dedup([k for k in fks if k])
    return urls, fks


WORD_RE = re.compile(r"\b[\w’'-]+\b", flags=re.UNICODE)

def count_words(txt: str) -> int:
    return len(WORD_RE.findall(txt))


def detect_lang_fallback(text: str, default: Optional[str]) -> Optional[str]:
    if default:
        return default
    if not text or not lang_detect:
        return default
    try:
        return lang_detect(text[:1000])
    except Exception:
        return default


# Pixel estimation (approx)
def px_estimate(text: str, is_title: bool = False) -> int:
    if not text: return 0
    uc = len(re.findall(r"[A-Z]", text))
    lc = len(re.findall(r"[a-z]", text))
    dg = len(re.findall(r"[0-9]", text))
    sp = len(re.findall(r"\s", text))
    ot = max(0, len(text) - (uc + lc + dg + sp))
    px = uc*9.5 + lc*7.5 + dg*7.5 + sp*3.0 + ot*8.0
    return int(px * (1.05 if is_title else 1.0))
