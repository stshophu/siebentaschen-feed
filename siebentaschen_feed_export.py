#!/usr/bin/env python3
"""
Siebentaschen (Shopify) product feed exporter.

Pulls every product that is:
  - status: ACTIVE
  - has inventory available (in stock)
  - has a price > 0 (no 0-priced products/variants)

and writes them to a CSV.

SETUP
-----
1. pip install requests
2. Set environment variables (or edit the constants below):
     SHOPIFY_STORE_DOMAIN        e.g. "siebentaschen.myshopify.com"
     SHOPIFY_ADMIN_TOKEN         Admin API access token with `read_products`
                                 scope (Settings > Apps > Develop apps, or
                                 your existing private app used for
                                 RewixSync/BUYMA).
     SUPABASE_URL                e.g. "https://abcdefgh.supabase.co"
     SUPABASE_SERVICE_ROLE_KEY   service_role key (NOT the anon key —
                                 needed to write to Storage). Find it in
                                 Supabase: Settings > API.
     SUPABASE_BUCKET             name of a PUBLIC Storage bucket to upload
                                 into, e.g. "feeds" (create it once in
                                 Supabase: Storage > New bucket > toggle
                                 "Public bucket" on).
   Optional:
     SUPABASE_FILE_PATH          path inside the bucket, defaults to
                                 "siebentaschen_product_feed.csv"
3. Run: python3 siebentaschen_feed_export.py

OUTPUT
------
- siebentaschen_product_feed.csv written locally, one row per qualifying
  variant: Handle, Title, Vendor, Product Type, SKU, Variant, Price,
  Currency, Inventory, Image URL, Product URL
- The same file is uploaded (upserted) to the Supabase Storage bucket
  above. The PUBLIC URL is always:
    {SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{SUPABASE_FILE_PATH}
  That URL never changes between runs — only the file content does — so
  it's safe to hand to a marketplace/feed consumer once and forget it.

SCHEDULING (every 15 minutes)
------------------------------
Deploy this as a Render "Cron Job" (not a Web Service — no need to serve
HTTP, since Supabase Storage is doing that):
  - Runtime: Python 3
  - Build command:   pip install -r requirements.txt
  - Command:         python3 siebentaschen_feed_export.py
  - Schedule:        */15 * * * *
  - Environment:     add the env vars listed above as Render secrets
Render spins up a fresh container per run, executes the script, and shuts
down — so the script intentionally doesn't keep any state between runs.
"""

import csv
import os
import sys
import time
import requests

STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")   # e.g. siebentaschen.myshopify.com
ACCESS_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
API_VERSION = "2025-01"
OUTPUT_FILE = "siebentaschen_product_feed.csv"
STOREFRONT_BASE_URL = "https://siebentaschen.com/products/"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "feeds")
SUPABASE_FILE_PATH = os.environ.get("SUPABASE_FILE_PATH", "siebentaschen_product_feed.csv")

QUERY = """
query Products($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active AND inventory_total:>0 AND price:>0") {
    edges {
      node {
        handle
        title
        vendor
        productType
        featuredImage { url }
        variants(first: 100) {
          edges {
            node {
              title
              sku
              price
              inventoryQuantity
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def fetch_page(cursor):
    url = f"https://{STORE_DOMAIN}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    resp = requests.post(
        url,
        headers=headers,
        json={"query": QUERY, "variables": {"cursor": cursor}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]["products"]


def upload_to_supabase(local_path):
    """Upsert the CSV into a public Supabase Storage bucket and return
    its stable public URL."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print(
            "WARNING: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — "
            "skipping upload, CSV only saved locally.",
            file=sys.stderr,
        )
        return None

    upload_url = (
        f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{SUPABASE_FILE_PATH}"
    )
    with open(local_path, "rb") as f:
        resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "text/csv",
                "x-upsert": "true",  # overwrite if it already exists
            },
            data=f,
            timeout=60,
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Supabase upload failed ({resp.status_code}): {resp.text}"
        )

    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{SUPABASE_FILE_PATH}"


def main():
    if not STORE_DOMAIN or not ACCESS_TOKEN:
        print(
            "ERROR: Set SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_TOKEN environment "
            "variables before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    rows_written = 0
    products_seen = 0
    cursor = None

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Handle", "Title", "Vendor", "Product Type", "SKU",
                "Variant", "Price", "Currency", "Inventory",
                "Image URL", "Product URL",
            ]
        )

        page_num = 1
        while True:
            page = fetch_page(cursor)
            edges = page["edges"]
            products_seen += len(edges)

            for edge in edges:
                p = edge["node"]
                handle = p["handle"]
                image_url = (p.get("featuredImage") or {}).get("url", "")
                product_url = STOREFRONT_BASE_URL + handle

                for vedge in p["variants"]["edges"]:
                    v = vedge["node"]
                    try:
                        price = float(v["price"])
                    except (TypeError, ValueError):
                        continue
                    inv = v.get("inventoryQuantity") or 0

                    # Belt-and-suspenders filter at the variant level,
                    # since the product-level query can include products
                    # where only SOME variants are in stock / priced.
                    if price <= 0 or inv <= 0:
                        continue

                    writer.writerow(
                        [
                            handle,
                            p["title"],
                            p["vendor"],
                            p["productType"],
                            v.get("sku", ""),
                            v.get("title", ""),
                            f"{price:.2f}",
                            "EUR",
                            inv,
                            image_url,
                            product_url,
                        ]
                    )
                    rows_written += 1

            print(f"Page {page_num}: {len(edges)} products processed, "
                  f"{rows_written} rows written so far.")

            page_info = page["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
            page_num += 1
            time.sleep(0.5)  # gentle on rate limits

    print(f"\nDone. {products_seen} products scanned, "
          f"{rows_written} in-stock/active/priced variant rows written to {OUTPUT_FILE}.")

    public_url = upload_to_supabase(OUTPUT_FILE)
    if public_url:
        print(f"Uploaded to Supabase. Public feed URL:\n{public_url}")


if __name__ == "__main__":
    main()
