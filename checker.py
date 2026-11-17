import os
import random
import re
import time
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import pandas as pd

# ==============================================================================
# CONFIGURATION
# ==============================================================================
INPUT_CSV = "parallel_verified_vat_hits.csv"
OUTPUT_CSV = "hmrc_portal_double_checked.csv"
PORTAL_URL = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"

# Set this to a number greater than 0 if you want to resume/skip already checked items
SKIP_FIRST_X_RECORDS = 42


# ==============================================================================
# PARSERS & CLASSIFIERS
# ==============================================================================
def parse_result_page(html: str, vat_input: str, crn: str) -> dict | None:
  soup = BeautifulSoup(html, "html.parser")

  panel = soup.select_one(".govuk-panel--confirmation")
  if not panel or "Valid UK VAT number" not in panel.get_text():
    return None

  name = ""
  address_text = ""

  for h3 in soup.find_all("h3", class_="govuk-heading-s"):
    h3_text = h3.get_text(strip=True).lower()
    if "registered business name" in h3_text:
      p_tag = h3.find_next_sibling("p", class_="govuk-body")
      if p_tag:
        name = p_tag.get_text(" ", strip=True)
    elif "registered business address" in h3_text:
      p_tag = h3.find_next_sibling("p", class_="govuk-body")
      if p_tag:
        address_text = p_tag.get_text(" ", strip=True)

  full_address = re.sub(r"\s+", " ", address_text).strip()
  pc_match = re.search(
      r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", full_address, re.I
  )
  postcode = pc_match.group(1).upper() if pc_match else ""

  if name:
    return {
        "CRN": crn,
        "VAT Number": vat_input,
        "Confirmed Company Name": name,
        "Postcode": postcode,
        "Registered Address": full_address,
    }
  return None


def classify_result_page(html: str) -> str:
  """Accurately separates Valid Hits, Genuinely Invalid VATs, and Real WAF Blocks."""
  soup = BeautifulSoup(html, "html.parser")
  page_text = soup.get_text(" ", strip=True).lower()

  # 1. SUCCESS: Confirmed registration panel is present
  if soup.select_one(".govuk-panel--confirmation"):
    return "SUCCESS"

  # 2. INVALID VAT: Explicitly matches GOV.UK 'VAT registration not found' page layout
  heading_xl = soup.select_one("h1.govuk-heading-xl")
  if (
      (heading_xl and "vat registration not found" in heading_xl.get_text().lower())
      or soup.select_one(".govuk-error-summary")
      or soup.select_one(".govuk-error-message")
      or "we could not find a matching vat registration" in page_text
      or "we could not find this vat number" in page_text
      or "check the vat number" in page_text
      or "is not a valid" in page_text
      or "not registered" in page_text
  ):
    return "INVALID_VAT"

  # 3. REAL WAF BLOCK / RATE LIMIT: Akamai block page or completely blank/broken load
  if (
      "access denied" in page_text
      or "too many requests" in page_text
      or "security check" in page_text
      or len(page_text) < 500
  ):
    return "WAF_BLOCKED"

  # Fallback: if it has standard HMRC header/footer layout but no error/success, check title text
  if "vat registration not found" in page_text:
    return "INVALID_VAT"

  return "WAF_BLOCKED"


# ==============================================================================
# MAIN EXECUTOR
# ==============================================================================
def run():
  if not os.path.exists(INPUT_CSV):
    print(
        f"[!] Input file '{INPUT_CSV}' not found. Please make sure it's in"
        " this folder."
    )
    return

  df = pd.read_csv(INPUT_CSV)
  if "VAT Number" not in df.columns or "CRN" not in df.columns:
    print("[!] Input CSV is missing 'VAT Number' or 'CRN' columns.")
    return

  # Clean data and keep unique mapping
  df["CleanVAT"] = df["VAT Number"].astype(str).str.replace(r"\D", "", regex=True)
  df = df[df["CleanVAT"].str.len() == 9].drop_duplicates(subset=["CleanVAT"])

  tasks = list(zip(df["CleanVAT"], df["CRN"]))
  total_records = len(tasks)
  
  if SKIP_FIRST_X_RECORDS > 0:
    tasks = tasks[SKIP_FIRST_X_RECORDS:]
    print(f"[*] Resuming: Skipped first {SKIP_FIRST_X_RECORDS} records. Remaining to check: {len(tasks)} (Total dataset: {total_records}).")
  else:
    print(f"[*] Loaded {total_records} unique records to check from {INPUT_CSV}.")

  print("[*] Launching real Chrome browser with updated GOV.UK parser...")

  confirmed_hits = 0

  with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, channel="chrome")
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
            " like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()

    idx = 0
    while idx < len(tasks):
      vat_9, crn = tasks[idx]
      current_num = SKIP_FIRST_X_RECORDS + idx + 1

      try:
        print(
            f"[{current_num}/{total_records}] Checking VAT: {vat_9} (CRN: {crn})..."
        )

        page.goto(PORTAL_URL, wait_until="domcontentloaded")
        page.fill('input[name="target"]', vat_9)
        page.press('input[name="target"]', "Enter")
        page.wait_for_load_state("domcontentloaded")
        html = page.content()

        status = classify_result_page(html)

        if status == "WAF_BLOCKED":
          print(
              "    [!] Real Akamai Block / Rate-Limit detected! Pausing for 45"
              " seconds to cool down..."
          )
          time.sleep(45)
          print("    [+] Resuming and retrying the same record...")
          continue  # Retries this exact index

        elif status == "INVALID_VAT":
          print("    -> Unregistered or genuinely invalid entity on portal.")
          idx += 1  # Correctly advances to the next record!

        elif status == "SUCCESS":
          parsed = parse_result_page(html, f"GB{vat_9}", str(crn).zfill(8))
          if parsed:
            confirmed_hits += 1
            print(
                f"\n >>> [CONFIRMED #{confirmed_hits}] CRN: {parsed['CRN']} |"
                f" {parsed['VAT Number']} -> {parsed['Confirmed Company Name']}"
                f" ({parsed['Postcode']})\n"
            )

            df_row = pd.DataFrame([parsed])
            file_exists = os.path.exists(OUTPUT_CSV)
            df_row.to_csv(
                OUTPUT_CSV, mode="a", index=False, header=not file_exists
            )
          idx += 1  # Correctly advances to the next record!

        time.sleep(random.uniform(5.0, 8.0))

      except Exception as e:
        print(f"    [!] Connection/Browser Error: {e}. Retrying in 10s...")
        time.sleep(10)

    browser.close()

  print(
      f"\n[+] Complete! Saved {confirmed_hits} confirmed hits to {OUTPUT_CSV}"
  )


if __name__ == "__main__":
  run()