# src/runcrawl.py
import sys
from pathlib import Path

# ضروري تضبط path باش يلقا imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from backend.app.service_crawler import start_crawl_job, run_crawl_job_sync
from backend.app.schemas_crawler import CrawlBody

def main():
    # عدل هاد القيم على حسب الحاجة
    domain = "https://fitnovahealth.com"
    max_pages = 300

    body = CrawlBody(domain=domain, max_pages=max_pages)
    status = start_crawl_job(body)
    print(f"[SCRIPT] Started crawl job: {status.job_id}")

    run_crawl_job_sync(status.job_id, body)

    print(f"[SCRIPT] Crawl finished. Check data/cache/crawl_status.json and data/reports/ for output.")

if __name__ == "__main__":
    main()
