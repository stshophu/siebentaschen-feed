#!/usr/bin/env python3
"""
Black Friday price check (German PAngV §11: a price reduction must be measured
against the lowest price of the previous 30 days).

For every product in the "black-friday" collection, compare the current price
of each variant with its lowest price in the price history kept in this repo
(siebentaschen_product_feed.csv, one commit per feed run). A product is "not
clean" when any variant is now more expensive than its 30-day low (tolerance
1 %), or when no price history exists for it.

Not-clean products get the tag `bf-exclude`, and the collection gets the rule
"tag is not equal to bf-exclude" — so the Black Friday discount and the theme
label (both driven by the collection) no longer apply to them.

Without --apply nothing is written (dry run: counts + examples).

Usage:
    python bf_price_check.py [--apply] [--start 2026-11-16] [--days 30]
Environment:
    SHOPIFY_STORE, SHOPIFY_TOKEN (write_products for --apply)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import subprocess
import sys
import time
from collections import defaultdict

import requests

API_VERSION = "2026-10"
COLLECTION_ID = "gid://shopify/Collection/734094295380"  # "black-friday"
TAG = "bf-exclude"
CSV_PATH = "siebentaschen_product_feed.csv"
TOLERANCE = 1.01


def gql(store, token, query, variables=None):
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for _ in range(10):
        r = requests.post(url, json={"query": query, "variables": variables or {}}, headers=headers, timeout=90)
        if r.status_code in (429, 502, 503, 504):
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


COLLECTION_Q = """
query C($id: ID!, $cursor: String) {
  collection(id: $id) {
    id title
    ruleSet { appliedDisjunctively rules { column relation condition } }
    products(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { id handle title tags variants(first: 100) { nodes { title price } } }
    }
  }
}
"""

COLLECTION_UPDATE = """
mutation U($input: CollectionInput!) {
  collectionUpdate(input: $input) { collection { id } userErrors { field message } }
}
"""

TAGS_ADD = """
mutation T($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
}
"""


def price_history(since: str):
    """(handle, variant title) -> list of (iso date, price), from git history of the CSV."""
    log = subprocess.run(["git", "log", f"--since={since}", "--format=%H %cI", "--", CSV_PATH],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    hist = defaultdict(list)
    commits = [l.split() for l in log if l.strip()]
    for sha, date in commits:
        data = subprocess.run(["git", "show", f"{sha}:{CSV_PATH}"], capture_output=True, text=True).stdout
        for row in csv.DictReader(io.StringIO(data)):
            try:
                hist[(row["Handle"], row["Variant"])].append((date, float(row["Preis (Brutto)"])))
            except (KeyError, ValueError):
                continue
    return hist, len(commits), (commits[-1][1] if commits else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--start", default="2026-11-16", help="day the Black Friday discount started")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    store, token = os.environ.get("SHOPIFY_STORE"), os.environ.get("SHOPIFY_TOKEN")
    if not store or not token:
        print("SHOPIFY_STORE and SHOPIFY_TOKEN must be set", file=sys.stderr)
        return 2

    start = dt.date.fromisoformat(args.start)
    window_from = (start - dt.timedelta(days=args.days)).isoformat()
    hist, n_commits, oldest = price_history(window_from)
    print(f"Price history: {n_commits} feed snapshots since {window_from} (oldest {oldest})")

    products, collection, cursor = [], None, None
    while True:
        c = gql(store, token, COLLECTION_Q, {"id": COLLECTION_ID, "cursor": cursor})["collection"]
        if c is None:
            print("Collection not visible to this token/API version", file=sys.stderr)
            return 2
        collection = collection or c
        products += c["products"]["nodes"]
        if not c["products"]["pageInfo"]["hasNextPage"]:
            break
        cursor = c["products"]["pageInfo"]["endCursor"]
    print(f"Collection '{collection['title']}': {len(products)} products")
    print("Current rules:", collection["ruleSet"])

    clean, bad, nohist, examples = [], [], [], []
    for p in products:
        worst = None
        has_hist = False
        for v in p["variants"]["nodes"]:
            series = hist.get((p["handle"], v["title"]))
            if not series:
                continue
            has_hist = True
            low = min(price for _, price in series)
            now = float(v["price"])
            if now > low * TOLERANCE and (worst is None or now / low > worst[2]):
                worst = (v["title"], low, now / low, now)
        if not has_hist:
            nohist.append(p)
        elif worst:
            bad.append(p)
            if len(examples) < 15:
                examples.append(f"  {p['title'][:55]:55} 30-day low {worst[1]:>8.2f}  now {worst[3]:>8.2f}  (+{(worst[2]-1)*100:.0f}%)")
        else:
            clean.append(p)

    print(f"\nClean (price not above 30-day low): {len(clean)}")
    print(f"Not clean (price went up):          {len(bad)}")
    print(f"No price history (excluded too):    {len(nohist)}")
    print("\nExamples of products that went up:")
    print("\n".join(examples))
    print("\nClean products (first 40):")
    for p in clean[:40]:
        print("  ", p["title"][:80])

    exclude = [p for p in bad + nohist if TAG not in p["tags"]]
    if not args.apply:
        print(f"\n(dry run — would tag {len(exclude)} products '{TAG}' and add the rule TAG NOT_EQUALS {TAG})")
        return 0

    rules = (collection["ruleSet"] or {}).get("rules")
    if rules is None:
        print("Collection uses the new conditions format — add the exclusion "
              f"'tagged with {TAG}' to its conditions separately.")
    elif not any(r["column"] == "TAG" and r["relation"] == "NOT_EQUALS" and r["condition"] == TAG for r in rules):
        if collection["ruleSet"]["appliedDisjunctively"]:
            print("Collection uses ANY-rules; an exclusion rule would not work. Stopping.", file=sys.stderr)
            return 2
        new_rules = rules + [{"column": "TAG", "relation": "NOT_EQUALS", "condition": TAG}]
        res = gql(store, token, COLLECTION_UPDATE, {"input": {"id": collection["id"], "ruleSet": {
            "appliedDisjunctively": False, "rules": new_rules}}})["collectionUpdate"]
        if res["userErrors"]:
            print("Rule update failed:", res["userErrors"], file=sys.stderr)
            return 1
        print("Added rule: TAG NOT_EQUALS", TAG)

    errors = 0
    for i, p in enumerate(exclude, 1):
        res = gql(store, token, TAGS_ADD, {"id": p["id"], "tags": [TAG]})["tagsAdd"]
        if res["userErrors"]:
            errors += 1
        if i % 100 == 0:
            print(f"  tagged {i}/{len(exclude)}")
        time.sleep(0.15)
    print(f"Tagged {len(exclude) - errors} products '{TAG}' ({errors} errors)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
