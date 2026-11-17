from concurrent.futures import ThreadPoolExecutor
import os
import queue
import re
import threading
import time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from ddgs import DDGS
import pandas as pd
from requests.adapters import HTTPAdapter
import requests

# ==============================================================================
# CONFIGURATION
# ==============================================================================
COMPANIES_HOUSE_CSV = "BasicCompanyData-2026-11-01-part1_7.csv"
OUTPUT_CSV = "parallel_verified_vat_hits.csv"

DISCOVERY_WORKERS = 10  # Dedicated threads finding websites
CRAWL_WORKERS = 30  # Dedicated threads crawling & extracting VATs

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

VAT_REGEX = re.compile(
    r"(?:VAT|V\.A\.T\.?)[\s#:.-]*(?:No|Number|Reg|Registration)?[\s#:.-]*(?:GB|XI)?\s*(\d{3}[\s-]?\d{4}[\s-]?\d{2})",
    re.I,
)
CRN_REGEX = re.compile(
    r"(?:Company|Reg(?:istration)?)[\s#:.-]*(?:No|Number)?[\s#:.-]*(\d{8}|[A-Z]{2}\d{6})",
    re.I,
)

DIRECTORY_BLOCKLIST = {
    "company-information.service.gov.uk",
    "companieshouse.gov.uk",
    "endole.co.uk",
    "checkcompany.co.uk",
    "opencorporates.com",
    "pomanda.com",
    "linkedin.com",
    "facebook.com",
    "yell.com",
    "wikipedia.org",
}

SESSION = requests.Session()
ADAPTER = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
SESSION.mount("http://", ADAPTER)
SESSION.mount("https://", ADAPTER)
SESSION.headers.update(HEADERS)


# ==============================================================================
# 1. MODULUS-97 CHECK-DIGIT VALIDATOR
# ==============================================================================
def is_valid_uk_vat(vat_9: str) -> bool:
  if not vat_9.isdigit() or len(vat_9) != 9 or vat_9 == "000000000":
    return False
  digits = [int(d) for d in vat_9]
  total = sum(d * w for d, w in zip(digits[:7], [8, 7, 6, 5, 4, 3, 2]))
  check = int(vat_9[7:])
  rem = total % 97
  std = (97 - rem) if rem != 0 else 0
  return check in (std, (std + 55) % 97)


