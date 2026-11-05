#!/usr/bin/env python3
"""
Fill empty image alt texts on Siebentaschen products (for Google Images).

Alt text = the same descriptive title the Google feed uses
("Dolce & Gabbana Men's Sweatshirt Blue – Cotton"); images after the first
get " – view 2", " – view 3" … so every image has a distinct, readable alt.

Only images WITHOUT an alt text are touched, unless --overwrite is given.
Without --apply nothing is written (dry run: prints samples + counts).

Usage:
    python alt_text_backfill.py                 # dry run
    python alt_text_backfill.py --apply         # write to Shopify
Environment:
    SHOPIFY_STORE, SHOPIFY_TOKEN  (token needs write_products / write_files)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests

from feed_enrich import color_from_description, english_color, feed_title, material
from google_feed import API_VERSION, clean_description, gender_and_age, option

QUERY = """
query Alt($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title vendor productType tags descriptionHtml
      seo { title }
      media(first: 20) { nodes { id alt mediaContentType } }
      variants(first: 1) { nodes { selectedOptions { name value } } }
    }
  }
}
"""

MUTATION = """
mutation Alt($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) { files { id } userErrors { field message code } }
}
"""


def gql(store: str, token: str, query: str, variables: dict) -> dict:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for _ in range(8):
        r = requests.post(url, json={"query": query, "variables": variables},
                          headers=headers, timeout=90)
        if r.status_code in (429, 502, 503):
            time.sleep(4)
            continue
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in data["errors"]):
                time.sleep(4)
                continue
            raise RuntimeError(data["errors"])
        return data["data"]
    raise RuntimeError("Shopify API kept throttling")


def alt_for(product: dict) -> str:
    desc = clean_description(product.get("descriptionHtml"), product["title"])
    gender, age = gender_and_age(product.get("tags") or [], product.get("productType") or "",
                                 product["title"])
    variants = product["variants"]["nodes"]
    v = variants[0] if variants else {}
    color = english_color(option(v, "color", "colour", "colore")) or color_from_description(desc)
    return feed_title(product["title"], (product.get("seo") or {}).get("title"),
                      (product.get("vendor") or "").strip(), gender, age, color, material(desc))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to Shopify (default: dry run)")
    ap.add_argument("--overwrite", action="store_true", help="also replace existing alt texts")
    args = ap.parse_args()
    store, token = os.environ.get("SHOPIFY_STORE"), os.environ.get("SHOPIFY_TOKEN")
    if not store or not token:
        print("SHOPIFY_STORE and SHOPIFY_TOKEN must be set", file=sys.stderr)
        return 2

    pending: list[dict] = []
    stats = {"products": 0, "images": 0, "already_set": 0, "to_update": 0, "updated": 0, "errors": 0}
    samples = 0

    def flush() -> None:
        nonlocal pending
        if args.apply and pending:
            res = gql(store, token, MUTATION, {"files": pending})["fileUpdate"]
            if res["userErrors"]:
                stats["errors"] += len(res["userErrors"])
                print("  errors:", res["userErrors"][:3])
            stats["updated"] += len(res["files"] or [])
            time.sleep(0.5)
        pending = []

    cursor = None
    while True:
        block = gql(store, token, QUERY, {"cursor": cursor})["products"]
        for p in block["nodes"]:
            stats["products"] += 1
            images = [m for m in p["media"]["nodes"] if m["mediaContentType"] == "IMAGE"]
            if not images:
                continue
            base = alt_for(p)
            for i, m in enumerate(images, start=1):
                stats["images"] += 1
                if (m.get("alt") or "").strip() and not args.overwrite:
                    stats["already_set"] += 1
                    continue
                alt = base if i == 1 else f"{base} – view {i}"
                stats["to_update"] += 1
                if samples < 25:
                    print(f"  {p['title']!r:60.60} -> {alt}")
                    samples += 1
                pending.append({"id": m["id"], "alt": alt[:512]})
                if len(pending) >= 50:
                    flush()
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(0.4)
    flush()

    print("--- Summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if not args.apply:
        print("(dry run — nothing written; rerun with --apply)")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
