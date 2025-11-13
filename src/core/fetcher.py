# fetcher.py — fetch HTML (memory), optionally save raw HTML (debug)
import time
from typing import Optional, Tuple

from .utils import DESKTOP_UA, detect_encoding

# optional libs
try:
    import httpx
except Exception:
    httpx = None
try:
    import requests
except Exception:
    requests = None


def fetch_httpx(url: str, ua: str, timeout: int, http2: bool, retries: int, proxy: Optional[str]):
    if httpx is None:
        raise RuntimeError("httpx is not installed")
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    backoff = 0.5
    last_exc = None
    client_kwargs = dict(http2=http2, headers=headers, follow_redirects=True, timeout=timeout)
    if proxy:
        client_kwargs["transport"] = httpx.HTTPTransport(proxy=proxy)
    start = time.perf_counter()
    with httpx.Client(**client_kwargs) as client:
        for _ in range(retries + 1):
            try:
                r = client.get(url)
                dur = int((time.perf_counter() - start) * 1000)
                return r, dur
            except httpx.RequestError as e:
                last_exc = e
                time.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
    raise last_exc  # type: ignore


def fetch_requests(url: str, ua: str, timeout: int, proxy: Optional[str]):
    if requests is None:
        raise RuntimeError("requests is not installed")
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    sess = requests.Session()
    start = time.perf_counter()
    r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True,
                 proxies={"http": proxy, "https": proxy} if proxy else None)
    dur = int((time.perf_counter() - start) * 1000)
    return r, dur


def fetch(url: str,
          ua: str = DESKTOP_UA,
          engine: str = "httpx",
          timeout: int = 25,
          http2: bool = True,
          retries: int = 2,
          proxy: Optional[str] = None) -> Tuple[bool, dict, bytes]:
    if engine == "requests":
        r, dur = fetch_requests(url, ua, timeout, proxy)
    else:
        r, dur = fetch_httpx(url, ua, timeout, http2, retries, proxy)

    status = getattr(r, "status_code", None)
    final_url = str(getattr(r, "url", url))
    content = getattr(r, "content", b"")
    enc = (getattr(r, "encoding", None) or detect_encoding(content))

    meta = {
        "url": url,
        "final_url": final_url,
        "status": status,
        "duration_ms": dur,
        "size_bytes": len(content),
        "encoding_guess": enc
    }
    ok = 200 <= (status or 0) < 400
    return ok, meta, content
