# run.py — Orchestrate A (fetch) + B (extract) + C (analyze)
import os
from urllib.parse import urlparse

from core.utils import (
    load_json, load_input_urls, ensure_dir, slugify_path, save_json
)
from core.fetcher import fetch
from core.extractor import html_bytes_to_json
from core.analyzer import run as analyze_run

def main():
    cfg = load_json("src/config.json")
    paths = cfg["paths"]
    http  = cfg["http"]
    thr   = cfg["thresholds"]

    input_file   = paths["input_file"]
    cache_dir    = paths["save_dir"]
    reports_dir  = paths["reports_dir"]

    ensure_dir(cache_dir)
    ensure_dir(reports_dir)

    urls, _ = load_input_urls(input_file)
    if not urls:
        print("[ERR] No URLs in Input.json"); return

    made_json = []
    for u in urls:
        print(f"[FETCH] {u}")
        ok, meta, content = fetch(
            u,
            ua=http.get("user_agent"),
            engine=http.get("engine", "httpx"),
            timeout=http.get("timeout", 25),
            http2=http.get("http2", True),
            retries=http.get("retries", 2),
            proxy=http.get("proxy")
        )
        if not ok:
            print(f"[ERR] Fetch failed: {u} (status={meta.get('status')})")
            continue

        # build JSON path
        p = urlparse(meta["final_url"])
        json_name = f"{p.netloc}_{slugify_path(p.path)}.json"
        json_path = os.path.join(cache_dir, json_name).replace("\\", "/")

        outp = html_bytes_to_json(
            html_bytes=content,
            final_url=meta["final_url"],
            save_path=json_path,
            base_override=None,
            pretty=True,
            compact=False
        )
        print(f"[JSON] {outp}")
        made_json.append(outp)

    if not made_json:
        print("[ERR] No JSON produced. Abort analyze.")
        return

    # Analyze first produced JSON into report.json (inside cache dir)
    report_path = analyze_run(cache_dir, input_file, thr, outfile="report.json", pretty=True)
    print(f"[REPORT] {report_path}")

if __name__ == "__main__":
    main()
