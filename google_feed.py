#!/usr/bin/env python3
"""
Google Merchant Center product feed for Siebentaschen — no Google & YouTube app needed.

Pulls active, in-stock products from Shopify and writes an RSS 2.0 feed with
the g: namespace (one <item> per in-stock variant). Merchant Center fetches it
on a schedule from the public URL of this repo:

    https://raw.githubusercontent.com/stshophu/siebentaschen-feed/main/google_feed.xml

Rules (all driven by the Merchant Center misrepresentation clean-up, Oct 2026):
  * Products tagged `no-google` (e.g. "made with Loro Piana fabric") are left out.
  * Factory seconds ("2° choice" brands) are left out.
  * No sale_price / strikethrough: <g:price> is the actual selling price only.
  * <g:gtin> only for a real GTIN (valid check digit, not an in-store range);
    otherwise brand + mpn (the SKU) identify the item.
  * Item IDs use the same format as the Google & YouTube app
    (shopify_DE_<product>_<variant>) so existing item history carries over.

Usage:
    python google_feed.py --out google_feed.xml
    python google_feed.py --audit          # stats only, writes nothing

Environment:
    SHOPIFY_STORE   e.g. modalist-de.myshopify.com
    SHOPIFY_TOKEN   Admin API token (read_products, read_inventory)
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from collections import Counter
from decimal import Decimal
from typing import Iterator
from xml.sax.saxutils import escape

import requests

from feed_enrich import (color_from_description, english_color, feed_title,
                         google_category, material, size_system)

API_VERSION = "2025-07"
STOREFRONT = "https://siebentaschen.com"
COUNTRY = "DE"
EXCLUDE_TAGS = {"no-google", "fabric-brand"}
SECONDS_VENDOR = re.compile(r"2\s*°\s*choice", re.IGNORECASE)

PRODUCTS_QUERY = """
query Feed($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle vendor productType tags descriptionHtml onlineStoreUrl
      seo { title }
      googleCategory: metafield(namespace: "mm-google-shopping", key: "google_product_category") { value }
      media(first: 10) { nodes { preview { image { url } } } }
      variants(first: 100) {
        nodes {
          id sku barcode price inventoryQuantity
          selectedOptions { name value }
        }
      }
    }
  }
}
"""


def shopify_products(store: str, token: str) -> Iterator[dict]:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    cursor = None
    while True:
        resp = requests.post(url, json={"query": PRODUCTS_QUERY,
                                        "variables": {"cursor": cursor}},
                             headers=headers, timeout=90)
        if resp.status_code in (429, 502, 503):
            time.sleep(4)
            continue
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in payload["errors"]):
                time.sleep(4)
                continue
            raise RuntimeError(payload["errors"])
        block = payload["data"]["products"]
        yield from block["nodes"]
        if not block["pageInfo"]["hasNextPage"]:
            return
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(0.4)


def valid_gtin(code: str | None) -> bool:
    """Real, publicly usable GTIN: right length, right check digit, not an in-store range."""
    g = (code or "").strip()
    if not g.isdigit() or len(g) not in (8, 12, 13, 14) or len(set(g)) == 1:
        return False
    digits = [int(c) for c in g][::-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[1:]))
    if (10 - total % 10) % 10 != digits[0]:
        return False
    g13 = g.zfill(13) if len(g) < 13 else g[-13:]
    return not (g13[0] == "2" or g13[:2] in ("02", "04"))


TAGS_RE = re.compile(r"<[^>]+>")
JUNK_RE = re.compile(r"<style.*?</style>|<script.*?</script>|<meta[^>]*>", re.S | re.I)


def clean_description(raw: str | None, fallback: str) -> str:
    t = JUNK_RE.sub(" ", raw or "")
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", t, flags=re.I)
    t = html.unescape(TAGS_RE.sub(" ", t))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return (t or fallback)[:5000]


def option(variant: dict, *names: str) -> str | None:
    for opt in variant.get("selectedOptions") or []:
        if opt["name"].strip().lower() in names:
            v = (opt.get("value") or "").strip()
            if v and v.lower() not in ("default title", "default"):
                return v
    return None


def gender_and_age(tags: list[str], product_type: str, title: str) -> tuple[str, str]:
    words = {t.strip().lower() for t in tags} | set(re.findall(r"[a-z]+", f"{product_type} {title}".lower()))
    if words & {"junior", "kids", "kid", "boy", "girl", "baby"}:
        age = "kids"
    else:
        age = "adult"
    women = bool(words & {"woman", "women", "female", "damen"})
    men = bool(words & {"man", "men", "male", "herren"})
    if women and not men:
        return "female", age
    if men and not women:
        return "male", age
    return "unisex", age


def text(tag: str, value) -> str:
    return f"<{tag}>{escape(str(value))}</{tag}>"


def build_item(product: dict, variant: dict, images: list[str], description: str) -> str:
    pid = product["id"].rsplit("/", 1)[-1]
    vid = variant["id"].rsplit("/", 1)[-1]
    url = product.get("onlineStoreUrl") or f"{STOREFRONT}/products/{product['handle']}"
    gender, age = gender_and_age(product.get("tags") or [], product.get("productType") or "",
                                 product["title"])
    sku = (variant.get("sku") or "").strip()
    barcode = (variant.get("barcode") or "").strip()
    gcat = (product.get("googleCategory") or {}).get("value")
    ptype = (product.get("productType") or "").strip()
    color = english_color(option(variant, "color", "colour", "colore")) \
        or color_from_description(description)
    size = option(variant, "size", "shoe size", "accessory size", "taglia", "misura")
    mat = material(description)
    gcat = google_category(gcat, ptype, product["title"])
    title = feed_title(product["title"], (product.get("seo") or {}).get("title"),
                       product["vendor"].strip(), gender, age, color, mat)

    L = ["<item>",
         text("g:id", f"shopify_{COUNTRY}_{pid}_{vid}"),
         text("g:item_group_id", f"shopify_{COUNTRY}_{pid}"),
         text("g:title", title),
         text("g:description", description),
         text("g:link", f"{url}?variant={vid}"),
         text("g:image_link", images[0])]
    L += [text("g:additional_image_link", img) for img in images[1:10]]
    L += [text("g:availability", "in_stock"),
          text("g:price", f"{Decimal(variant['price']):.2f} EUR"),
          text("g:condition", "new"),
          text("g:brand", product["vendor"].strip())]
    if valid_gtin(barcode):
        L.append(text("g:gtin", barcode))
    if sku:
        L.append(text("g:mpn", sku[:70]))
    if gcat:
        L.append(text("g:google_product_category", gcat))
    if ptype:
        L.append(text("g:product_type", ptype))
    L += [text("g:gender", gender), text("g:age_group", age)]
    if color:
        L.append(text("g:color", color[:100]))
    if size:
        L.append(text("g:size", size[:100]))
        system = size_system(size, description)
        if system:
            L.append(text("g:size_system", system))
    if mat:
        L.append(text("g:material", mat))
    L.append("</item>")
    return "".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="google_feed.xml")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--small-out", default=None,
                    help="also write a reduced feed (for accounts with a low item limit)")
    ap.add_argument("--small-max", type=int, default=400,
                    help="max items in the reduced feed (all sizes of a product kept together)")
    args = ap.parse_args()

    store, token = os.environ.get("SHOPIFY_STORE"), os.environ.get("SHOPIFY_TOKEN")
    if not store or not token:
        print("SHOPIFY_STORE and SHOPIFY_TOKEN must be set", file=sys.stderr)
        return 2

    items: list[str] = []
    groups: list[tuple] = []   # (has_gtin, max_price, [item xml]) per product, for the reduced feed
    stats = Counter()
    for product in shopify_products(store, token):
        stats["products_seen"] += 1
        tags = {t.strip().lower() for t in product.get("tags") or []}
        if tags & EXCLUDE_TAGS:
            stats["skipped_no_google"] += 1
            continue
        if SECONDS_VENDOR.search(product.get("vendor") or ""):
            stats["skipped_second_choice"] += 1
            continue
        if not (product.get("vendor") or "").strip():
            stats["skipped_no_brand"] += 1
            continue
        images = [n["preview"]["image"]["url"] for n in product["media"]["nodes"]
                  if (n.get("preview") or {}).get("image", {}).get("url")]
        if not images:
            stats["skipped_no_image"] += 1
            continue
        in_stock = [v for v in product["variants"]["nodes"]
                    if (v.get("inventoryQuantity") or 0) > 0 and Decimal(v["price"]) > 0]
        if not in_stock:
            stats["skipped_out_of_stock"] += 1
            continue
        description = clean_description(product.get("descriptionHtml"), product["title"])
        group = []
        for v in in_stock:
            xml = build_item(product, v, images, description)
            items.append(xml)
            group.append(xml)
            stats["items_written"] += 1
            stats["with_gtin"] += valid_gtin(v.get("barcode"))
        groups.append((any(valid_gtin(v.get("barcode")) for v in in_stock),
                       max(Decimal(v["price"]) for v in in_stock), group))

    print("--- Summary ---")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    if args.audit:
        print("(audit mode: no feed written)")
        return 0

    write_feed(args.out, items)
    if args.small_out:
        # Reduced feed: products with a valid GTIN first, then highest price;
        # whole products only, until the item cap is reached.
        small: list[str] = []
        for _, _, group in sorted(groups, key=lambda g: (not g[0], -g[1])):
            if len(small) + len(group) > args.small_max:
                continue
            small.extend(group)
        write_feed(args.small_out, small)
    return 0


def write_feed(path: str, items: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>'
                "<title>Siebentaschen</title>"
                f"<link>{STOREFRONT}</link>"
                "<description>Siebentaschen product feed for Google Merchant Center</description>\n")
        f.write("\n".join(items))
        f.write("\n</channel></rss>\n")
    print(f"Wrote {len(items)} items to {path}")


if __name__ == "__main__":
    sys.exit(main())
