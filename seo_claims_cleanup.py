#!/usr/bin/env python3
"""
Remove authenticity claims ("authentic", "guaranteed authenticity", "Authentische
Luxusmode", "garantierte Echtheit", 正規品 …) from product SEO descriptions in
every shop language. Google treated this kind of wording as a misrepresentation
risk.

Touches only the SEO description (meta_description) of products: the shop's
primary language (English) via productUpdate, and the translations (de, fr, it,
ja, zh-TW …) via translationsRegister. Product descriptions (body_html) are
only counted, never changed.

With --fix-brand it also repairs the brand suffix of SEO titles, where machine
translation turned "| Siebentaschen" into "Sieben Taschen", "Siebte Tasche",
"Siebenschenkel", "ジーベントaschen" …

Without --apply nothing is written (dry run: samples + counts).

Usage:
    python seo_claims_cleanup.py                       # dry run
    python seo_claims_cleanup.py --apply               # write
    python seo_claims_cleanup.py --apply --fix-brand   # also fix title suffixes
Environment:
    SHOPIFY_STORE, SHOPIFY_TOKEN  (needs write_products and write_translations)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter

import requests

API_VERSION = "2025-07"
BRAND = "Siebentaschen"

LIST_QUERY = """
query R($cursor: String) {
  translatableResources(resourceType: PRODUCT, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      resourceId
      translatableContent { key value digest locale }
      translations(locale: "de") { key value locale }
    }
  }
}
"""

ONE_QUERY = """
query One($id: ID!) {
  translatableResource(resourceId: $id) { translatableContent { key value digest } }
}
"""

SCOPES_QUERY = "{ currentAppInstallation { accessScopes { handle } } }"
LOCALES_QUERY = "{ shopLocales { locale primary published } }"
DEFAULT_PRIMARY, DEFAULT_LOCALES = "en", ["de", "fr", "it", "ja", "zh-TW"]

PRODUCT_UPDATE = """
mutation U($p: ProductUpdateInput!) {
  productUpdate(product: $p) { product { id } userErrors { field message } }
}
"""

REGISTER = """
mutation T($id: ID!, $t: [TranslationInput!]!) {
  translationsRegister(resourceId: $id, translations: $t) { userErrors { field message code } }
}
"""

# --------------------------------------------------------------------------
# Cleaning rules, per language. Order matters: whole phrases first, then the
# single adjective.
# --------------------------------------------------------------------------
RULES: dict[str, list[tuple[str, str]]] = {
    "en": [
        (r"\s*(?:&|and|,)\s*(?:guaranteed\s+authenticity|authenticity\s+guaranteed)", ""),
        (r"(?:guaranteed\s+authenticity|authenticity\s+guaranteed)\s*[.,]?", ""),
        (r"\b100\s?%\s*authentic\b\s*", ""),
        (r"\b[Aa]uthentic\s+", ""),
        (r"\s+authentic\b", ""),
    ],
    "de": [
        (r"\s*(?:&|und|,)\s*garantierter?\s+(?:Echtheit|Authentizität|Originalität)", ""),
        (r"\s*(?:&|und|,)\s*(?:Echtheit|Authentizität)sgarantie", ""),
        (r"garantierter?\s+(?:Echtheit|Authentizität|Originalität)\s*[.,]?", ""),
        (r"\b100\s?%\s*(?:authentisch|echt|original)\b\s*", ""),
        (r"\b[Aa]uthentisch(?:e|en|er|es|em)?\s+", ""),
        (r"\s+authentisch\b", ""),
    ],
    "fr": [
        (r"\s*(?:&|et|,)\s*(?:authenticité\s+garantie|garantie\s+d['’]authenticité)", ""),
        (r"(?:authenticité\s+garantie|garantie\s+d['’]authenticité)\s*[.,]?", ""),
        (r"\b[Aa]uthentiques?\s+", ""),
        (r"\s+authentiques?\b", ""),
    ],
    "it": [
        (r"\s*(?:&|e|,)\s*(?:autenticità\s+garantita|garanzia\s+di\s+autenticità)", ""),
        (r"(?:autenticità\s+garantita|garanzia\s+di\s+autenticità)\s*[.,]?", ""),
        (r"\b[Aa]utentic[oaie]\s+", ""),
        (r"\s+autentic[oaie]\b", ""),
    ],
    "es": [
        (r"\s*(?:&|y|,)\s*autenticidad\s+garantizada", ""),
        (r"autenticidad\s+garantizada\s*[.,]?", ""),
        (r"\b[Aa]ut[ée]ntic[oa]s?\s+", ""),
        (r"\s+aut[ée]ntic[oa]s?\b", ""),
    ],
    "ja": [
        (r"\s*[＆&と、]\s*(?:正規品|本物)保証", ""),
        (r"(?:正規品|本物)保証", ""),
        (r"(?:正規品|本物)の", ""),
        (r"正規品", ""),
    ],
    "zh": [
        (r"\s*[與及和、&＆]\s*(?:保證正品|正品保證)", ""),
        (r"(?:保證正品|正品保證)", ""),
        (r"正品", ""),
    ],
}

DETECT = re.compile(
    r"authenti|authentisch|authentizität|echtheit|authentique|authenticité|autentic|"
    r"auténtic|autenticidad|正規品|本物|正品",
    re.I,
)


def lang_of(locale: str) -> str:
    return locale.split("-")[0].lower()


def tidy(text: str) -> str:
    t = re.sub(r"[ \t]{2,}", " ", text)
    t = re.sub(r"\s+([.,;:!?。、])", r"\1", t)
    t = re.sub(r"\s*[—–-]\s*([.。])", r"\1", t)          # "— ." left behind
    t = re.sub(r"\s*[—–-]\s*$", "", t)                   # dangling dash at end
    t = re.sub(r"(?<!\.)\.\s+\.(?!\.)", ".", t)
    # Capitalise the first letter of each sentence (Latin scripts only).
    t = re.sub(r"(^|[.!?]\s+)([a-zàâäéèêëîïôöùûüç])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t.strip()


def clean(text: str | None, locale: str) -> str | None:
    if not text:
        return text
    lang = lang_of(locale)
    rules = RULES.get(lang)
    if not rules or not DETECT.search(text):
        return text
    out = text
    for pat, rep in rules:
        out = re.sub(pat, rep, out)
    # Truncated descriptions ("… Kaufen Sie authenti...") — drop the cut-off
    # last sentence if it still carries a claim fragment.
    if DETECT.search(out) and re.search(r"(\.\.\.|…)\s*$", out):
        cut = max(out.rstrip(".… ").rfind(". "), out.rfind("。"))
        if cut > 0:
            out = out[: cut + 1]
    return tidy(out) if out != text else text


BRAND_SUFFIX = re.compile(r"\s*\|\s*([^|]{3,40})$")


def fix_brand(title: str | None) -> str | None:
    if not title:
        return title
    m = BRAND_SUFFIX.search(title)
    if not m:
        return title
    suffix = m.group(1).strip()
    if suffix == BRAND:
        return title
    looks_like_brand = re.search(r"sieb|taschen|tasche|aschen|ジーベン|ズィーベン", suffix, re.I)
    if not looks_like_brand:
        return title
    return title[: m.start()] + f" | {BRAND}"


# --------------------------------------------------------------------------


def gql(store: str, token: str, query: str, variables: dict | None = None) -> dict:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for _ in range(10):
        r = requests.post(url, json={"query": query, "variables": variables or {}},
                          headers=headers, timeout=90)
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


def translations_query(locales: list[str]) -> str:
    parts = " ".join(
        f'l{i}: translations(locale: "{loc}") {{ key value locale }}' for i, loc in enumerate(locales)
    )
    return LIST_QUERY.replace('translations(locale: "de") { key value locale }', parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to Shopify (default: dry run)")
    ap.add_argument("--fix-brand", action="store_true", help="also fix '| Siebentaschen' in SEO titles")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products (testing)")
    args = ap.parse_args()
    store, token = os.environ.get("SHOPIFY_STORE"), os.environ.get("SHOPIFY_TOKEN")
    if not store or not token:
        print("SHOPIFY_STORE and SHOPIFY_TOKEN must be set", file=sys.stderr)
        return 2

    try:
        meta = gql(store, token, LOCALES_QUERY)
        locales = [l["locale"] for l in meta["shopLocales"] if not l["primary"]]
        primary = next(l["locale"] for l in meta["shopLocales"] if l["primary"])
    except RuntimeError:
        primary, locales = DEFAULT_PRIMARY, DEFAULT_LOCALES
    scopes = {s["handle"] for s in gql(store, token, SCOPES_QUERY)["currentAppInstallation"]["accessScopes"]}
    print("Scopes:", ", ".join(sorted(scopes)))
    can_products = "write_products" in scopes
    can_translations = "write_translations" in scopes
    print(f"Primary locale: {primary}; translations: {', '.join(locales)}")
    print(f"Token scopes: write_products={can_products} write_translations={can_translations}")
    if args.apply and not can_products:
        print("Token lacks write_products — cannot apply.", file=sys.stderr)
        return 2
    if args.apply and not can_translations:
        print("WARNING: token lacks write_translations — translations will be skipped.")

    query = translations_query(locales)
    keys = ["meta_description"] + (["meta_title"] if args.fix_brand else [])

    stats: Counter = Counter()
    samples: Counter = Counter()
    errors: list = []

    cursor = None
    while True:
        block = gql(store, token, query, {"cursor": cursor})["translatableResources"]
        for node in block["nodes"]:
            stats["products"] += 1
            rid = node["resourceId"]
            src = {c["key"]: c for c in node["translatableContent"]}
            trans = {loc: {t["key"]: t["value"] for t in node[f"l{i}"]} for i, loc in enumerate(locales)}

            for loc in [primary] + locales:
                body = src.get("body_html", {}).get("value") if loc == primary else trans[loc].get("body_html")
                if body and DETECT.search(body):
                    stats[f"body_html_with_claim[{loc}]"] += 1

            # --- primary language ---
            new_primary: dict[str, str] = {}
            for key in keys:
                old = (src.get(key) or {}).get("value")
                new = clean(old, primary) if key == "meta_description" else fix_brand(old)
                if old and new != old:
                    new_primary[key] = new
                    stats[f"{key}[{primary}]"] += 1
                    if samples[(key, primary)] < 6:
                        print(f"\n[{primary} {key}] {rid}\n  - {old}\n  + {new}")
                        samples[(key, primary)] += 1

            # --- translations ---
            new_trans: list[dict] = []
            for loc in locales:
                for key in keys:
                    old = trans[loc].get(key)
                    new = clean(old, loc) if key == "meta_description" else fix_brand(old)
                    if old and new != old:
                        new_trans.append({"locale": loc, "key": key, "value": new})
                        stats[f"{key}[{loc}]"] += 1
                        if samples[(key, loc)] < 4:
                            print(f"\n[{loc} {key}] {rid}\n  - {old}\n  + {new}")
                            samples[(key, loc)] += 1
                    elif old and key == "meta_description" and DETECT.search(old) and lang_of(loc) not in RULES:
                        stats[f"unhandled_language[{loc}]"] += 1

            if not (new_primary or new_trans):
                continue
            stats["products_changed"] += 1
            if not args.apply:
                continue

            try:
                if new_primary:
                    seo = {}
                    if "meta_description" in new_primary:
                        seo["description"] = new_primary["meta_description"]
                    if "meta_title" in new_primary:
                        seo["title"] = new_primary["meta_title"]
                    res = gql(store, token, PRODUCT_UPDATE, {"p": {"id": rid, "seo": seo}})["productUpdate"]
                    if res["userErrors"]:
                        errors.append((rid, res["userErrors"]))
                        continue
                    stats["primary_written"] += 1
                    # Source changed -> fresh digests; re-register every existing
                    # translation of that key so none is left "outdated".
                    fresh = gql(store, token, ONE_QUERY, {"id": rid})["translatableResource"]
                    src = {c["key"]: c for c in fresh["translatableContent"]}
                    pending = {(t["locale"], t["key"]) for t in new_trans}
                    for loc in locales:
                        for key in new_primary:
                            if (loc, key) not in pending and trans[loc].get(key):
                                new_trans.append({"locale": loc, "key": key, "value": trans[loc][key]})

                if new_trans and can_translations:
                    payload = []
                    for t in new_trans:
                        digest = (src.get(t["key"]) or {}).get("digest")
                        if not digest:
                            stats["skipped_no_source_digest"] += 1
                            continue
                        payload.append({**t, "translatableContentDigest": digest})
                    if payload:
                        res = gql(store, token, REGISTER, {"id": rid, "t": payload})["translationsRegister"]
                        if res["userErrors"]:
                            errors.append((rid, res["userErrors"]))
                        else:
                            stats["translations_written"] += len(payload)
                time.sleep(0.25)
            except Exception as exc:  # keep going; report at the end
                errors.append((rid, str(exc)[:300]))

            if args.limit and stats["products_changed"] >= args.limit:
                break
        if args.limit and stats["products_changed"] >= args.limit:
            break
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(0.3)

    print("\n--- Summary ---")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if errors:
        print(f"\n{len(errors)} errors, first ones:")
        for e in errors[:10]:
            print("  ", e)
    if not args.apply:
        print("\n(dry run — nothing written; rerun with --apply)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
