#!/usr/bin/env python3
"""
Glami.pl product feed generator for Siebentaschen.

Pulls active, in-stock products from Shopify and emits a GLAMI-compliant XML
feed (one SHOPITEM per variant), ready to be served over HTTP for Glami to
poll hourly.

Spec: https://www.glami.pl/info/feed/

What this fixes versus the previous app-generated feed:
  * PRICE_VAT converted EUR -> PLN at the live ECB rate. The old feed sent
    bare EUR numbers, which Glami.pl reads as PLN — listing everything at
    roughly a quarter of its real price.
  * CATEGORYTEXT is a real Polish Glami path, verified against their live
    category XML, instead of English strings like "Glami.pl | Women Dresses".
  * size_system emitted alongside every size (was absent entirely).
  * EAN only when it's a genuine 8/13/14-digit GTIN (the old feed sent SKUs).
  * URL points at siebentaschen.com, not the raw myshopify domain.
  * Descriptions stripped of supplier HTML, no empty PARAM blocks,
    colours translated to Polish.

Usage:
    python glami_feed.py --out glami_feed.xml
    python glami_feed.py --audit      # report gaps, write nothing

Environment:
    SHOPIFY_STORE     e.g. modalist-de.myshopify.com
    SHOPIFY_TOKEN     Admin API token (read_products, read_inventory)
    GLAMI_FX_EUR_PLN  optional float override; otherwise fetched from the ECB
    GLAMI_CPC         optional default CPC bid, e.g. "0.5"
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterator
from xml.etree import ElementTree as ET

import requests

API_VERSION = "2025-07"
ECB_RATES = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
STOREFRONT = "https://siebentaschen.com"
FX_EUR_PLN_FALLBACK = Decimal("4.30")

# ---------------------------------------------------------------------------
# Category mapping — Shopify product_type -> GLAMI CATEGORYTEXT
# ---------------------------------------------------------------------------
# Every path below was verified against https://www.glami.pl/kategoria-xml/
# on 2026-08-16. Nothing here is guessed. If Glami reshuffles their taxonomy,
# re-verify rather than editing by feel; --audit reports unmapped types.
GLAMI_CATEGORY = {
    'Men Ankle Boots': 'Glami.pl | Moda męska | Buty męskie | Botki męskie',
    'Men Backpacks': 'Glami.pl | Moda męska | Akcesoria męskie | Plecaki męskie',
    'Men Belts': 'Glami.pl | Moda męska | Akcesoria męskie | Paski męskie',
    'Men Blazers': 'Glami.pl | Moda męska | Odzież męska | Garnitury męskie | Marynarki męskie',
    'Men Boots': 'Glami.pl | Moda męska | Buty męskie | Botki męskie',
    'Men Card Holders': 'Glami.pl | Moda męska | Akcesoria męskie | Portfele męskie | Etui na karty męskie',
    'Men Tops': 'Glami.pl | Moda męska | Odzież męska | T-shirty i koszulki męskie | Koszulki męskie',
    'Men Clutches': 'Glami.pl | Moda męska | Akcesoria męskie | Torby męskie | Torebki męskie',
    'Men Coats': 'Glami.pl | Moda męska | Odzież męska | Kurtki i płaszcze męskie | Płaszcze męskie',
    'Men Down Jackets': 'Glami.pl | Moda męska | Odzież męska | Kurtki i płaszcze męskie | Kurtki męskie | Kurtki puchowe męskie',
    'Men Espadrilles': 'Glami.pl | Moda męska | Buty męskie | Espadryle męskie',
    'Men Flats': 'Glami.pl | Moda męska | Buty męskie | Półbuty męskie',
    'Men Gloves': 'Glami.pl | Moda męska | Akcesoria męskie | Rękawiczki męskie',
    'Men Handbags': 'Glami.pl | Moda męska | Akcesoria męskie | Torby męskie | Torebki męskie',
    'Men Hats': 'Glami.pl | Moda męska | Akcesoria męskie | Nakrycia głowy męskie | Czapki męskie',
    'Men Heeled Sandals': 'Glami.pl | Moda męska | Buty męskie | Sandały męskie',
    'Men Jackets': 'Glami.pl | Moda męska | Odzież męska | Kurtki i płaszcze męskie | Kurtki męskie',
    'Men Jeans': 'Glami.pl | Moda męska | Odzież męska | Spodnie męskie | Jeansy męskie',
    'Men Key Holder': 'Glami.pl | Moda męska | Akcesoria męskie | Breloki męskie',
    'Men Knitwear': 'Glami.pl | Moda męska | Odzież męska | Swetry męskie',
    'Men Lace-ups': 'Glami.pl | Moda męska | Buty męskie | Półbuty męskie | Buty derby męskie',
    'Men Loafers': 'Glami.pl | Moda męska | Buty męskie | Mokasyny męskie',
    'Men Polo Shirts': 'Glami.pl | Moda męska | Odzież męska | T-shirty i koszulki męskie | Koszulki polo męskie',
    'Men Pouches': 'Glami.pl | Moda męska | Akcesoria męskie | Torby męskie | Saszetki męskie',
    'Men Sandals': 'Glami.pl | Moda męska | Buty męskie | Sandały męskie',
    'Men Scarves': 'Glami.pl | Moda męska | Akcesoria męskie | Szaliki i chusty męskie | Szaliki męskie',
    'Men Shirts': 'Glami.pl | Moda męska | Odzież męska | Koszule męskie',
    'Men Shorts': 'Glami.pl | Moda męska | Odzież męska | Szorty męskie',
    'Men Shoulder Bags': 'Glami.pl | Moda męska | Akcesoria męskie | Torby męskie',
    'Men Slippers': 'Glami.pl | Moda męska | Buty męskie | Kapcie męskie',
    'Men Sneakers': 'Glami.pl | Moda męska | Buty męskie | Sneakersy męskie',
    'Men Socks': 'Glami.pl | Moda męska | Odzież męska | Bielizna męska | Skarpety męskie',
    'Men Suits': 'Glami.pl | Moda męska | Odzież męska | Garnitury męskie',
    'Men Sunglasses': 'Glami.pl | Moda męska | Akcesoria męskie | Okulary męskie | Okulary przeciwsłoneczne męskie',
    'Men Sweatshirts': 'Glami.pl | Moda męska | Odzież męska | Bluzy męskie',
    'Men Swimwear': 'Glami.pl | Moda męska | Odzież męska | Kąpielówki męskie',
    'Men T-Shirts': 'Glami.pl | Moda męska | Odzież męska | T-shirty i koszulki męskie | Koszulki męskie',
    'Men Ties': 'Glami.pl | Moda męska | Akcesoria męskie | Krawaty męskie',
    'Men Tracksuits': 'Glami.pl | Moda męska | Odzież męska | Dresy męskie',
    'Men Travel Bags': 'Glami.pl | Moda męska | Akcesoria męskie | Walizki i torby podróżne męskie | Torby podróżne męskie',
    'Men Trousers': 'Glami.pl | Moda męska | Odzież męska | Spodnie męskie',
    'Men Underwear': 'Glami.pl | Moda męska | Odzież męska | Bielizna męska',
    'Men Vests': 'Glami.pl | Moda męska | Odzież męska | Kamizelki męskie',
    'Men Wallets': 'Glami.pl | Moda męska | Akcesoria męskie | Portfele męskie',
    'Women Ankle Boots': 'Glami.pl | Moda damska | Buty damskie | Botki i kozaki damskie | Botki damskie',
    'Women Backpacks': 'Glami.pl | Moda damska | Akcesoria damskie | Plecaki damskie',
    'Women Belts': 'Glami.pl | Moda damska | Akcesoria damskie | Paski damskie',
    'Women Blazers': 'Glami.pl | Moda damska | Odzież damska | Marynarki i blezery damskie | Marynarki damskie',
    'Women Boots': 'Glami.pl | Moda damska | Buty damskie | Botki i kozaki damskie | Kozaki damskie',
    'Women Card Holders': 'Glami.pl | Moda damska | Akcesoria damskie | Portfele damskie | Etui na karty damskie',
    'Women Clutches': 'Glami.pl | Moda damska | Akcesoria damskie | Torby i torebki damskie | Torebki damskie | Kopertówki damskie',
    'Women Coats': 'Glami.pl | Moda damska | Odzież damska | Kurtki i płaszcze damskie | Płaszcze damskie',
    'Women Down Jackets': 'Glami.pl | Moda damska | Odzież damska | Kurtki i płaszcze damskie | Kurtki damskie | Kurtki puchowe damskie',
    'Women Dresses': 'Glami.pl | Moda damska | Odzież damska | Sukienki damskie',
    'Women Espadrilles': 'Glami.pl | Moda damska | Buty damskie | Espadryle damskie',
    'Women Flats': 'Glami.pl | Moda damska | Buty damskie | Baleriny damskie',
    'Women Gloves': 'Glami.pl | Moda damska | Akcesoria damskie | Rękawiczki damskie',
    'Women Hair Accessories': 'Glami.pl | Moda damska | Akcesoria damskie | Akcesoria do włosów damskie | Ozdoby do włosów damskie',
    'Women Handbags': 'Glami.pl | Moda damska | Akcesoria damskie | Torby i torebki damskie | Torebki damskie',
    'Women Hats': 'Glami.pl | Moda damska | Akcesoria damskie | Nakrycia głowy damskie | Czapki damskie',
    'Women Heeled Sandals': 'Glami.pl | Moda damska | Buty damskie | Sandały damskie',
    'Women Jackets': 'Glami.pl | Moda damska | Odzież damska | Kurtki i płaszcze damskie | Kurtki damskie',
    'Women Jeans': 'Glami.pl | Moda damska | Odzież damska | Spodnie damskie | Jeansy damskie',
    'Women Jewellery': 'Glami.pl | Moda damska | Biżuteria i zegarki damskie',
    'Women Jumpsuits': 'Glami.pl | Moda damska | Odzież damska | Kombinezony damskie',
    'Women Key Holder': 'Glami.pl | Moda damska | Akcesoria damskie | Breloki damskie',
    'Women Knitwear': 'Glami.pl | Moda damska | Odzież damska | Swetry damskie',
    'Women Lace-ups': 'Glami.pl | Moda damska | Buty damskie | Półbuty damskie | Buty derby damskie',
    'Women Loafers': 'Glami.pl | Moda damska | Buty damskie | Mokasyny damskie',
    'Women Mules': 'Glami.pl | Moda damska | Buty damskie | Mule damskie',
    'Women Polo Shirts': 'Glami.pl | Moda damska | Odzież damska | Topy i koszulki damskie | Koszulki polo damskie',
    'Women Pouches': 'Glami.pl | Moda damska | Akcesoria damskie | Torby i torebki damskie | Nerki damskie',
    'Women Pumps': 'Glami.pl | Moda damska | Buty damskie | Buty na obcasie damskie',
    'Women Sandals': 'Glami.pl | Moda damska | Buty damskie | Sandały damskie',
    'Women Scarves': 'Glami.pl | Moda damska | Akcesoria damskie | Szaliki i chusty damskie | Szaliki damskie',
    'Women Shirts': 'Glami.pl | Moda damska | Odzież damska | Bluzki i koszule damskie | Koszule damskie',
    'Women Shorts': 'Glami.pl | Moda damska | Odzież damska | Szorty damskie',
    'Women Shoulder Bags': 'Glami.pl | Moda damska | Akcesoria damskie | Torby i torebki damskie | Torby na ramię damskie',
    'Women Skirts': 'Glami.pl | Moda damska | Odzież damska | Spódnice damskie',
    'Women Slippers': 'Glami.pl | Moda damska | Buty damskie | Kapcie damskie',
    'Women Sneakers': 'Glami.pl | Moda damska | Buty damskie | Sneakersy damskie',
    'Women Socks': 'Glami.pl | Moda damska | Odzież damska | Bielizna damska | Skarpetki damskie',
    'Women Suits': 'Glami.pl | Moda damska | Odzież damska | Garsonki i garnitury damskie | Garnitury damskie',
    'Women Sunglasses': 'Glami.pl | Moda damska | Akcesoria damskie | Okulary damskie | Okulary przeciwsłoneczne damskie',
    'Women Sweatshirts': 'Glami.pl | Moda damska | Odzież damska | Bluzy damskie',
    'Women Swimwear': 'Glami.pl | Moda damska | Odzież damska | Stroje kąpielowe damskie',
    'Women T-Shirts': 'Glami.pl | Moda damska | Odzież damska | Topy i koszulki damskie | Koszulki damskie',
    'Women Tops': 'Glami.pl | Moda damska | Odzież damska | Topy i koszulki damskie | Topy damskie',
    'Women Tracksuits': 'Glami.pl | Moda damska | Odzież damska | Dresy damskie',
    'Women Travel Bags': 'Glami.pl | Moda damska | Akcesoria damskie | Walizki i torby podróżne damskie | Torby podróżne damskie',
    'Women Trousers': 'Glami.pl | Moda damska | Odzież damska | Spodnie damskie',
    'Women Underwear': 'Glami.pl | Moda damska | Odzież damska | Bielizna damska',
    'Women Vests': 'Glami.pl | Moda damska | Odzież damska | Kamizelki damskie',
    'Women Wallets': 'Glami.pl | Moda damska | Akcesoria damskie | Portfele damskie',}

# Product types with no Glami equivalent. Glami is fashion-only; forcing these
# into an unrelated category would be a misrepresentation, so they're dropped.
EXCLUDED_TYPES = {
    "Women Blankets",
    "Women Collar",
    "Women Tech Accessories",
    "Women Clothing",  # still generic — fix the product, don't guess here
    "Men Blankets", "Men Tech Accessories", "Men Collar",
}

# Small accessories where a few CPC clicks can outweigh the margin. Set
# ACCESSORY_MIN_PRICE_EUR (env) to skip these below a euro threshold; leave
# it unset to include everything. Applies to accessories ONLY — a cheap
# T-shirt still goes in the feed.
ACCESSORY_TYPES = {
    "Belts", "Scarves", "Gloves", "Wallets", "Socks", "Hats", "Ties",
    "Key Holder", "Card Holders", "Hair Accessories", "Pouches",
}

# Glami exempts these from the size requirement.
SIZE_EXEMPT_WORDS = {
    "Sunglasses", "Scarves", "Handbags", "Shoulder Bags", "Travel Bags",
    "Backpacks", "Wallets", "Jewellery", "Clutches", "Pouches",
    "Card Holders", "Key Holder", "Hair Accessories",
}
SHOE_WORDS = {
    "Sneakers", "Boots", "Ankle Boots", "Sandals", "Heeled Sandals",
    "Loafers", "Flats", "Slippers", "Espadrilles", "Mules", "Pumps", "Lace-ups",
}
LETTER_SIZES = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL", "5XL",
                "XS/S", "S/M", "M/L", "L/XL"}

# Suppliers send a mix of Italian and English colour names; Glami.pl wants Polish.
COLOR_PL = {
    "black": "czarny", "nero": "czarny", "white": "biały", "bianco": "biały",
    "grey": "szary", "gray": "szary", "grigio": "szary",
    "blue": "niebieski", "blu": "niebieski", "azzurro": "błękitny",
    "navy": "granatowy", "red": "czerwony", "rosso": "czerwony",
    "green": "zielony", "verde": "zielony", "pink": "różowy", "rosa": "różowy",
    "fuchsia": "fuksja", "beige": "beżowy", "brown": "brązowy",
    "marrone": "brązowy", "cognac": "koniakowy", "yellow": "żółty",
    "giallo": "żółty", "orange": "pomarańczowy", "arancione": "pomarańczowy",
    "purple": "fioletowy", "viola": "fioletowy", "lilac": "liliowy",
    "gold": "złoty", "oro": "złoty", "silver": "srebrny", "argento": "srebrny",
    "cream": "kremowy", "panna": "kremowy", "camel": "camel",
    "multicolour": "wielokolorowy", "multicolor": "wielokolorowy",
}

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
# Different sources write different words for the same thing: the CSV feed says
# "Knitwear", a supplier sync writes "Sweater", another writes "Sweaters".
# Everything here resolves to the vocabulary used in GLAMI_CATEGORY above.
TYPE_ALIASES = {
    "jacket": "Jackets", "jackets": "Jackets",
    "coat": "Coats", "coats": "Coats",
    "parka": "Coats", "parkas": "Coats",
    "down jacket": "Down Jackets",
    "sweater": "Knitwear", "sweaters": "Knitwear", "knitwear": "Knitwear",
    "cardigan": "Knitwear", "cardigans": "Knitwear",
    "pullover": "Knitwear", "jumper": "Knitwear",
    "sweatshirt": "Sweatshirts", "sweatshirts": "Sweatshirts",
    "hoodie": "Sweatshirts", "hoodies": "Sweatshirts",
    "t-shirt": "T-Shirts", "t-shirts": "T-Shirts", "tshirt": "T-Shirts",
    "tee": "T-Shirts", "top": "Tops", "tops": "Tops",
    "polo": "Polo Shirts", "polo shirt": "Polo Shirts", "polo shirts": "Polo Shirts",
    "shirt": "Shirts", "shirts": "Shirts",
    "blouse": "Shirts", "blouses": "Shirts",
    "pants": "Trousers", "pant": "Trousers", "trouser": "Trousers",
    "trousers": "Trousers", "sweatpants": "Tracksuits",
    "jean": "Jeans", "jeans": "Jeans", "denim": "Jeans",
    "short": "Shorts", "shorts": "Shorts",
    "skirt": "Skirts", "skirts": "Skirts",
    "dress": "Dresses", "dresses": "Dresses",
    "jumpsuit": "Jumpsuits", "jumpsuits": "Jumpsuits",
    "blazer": "Blazers", "blazers": "Blazers",
    "formal jacket": "Blazers", "formal jackets": "Blazers",
    "suit": "Suits", "suits": "Suits",
    "vest": "Vests", "vests": "Vests",
    "tracksuit": "Tracksuits", "tracksuits": "Tracksuits",
    "swimwear": "Swimwear", "bikini": "Swimwear", "bikinis": "Swimwear",
    "underwear": "Underwear", "bra": "Underwear", "bras": "Underwear",
    "briefs": "Underwear", "socks": "Socks", "sock": "Socks",
    # shoes
    "shoe": "Sneakers", "shoes": "Sneakers",
    "sneaker": "Sneakers", "sneakers": "Sneakers",
    "boot": "Boots", "boots": "Boots",
    "ankle boot": "Ankle Boots", "ankle boots": "Ankle Boots",
    "sandal": "Sandals", "sandals": "Sandals",
    "heeled sandals": "Heeled Sandals",
    "loafer": "Loafers", "loafers": "Loafers",
    "flat": "Flats", "flats": "Flats", "ballerina": "Flats",
    "pump": "Pumps", "pumps": "Pumps", "heels": "Pumps",
    "mule": "Mules", "mules": "Mules",
    "slipper": "Slippers", "slippers": "Slippers",
    "espadrille": "Espadrilles", "espadrilles": "Espadrilles",
    "lace-up": "Lace-ups", "lace-ups": "Lace-ups", "oxfords": "Lace-ups",
    # bags & accessories
    "bag": "Handbags", "bags": "Handbags", "handbag": "Handbags",
    "handbags": "Handbags", "shoulder bag": "Shoulder Bags",
    "shoulder bags": "Shoulder Bags", "crossbody": "Shoulder Bags",
    "clutch": "Clutches", "clutches": "Clutches",
    "backpack": "Backpacks", "backpacks": "Backpacks",
    "travel bag": "Travel Bags", "travel bags": "Travel Bags",
    "luggage": "Travel Bags", "pouch": "Pouches", "pouches": "Pouches",
    "belt bag": "Pouches", "belt bags": "Pouches",
    "wallet": "Wallets", "wallets": "Wallets",
    "card holder": "Card Holders", "card holders": "Card Holders",
    "belt": "Belts", "belts": "Belts",
    "scarf": "Scarves", "scarves": "Scarves",
    "glove": "Gloves", "gloves": "Gloves",
    "hat": "Hats", "hats": "Hats", "cap": "Hats", "caps": "Hats",
    "tie": "Ties", "ties": "Ties",
    "sunglasses": "Sunglasses", "glasses": "Sunglasses",
    "jewellery": "Jewellery", "jewelry": "Jewellery",
    "key holder": "Key Holder", "keyring": "Key Holder",
    "hair accessories": "Hair Accessories",
}

GENDER_PREFIX = {"Women", "Men"}
FEMALE_TAGS = {"woman", "women", "womens", "gender_woman", "damen",
               "damenschuhe", "donna", "ladies", "female"}
MALE_TAGS = {"man", "men", "mens", "gender_man", "herren", "uomo", "male"}
SKIP_TAGS = {"junior", "kids", "gender_junior", "unisex", "gender_unisex"}
FEMALE_ONLY_TYPES = {"Dresses", "Skirts", "Jumpsuits", "Clutches", "Pumps",
                     "Mules", "Hair Accessories", "Jewellery"}


def normalise_type(product_type: str, tags: list[str], title: str) -> str | None:
    """
    Turn whatever is in product_type into a "{Gender} {Type}" key that
    GLAMI_CATEGORY knows, deriving gender from tags when it's absent.

    Handles the three shapes seen in the catalog:
      "Women Ankle Boots" -> already good
      "Jacket" / "Bags"   -> ungendered supplier value, gender from tags
      "Clothing"          -> too generic, returns None
    """
    pt = (product_type or "").strip()
    if not pt:
        return None

    # Split off a gender prefix if present.
    gender, tail = None, pt
    for prefix in ("Women", "Men"):
        if pt.startswith(prefix + " "):
            gender, tail = prefix, pt[len(prefix) + 1:]
            break
    for prefix in ("Junior ", "Unisex ", "Kids "):
        if pt.startswith(prefix):
            return None                      # not Men/Women — out of scope

    canonical = TYPE_ALIASES.get(tail.strip().lower(), tail.strip())
    if canonical.lower() in ("clothing", "accessories", "apparel", "other"):
        return None                          # genuinely too generic

    if not gender:
        low = {t.strip().lower() for t in tags}
        low |= {t.split("->")[0].strip().lower() for t in tags}
        if low & SKIP_TAGS:
            return None
        female, male = bool(low & FEMALE_TAGS), bool(low & MALE_TAGS)
        if female and not male:
            gender = "Women"
        elif male and not female:
            gender = "Men"
        elif canonical in FEMALE_ONLY_TYPES:
            gender = "Women"
        else:
            t = title.lower()
            if re.search(r"\b(woman|women|womens|ladies)\b", t):
                gender = "Women"
            elif re.search(r"\b(man|men|mens)\b", t):
                gender = "Men"
    if not gender:
        return None

    return f"{gender} {canonical}"


PRODUCTS_QUERY = """
query Feed($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle vendor productType tags descriptionHtml onlineStoreUrl
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
            raise RuntimeError(payload["errors"])
        block = payload["data"]["products"]
        yield from block["nodes"]
        if not block["pageInfo"]["hasNextPage"]:
            return
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(0.4)


def type_tail(product_type: str) -> str:
    """'Women Ankle Boots' -> 'Ankle Boots'."""
    for prefix in ("Women ", "Men ", "Junior ", "Unisex ", "Kids "):
        if product_type.startswith(prefix):
            return product_type[len(prefix):]
    return product_type


def resolve_size(product_type: str, options: list[dict],
                 single_variant: bool = False) -> tuple[str, str] | None:
    """
    (value, size_system), or None when Glami exempts this category.
    Raises ValueError when a size is required but unusable.

    Numeric clothing sizes are treated as IT: stock comes from Italian
    wholesalers, so a "40" dress is IT 40, not EU 40.
    """
    NUMERIC_CLOTHING_SYSTEM = "IT"
    tail = type_tail(product_type)

    raw = None
    for opt in options:
        if opt["name"].strip().lower() in ("size", "accessory size", "shoe size"):
            raw = (opt["value"] or "").strip()
            break

    if tail in SIZE_EXEMPT_WORDS:
        return None
    if not raw:
        # A product with a single variant and no size option is one-size —
        # dropping it would lose ~120 otherwise-valid accessories.
        if single_variant:
            return ("One size", "INT")
        raise ValueError("no size option")
    if raw.lower() in ("one size", "onesize", "os", "uni", "unisize"):
        return ("One size", "INT")

    # "EU39/US9" -> take the EU half
    dual = re.match(r"^(EU|IT|UK|US|FR|DE)\s*(\d{1,3}(?:[.,]5)?)\s*/", raw, re.I)
    if dual:
        return (dual.group(2).replace(",", "."), dual.group(1).upper())
    # "IT44", "EU 39"
    pre = re.fullmatch(r"(EU|IT|UK|US|FR|DE)\s*(\d{1,3}(?:[.,]5)?)", raw, re.I)
    if pre:
        return (pre.group(2).replace(",", "."), pre.group(1).upper())

    # "IT50 | L" — supplier gives both; the numeric half is more precise.
    combo = re.match(r"^(EU|IT|UK|US|FR|DE)\s*(\d{1,3}(?:[.,]5)?)\s*\|", raw, re.I)
    if combo:
        return (combo.group(2).replace(",", "."), combo.group(1).upper())

    # "43-44", "39-40" — a range; take the lower bound, as retailers do.
    rng = re.fullmatch(r"(\d{1,3})\s*[-–/]\s*(\d{1,3})", raw)
    if rng:
        return (rng.group(1), "EU" if tail in SHOE_WORDS else NUMERIC_CLOTHING_SYSTEM)

    # "8cm", "12 cm" — a measurement, already a size in its own right.
    cm = re.fullmatch(r"(\d{1,3})\s*cm", raw, re.I)
    if cm:
        return (f"{cm.group(1)} cm", "INT")

    if raw.upper() in ("U", "UNI", "TU", "ONE", "OS", "NOSIZE", "NO SIZE"):
        return ("One size", "INT")

    # Roman numerals — some Italian suppliers size gloves and belts this way.
    # Passed through as-is rather than guessed into letter sizes.
    if re.fullmatch(r"I{1,4}|IV|V|VI", raw.upper()):
        return (raw.upper(), "INT")

    # "S-M", "L-XL" — letter range written with a hyphen.
    lr = re.fullmatch(r"(XS|S|M|L|XL|XXL)\s*[-–/]\s*(XS|S|M|L|XL|XXL)", raw.upper())
    if lr:
        return (f"{lr.group(1)}/{lr.group(2)}", "INT")

    # "7-XXL" — numeric glove size paired with a letter size.
    mixed = re.fullmatch(r"(\d{1,2}(?:[.,]5)?)\s*[-–/]\s*(?:XS|S|M|L|XL|XXL)", raw.upper())
    if mixed:
        return (mixed.group(1).replace(",", "."), "INT")

    # "W30", "W38" — jeans waist in inches.
    waist = re.fullmatch(r"W\s*(\d{2})", raw, re.I)
    if waist:
        return (waist.group(1), "US")

    if raw.upper() in LETTER_SIZES:
        return (raw.upper(), "INT")

    # "LXL" written without a separator
    nosep = re.fullmatch(r"(XS|S|M|L|XL)(S|M|L|XL|XXL)", raw.upper())
    if nosep:
        return (f"{nosep.group(1)}/{nosep.group(2)}", "INT")

    num = re.fullmatch(r"(\d{1,3})([.,]5)?", raw)
    if num:
        value = raw.replace(",", ".")
        if tail in SHOE_WORDS:
            return (value, "EU")
        if tail == "Gloves":
            return (value, "INT")
        if tail == "Belts":
            return (f"{num.group(1)} cm", "INT")
        return (value, NUMERIC_CLOTHING_SYSTEM)

    raise ValueError(f"unrecognised size {raw!r}")


def resolve_color(options: list[dict]) -> str | None:
    for opt in options:
        if opt["name"].strip().lower() in ("color", "colour"):
            raw = (opt["value"] or "").strip()
            if not raw:
                return None          # never emit an empty <PARAM>
            return COLOR_PL.get(raw.lower(), raw)
    return None


def is_valid_gtin(value: str | None) -> bool:
    """The old feed put SKUs in <EAN>; invalid GTINs poison Glami's pairing."""
    if not value:
        return False
    v = value.strip()
    return v.isdigit() and len(v) in (8, 12, 13, 14)


JUNK = re.compile(
    r"<div class=\"product-out-of-stock\">.*?</div>"
    r"|<section[^>]*>.*?</section>"
    r"|<meta[^>]*>",
    re.DOTALL | re.IGNORECASE)
TAGS = re.compile(r"<[^>]+>")
MANUFACTURER = re.compile(r"\bManufacturer\b.*", re.DOTALL | re.IGNORECASE)


def clean_description(raw: str | None) -> str:
    if not raw:
        return ""
    t = JUNK.sub(" ", raw)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", t, flags=re.IGNORECASE)
    t = TAGS.sub(" ", t)
    t = html.unescape(t)
    t = MANUFACTURER.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def fx_eur_pln() -> Decimal:
    override = os.environ.get("GLAMI_FX_EUR_PLN")
    if override:
        return Decimal(override)
    try:
        resp = requests.get(ECB_RATES, timeout=30)
        resp.raise_for_status()
        for node in ET.fromstring(resp.content).iter():
            if node.get("currency") == "PLN":
                return Decimal(node.get("rate"))
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ! FX fetch failed ({exc}); using fallback", file=sys.stderr)
    return FX_EUR_PLN_FALLBACK


def cdata(text: str) -> str:
    return f"<![CDATA[{text.replace(']]>', ']]]]><![CDATA[>')}]]>"


def build_item(product, variant, category, size, color, description,
               price_pln, cpc, item_id: str) -> str:
    images = [n["preview"]["image"]["url"] for n in product["media"]["nodes"]
              if n.get("preview", {}).get("image", {}).get("url")]
    url = product.get("onlineStoreUrl") or f"{STOREFRONT}/products/{product['handle']}"
    vid = variant["id"].rsplit("/", 1)[-1]

    L = ["<SHOPITEM>"]
    L.append(f"<ITEM_ID>{html.escape(item_id)}</ITEM_ID>")
    L.append(f"<ITEMGROUP_ID>{product['id'].rsplit('/', 1)[-1]}</ITEMGROUP_ID>")
    L.append(f"<PRODUCTNAME>{cdata(product['title'].strip())}</PRODUCTNAME>")
    if description:
        L.append(f"<DESCRIPTION>{cdata(description)}</DESCRIPTION>")
    L.append(f"<URL>{html.escape(url)}</URL>")
    L.append(f"<URL_SIZE>{html.escape(f'{url}?variant={vid}')}</URL_SIZE>")
    if images:
        L.append(f"<IMGURL>{html.escape(images[0])}</IMGURL>")
        for img in images[1:]:
            L.append(f"<IMGURL_ALTERNATIVE>{html.escape(img)}</IMGURL_ALTERNATIVE>")
    L.append(f"<PRICE_VAT>{price_pln}</PRICE_VAT>")
    L.append(f"<MANUFACTURER>{cdata(product['vendor'].strip())}</MANUFACTURER>")
    L.append(f"<CATEGORYTEXT>{cdata(category)}</CATEGORYTEXT>")
    if cpc:
        L.append(f"<GLAMI_CPC>{cpc}</GLAMI_CPC>")
    if size:
        value, system = size
        L.append(f"<PARAM><PARAM_NAME>rozmiar</PARAM_NAME><VAL>{cdata(value)}</VAL></PARAM>")
        L.append(f"<PARAM><PARAM_NAME>size_system</PARAM_NAME><VAL>{system}</VAL></PARAM>")
    if color:
        L.append(f"<PARAM><PARAM_NAME>kolor</PARAM_NAME><VAL>{cdata(color)}</VAL></PARAM>")
    L.append("<DELIVERY_DATE>0</DELIVERY_DATE>")
    if is_valid_gtin(variant.get("barcode")):
        L.append(f"<EAN>{html.escape(variant['barcode'].strip())}</EAN>")
    L.append("</SHOPITEM>")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="glami_feed.xml")
    ap.add_argument("--audit", action="store_true")
    args = ap.parse_args()

    store, token = os.environ.get("SHOPIFY_STORE"), os.environ.get("SHOPIFY_TOKEN")
    if not store or not token:
        print("SHOPIFY_STORE and SHOPIFY_TOKEN must be set", file=sys.stderr)
        return 2

    rate = fx_eur_pln()
    cpc = os.environ.get("GLAMI_CPC")
    min_acc = os.environ.get("ACCESSORY_MIN_PRICE_EUR")
    min_acc = Decimal(min_acc) if min_acc else None
    print(f"FX EUR->PLN: {rate}")
    if min_acc:
        print(f"Skipping accessories under EUR {min_acc}")

    items: list[str] = []
    seen_ids: set[str] = set()
    stats = Counter()
    unmapped: Counter = Counter()
    bad_sizes: Counter = Counter()

    for product in shopify_products(store, token):
        stats["products_seen"] += 1
        ptype = (product.get("productType") or "").strip()

        in_stock = [v for v in product["variants"]["nodes"]
                    if (v.get("inventoryQuantity") or 0) > 0]
        if not in_stock:
            stats["skipped_out_of_stock"] += 1
            continue
        if ptype in EXCLUDED_TYPES:
            stats["skipped_excluded_type"] += 1
            continue

        key = normalise_type(ptype, product.get("tags") or [], product["title"])
        category = GLAMI_CATEGORY.get(key) if key else None
        if not category:
            stats["skipped_unmapped_type"] += 1
            unmapped[f"{ptype or '(empty)'}" + (f" -> {key}" if key else "")] += 1
            continue

        description = clean_description(product.get("descriptionHtml"))

        for variant in in_stock:
            if min_acc and type_tail(key) in ACCESSORY_TYPES \
                    and Decimal(variant["price"]) < min_acc:
                stats["skipped_cheap_accessory"] += 1
                continue

            try:
                size = resolve_size(key, variant["selectedOptions"],
                                    single_variant=len(in_stock) == 1)
            except ValueError as exc:
                stats["skipped_bad_size"] += 1
                bad_sizes[str(exc)] += 1
                continue
            price = (Decimal(variant["price"]) * rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Glami rejects duplicate ITEM_IDs. SKUs are not reliably unique —
            # a duplicated Shopify product carries the supplier's SKU twice —
            # so fall back to the variant ID, which always is.
            vid = variant["id"].rsplit("/", 1)[-1]
            item_id = (variant.get("sku") or "").strip() or vid
            if item_id in seen_ids:
                stats["duplicate_sku_resolved"] += 1
                item_id = vid
            seen_ids.add(item_id)

            items.append(build_item(
                product, variant, category, size,
                resolve_color(variant["selectedOptions"]),
                description, price, cpc, item_id))
            stats["items_written"] += 1

    print("\n--- Summary ---")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    if unmapped:
        print(f"\n--- Unmapped product types ({len(unmapped)}) ---")
        for name, count in unmapped.most_common(30):
            print(f"  {count:>5}  {name}")
        print("  -> add to GLAMI_CATEGORY (verify the path against Glami's XML)")
    if bad_sizes:
        print(f"\n--- Size problems ---")
        for reason, count in bad_sizes.most_common(15):
            print(f"  {count:>5}  {reason}")

    if args.audit:
        print("\n(audit mode: no feed written)")
        return 0

    if not items:
        print("refusing to write an empty feed", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="utf-8"?>\n<SHOP>\n')
        fh.write("\n".join(items))
        fh.write("\n</SHOP>\n")
    print(f"\nWrote {stats['items_written']} items to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
