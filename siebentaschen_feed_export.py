#!/usr/bin/env python3
"""
Siebentaschen (Shopify) product feed exporter.

Pulls every product that is:
  - status: ACTIVE
  - has inventory available (in stock)
  - has a price > 0 (no 0-priced products/variants)

and writes them to siebentaschen_product_feed.csv with the fields
needed for marketplace feed onboarding (EAN/GTIN, category,
subcategory, delivery time, shipping cost, etc).

Required environment variables:
  SHOPIFY_STORE_DOMAIN   e.g. "siebentaschen.myshopify.com"
  SHOPIFY_ADMIN_TOKEN    Admin API access token with read_products scope

Optional environment variables (business policy values):
  LIEFERZEIT              delivery time shown to the marketplace.
                          Default: "3-5 Werktage" — override if wrong.
  SHIPPING_FREE_THRESHOLD order value (EUR) at and above which shipping
                          is free. Default: "150.00"
  SHIPPING_FLAT_RATE      shipping cost (EUR) below that threshold.
                          Default: "4.00"

Versandkosten is computed per row as: free if that item's own price
is >= SHIPPING_FREE_THRESHOLD, otherwise SHIPPING_FLAT_RATE. This
mirrors Siebentaschen's real DE shipping tiers (EUR 4.00 up to
EUR 149.99, free from EUR 150.00) the way a single-item feed needs to
represent a cart-level rule.
"""

import csv
import os
import sys
import time
import requests

STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")
ACCESS_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
API_VERSION = "2025-01"
OUTPUT_FILE = "siebentaschen_product_feed.csv"
STOREFRONT_BASE_URL = "https://siebentaschen.com/products/"

# Business policy values — not present in Shopify product data.
# LIEFERZEIT is a flat default; override via env var if it's wrong.
LIEFERZEIT = os.environ.get("LIEFERZEIT", "3-5 Werktage")

# Versandkosten follows Siebentaschen's actual DE shipping tiers:
# EUR 4.00 for orders up to EUR 149.99, free from EUR 150.00.
# A product feed needs one value per row, so this computes what
# shipping would cost if that item were bought alone — the standard
# way to represent a cart-level threshold in a per-product feed.
SHIPPING_FREE_THRESHOLD = float(os.environ.get("SHIPPING_FREE_THRESHOLD", "150.00"))
SHIPPING_FLAT_RATE = os.environ.get("SHIPPING_FLAT_RATE", "4.00")


def compute_versandkosten(price):
    return "0.00" if price >= SHIPPING_FREE_THRESHOLD else SHIPPING_FLAT_RATE

QUERY = """
query Products($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active AND inventory_total:>0 AND price:>0") {
    edges {
      node {
        handle
        title
        vendor
        productType
        featuredMedia { preview { image { url } } }
        googleCategory: metafield(namespace: "custom", key: "google_product_category") {
          value
        }
        subCategory: metafield(namespace: "custom", key: "category") {
          value
        }
        variants(first: 100) {
          edges {
            node {
              title
              sku
              barcode
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


def derive_category_subcategory(google_cat_value, custom_cat_value, product_type):
    """
    google_cat_value looks like "Apparel & Accessories > Clothing > Dresses"
    custom_cat_value looks like "Dresses" (a clean leaf-level subcategory)

    Falls back to product_type when metafields are missing, so every row
    still gets a usable value instead of an empty cell.
    """
    segments = []
    if google_cat_value:
        segments = [s.strip() for s in google_cat_value.split(">")]

    if len(segments) >= 2:
        category = segments[1]
    elif segments:
        category = segments[0]
    else:
        category = product_type

    if custom_cat_value:
        subcategory = custom_cat_value
    elif segments:
        subcategory = segments[-1]
    else:
        subcategory = product_type

    return category, subcategory


def main():
    if not STORE_DOMAIN or not ACCESS_TOKEN:
        print(
            "ERROR: Set SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_TOKEN "
            "environment variables before running this script.",
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
                "Handle",
                "Artikelnummer im Shop",   # SKU
                "EAN/GTIN",                # barcode
                "Produktname",             # Title
                "Vendor",
                "Kategorie",               # Category
                "Subkategorie",            # Subcategory
                "Produktart",              # Shopify Product Type
                "Variant",
                "Preis (Brutto)",          # Price
                "Currency",
                "Lieferzeit",
                "Versandkosten",
                "Inventory",
                "Image URL",
                "Produkt URL",
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
                preview = (p.get("featuredMedia") or {}).get("preview") or {}
                image_url = (preview.get("image") or {}).get("url", "")
                product_url = STOREFRONT_BASE_URL + handle

                google_cat_value = (p.get("googleCategory") or {}).get("value")
                custom_cat_value = (p.get("subCategory") or {}).get("value")
                category, subcategory = derive_category_subcategory(
                    google_cat_value, custom_cat_value, p["productType"]
                )

                for vedge in p["variants"]["edges"]:
                    v = vedge["node"]
                    try:
                        price = float(v["price"])
                    except (TypeError, ValueError):
                        continue
                    inv = v.get("inventoryQuantity") or 0

                    if price <= 0 or inv <= 0:
                        continue

                    writer.writerow(
                        [
                            handle,
                            v.get("sku", ""),
                            v.get("barcode", "") or "",
                            p["title"],
                            p["vendor"],
                            category,
                            subcategory,
                            p["productType"],
                            v.get("title", ""),
                            f"{price:.2f}",
                            "EUR",
                            LIEFERZEIT,
                            compute_versandkosten(price),
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
            time.sleep(0.5)

    print(f"\nDone. {products_seen} products scanned, "
          f"{rows_written} in-stock/active/priced variant rows written to {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