# ==============================================================================
# 2. DISCOVERY PRODUCER WORKER
# ==============================================================================
def discover_worker_task(
    item_tuple, crawl_queue: queue.Queue, crawl_active_counter: list
):
  _, row = item_tuple
  name = row["CompanyName"]
  postcode = row["RegAddress.PostCode"]

  clean = re.sub(
      r"\b(LIMITED|LTD|LLP|PLC|UK|THE|AND|&)\b", " ", name, flags=re.I
  )
  slug = re.sub(r"[^a-z0-9]", "", clean.lower())
  website = None

  # 1. Heuristic check
  if len(slug) >= 4:
    for url in (f"https://www.{slug}.co.uk", f"https://www.{slug}.com"):
      try:
        r = SESSION.get(url, timeout=2.5, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 500:
          website = r.url
          break
      except Exception:
        continue

  # 2. Search fallback
  if not website:
    query = f'"{clean.strip()}" "{postcode}"'
    try:
      with DDGS(timeout=4) as ddgs:
        results = list(ddgs.text(query, region="uk-en", max_results=2))
      for r in results:
        href = r.get("href", "")
        parsed = urlparse(href)
        domain = parsed.netloc.lower().replace("www.", "")
        if domain and not any(bad in domain for bad in DIRECTORY_BLOCKLIST):
          website = f"{parsed.scheme or 'https'}://{parsed.netloc}"
          break
    except Exception:
      pass

  if website:
    print(
        f"[Discovery] Found website for '{name}' -> {website}. Moved to"
        " crawling pool."
    )
  else:
    print(f"[Discovery] No website found for '{name}'.")

  crawl_queue.put(("CRAWL", row, website))
  crawl_active_counter[0] += 1


# ==============================================================================
# 3. CRAWLING & EXTRACTION CONSUMER (WRITES ONLY HITS)
# ==============================================================================
def crawl_worker_task(
    crawl_queue: queue.Queue,
    file_lock: threading.Lock,
    stats: dict,
):
  while True:
    try:
      task = crawl_queue.get(timeout=3.0)
    except queue.Empty:
      break

    _, row, website = task
    crn = str(row["CompanyNumber"]).strip().zfill(8)
    name = str(row["CompanyName"].strip())
    postcode = str(row["RegAddress.PostCode"].strip())

    if not website:
      res = {
          "Company Name": name,
          "CRN": crn,
          "Postcode": postcode,
          "Website": None,
          "VAT Number": None,
          "Status": "NO_WEBSITE_FOUND",
      }
    else:
      print(f"[Crawling] Starting page fetch for '{name}' at {website}...")
      pages = []
      try:
        resp = SESSION.get(website, timeout=4.5)
        if resp.status_code == 200:
          soup = BeautifulSoup(resp.text, "html.parser")
          pages.append({"url": resp.url, "text": soup.get_text(" ")})

          base_domain = urlparse(resp.url).netloc
          sub_urls = []
          for a in soup.find_all("a", href=True):
            full = urljoin(resp.url, a["href"].strip()).split("#")[0]
            lbl = a.get_text(" ", strip=True).lower()
            if urlparse(full).netloc == base_domain:
              if any(
                  k in full.lower() or k in lbl
                  for k in ("contact", "terms", "privacy", "about", "legal")
              ):
                sub_urls.append(full)

          for u in list(set(sub_urls))[:2]:
            try:
              sr = SESSION.get(u, timeout=3.5)
              if sr.status_code == 200:
                pages.append({
                    "url": u,
                    "text": BeautifulSoup(sr.text, "html.parser").get_text(" "),
                })
            except Exception:
              continue
      except Exception:
        pass

      if not pages:
        res = {
            "Company Name": name,
            "CRN": crn,
            "Postcode": postcode,
            "Website": website,
            "VAT Number": None,
            "Status": "SITE_UNREACHABLE",
        }
      else:
        vats = []
        source_url = ""
        for p in pages:
          for raw in VAT_REGEX.findall(p["text"]):
            digits = re.sub(r"\D", "", raw)
            if is_valid_uk_vat(digits) and digits not in vats:
              vats.append(digits)
              if not source_url:
                source_url = p["url"]

        if not vats:
          res = {
              "Company Name": name,
              "CRN": crn,
              "Postcode": postcode,
              "Website": website,
              "VAT Number": None,
              "Status": "NO_VAT_FOUND",
          }
        else:
          res = {
              "Company Name": name,
              "CRN": crn,
              "Postcode": postcode,
              "Website": website,
              "VAT Number": f"GB{vats[0]}",
              "Source Page": source_url,
              "Status": "VERIFIED_VAT",
          }

    # Thread-safe logging and conditional parallel write (Only if VAT found)
    with file_lock:
      stats["processed"] += 1

      if res["VAT Number"]:
        stats["hits"] += 1
        df_row = pd.DataFrame([res])
        file_exists = os.path.exists(OUTPUT_CSV)
        df_row.to_csv(
            OUTPUT_CSV, mode="a", index=False, header=not file_exists
        )
        print(
            f"\n >>> [HIT #{stats['hits']}] Found VAT {res['VAT Number']} for"
            f" '{res['Company Name']}' -> Stored in CSV!\n"
        )
      else:
        print(
            f"[-] Completed '{res['Company Name']}' ({res['Status']}) | Total"
            f" Processed: {stats['processed']}"
        )

    crawl_queue.task_done()


# ==============================================================================
# 4. MAIN PIPELINE ORCHESTRATOR
# ==============================================================================
def main():
  t0 = time.time()
  crawl_queue = queue.Queue()
  file_lock = threading.Lock()
  total_counter = [0]
  crawl_active_counter = [0]
  stats = {"processed": 0, "hits": 0}

  print(
      f"[*] Initializing pipeline with CSV: {COMPANIES_HOUSE_CSV} -> Hits Output:"
      f" {OUTPUT_CSV}"
  )

  with ThreadPoolExecutor(
      max_workers=DISCOVERY_WORKERS + CRAWL_WORKERS
  ) as executor:
    # 1. Start Crawler Workers (Consumer Pool)
    for _ in range(CRAWL_WORKERS):
      executor.submit(crawl_worker_task, crawl_queue, file_lock, stats)

    # 2. Stream CSV and launch Discovery Producers
    def discovery_producer_loop():
      cols = [
          "CompanyNumber",
          "CompanyName",
          "CompanyStatus",
          "RegAddress.PostCode",
          "Accounts.AccountCategory",
      ]
      for chunk in pd.read_csv(
          COMPANIES_HOUSE_CSV,
          usecols=lambda c: c.strip() in cols,
          chunksize=100_000,
      ):
        chunk.columns = [c.strip() for c in chunk.columns]
        filtered = chunk[
            (chunk["CompanyStatus"] == "Active")
            & (
                chunk["Accounts.AccountCategory"].isin(
                    ["FULL", "MEDIUM", "SMALL", "TOTAL EXEMPTION FULL"]
                )
            )
            & (chunk["RegAddress.PostCode"].notna())
        ]
        for row in filtered.to_dict(orient="records"):
          total_counter[0] += 1
          executor.submit(
              discover_worker_task,
              ("DISCOVER", row),
              crawl_queue,
              crawl_active_counter,
          )

    executor.submit(discovery_producer_loop)

    while total_counter[0] == 0 or crawl_active_counter[0] < total_counter[0]:
      time.sleep(1.0)

    crawl_queue.join()

  print(
      f"\n[+] Pipeline Complete! Processed {stats['processed']} companies in"
      f" {time.time() - t0:.1f}s. Found and saved {stats['hits']} verified"
      f" VAT hits to {OUTPUT_CSV}"
  )


if __name__ == "__main__":
  main()