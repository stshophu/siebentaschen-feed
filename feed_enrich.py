"""
Attribute enrichment for the Google Merchant Center feed (google_feed.py).

Supplier data arrives in mixed shapes (Italian option names and colours,
plural category words in titles, no Google category), so this module turns
it into what Google Shopping / Google Images match on:

  * google_category()  – Google taxonomy path from product type / title
  * english_color()    – Italian colour names -> English
  * feed_title()       – Brand + gender + product + colour (+ material)
  * material()         – main fabric from "Material:" / "Composition:" lines
  * size_system()      – IT/EU/UK/US/FR only when the data says so
"""

from __future__ import annotations

import html
import re

# --------------------------------------------------------------------------
# Google product category (text paths are accepted by Merchant Center)
# Order matters: the first pattern that matches wins.
# --------------------------------------------------------------------------
_A = "Apparel & Accessories"
_C = f"{_A} > Clothing"
_CA = f"{_A} > Clothing Accessories"
_HW = f"{_A} > Handbags, Wallets & Cases"

CATEGORY_RULES: list[tuple[str, str]] = [
    (r"bikini|swim|beachwear", f"{_C} > Swimwear"),
    (r"underwear|brief|boxer|bra\b|bras\b|socks?\b|lingerie", f"{_C} > Underwear & Socks"),
    (r"tracksuit", f"{_C} > Activewear"),
    (r"wallet|card ?holder|key ?holder|purse|holder", f"{_HW} > Wallets & Money Clips"),
    (r"backpack", "Luggage & Bags > Backpacks"),
    (r"travel bag|duffel|luggage|suitcase", "Luggage & Bags"),
    (r"bag|clutch|pouch|tote|handbag", f"{_HW} > Handbags"),
    (r"belt", f"{_CA} > Belts"),
    (r"scarf|scarves|shawl|stole|foulard", f"{_CA} > Scarves & Shawls"),
    (r"\bhats?\b|\bcaps?\b|beanie|bucket hat|baseball", f"{_CA} > Hats"),
    (r"glove", f"{_CA} > Gloves & Mittens"),
    (r"bow ?ties?|pocket squares?|collar", _CA),
    (r"\bties?\b|necktie", f"{_CA} > Neckties"),
    (r"sunglass|glasses|eyewear", f"{_CA} > Sunglasses"),
    (r"blanket|throw", "Home & Garden > Linens & Bedding > Bedding > Blankets"),
    (r"sneaker|shoe|boot|sandal|mule|loafer|mocassin|pump|flat|espadrille|slipper|"
     r"slide|slip-on|derby|oxford|brogue|heel|ballerina|trainer|85mm", f"{_A} > Shoes"),
    (r"jumpsuit|overall|salopette|romper|playsuit|dungaree", f"{_C} > One-Pieces"),
    (r"\bsuits?\b", f"{_C} > Suits"),
    (r"dress", f"{_C} > Dresses"),
    (r"skirt", f"{_C} > Skirts"),
    (r"shorts|bermuda|\bshort\b", f"{_C} > Shorts"),
    (r"jean|trouser|pant|jogger|sweatpant|chino|legging", f"{_C} > Pants"),
    (r"\bvests?\b|gilet|sleeveless jacket", f"{_C} > Outerwear > Vests"),
    (r"jacket|coat|parka|bomber|blazer|windbreaker|winkbreaker|cape|poncho|"
     r"down|trench|peacoat|anorak|outerwear", f"{_C} > Outerwear > Coats & Jackets"),
    (r"shirt|t-shirt|polo|top\b|tops\b|blouse|sweat|hoodie|knit|sweater|pullover|"
     r"jumper|cardigan|turtleneck|tank|overshirt|bodysuit", f"{_C} > Shirts & Tops"),
    (r"clothing", _C),
    (r"accessor", _CA),
]
_CATEGORY_RES = [(re.compile(p, re.I), path) for p, path in CATEGORY_RULES]


def google_category(existing: str | None, product_type: str, title: str) -> str | None:
    """Existing numeric metafield wins (normalised: '5423.0' -> '5423'), then
    keyword rules on the product type, then on the title."""
    v = (existing or "").strip()
    if re.fullmatch(r"\d+(\.0+)?", v):
        return v.split(".")[0]
    if v:
        return v
    for text in (product_type or "", title or ""):
        for rx, path in _CATEGORY_RES:
            if rx.search(text):
                return path
    return None


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------
IT_COLORS = {
    "blu": "Blue", "blu navy": "Navy", "navy": "Navy", "nero": "Black", "nera": "Black",
    "bianco": "White", "bianca": "White", "grigio": "Grey", "grigia": "Grey",
    "verde": "Green", "verde acqua": "Aqua Green", "beige": "Beige", "rosso": "Red",
    "rossa": "Red", "marrone": "Brown", "rosa": "Pink", "azzurro": "Light Blue",
    "azzurra": "Light Blue", "celeste": "Light Blue", "giallo": "Yellow", "gialla": "Yellow",
    "viola": "Purple", "arancione": "Orange", "multicolor": "Multicolor",
    "multicolore": "Multicolor", "camouflage": "Camouflage", "cammello": "Camel",
    "lilla": "Lilac", "fucsia": "Fuchsia", "ghiaccio": "Ice", "oro": "Gold",
    "argento": "Silver", "bordeaux": "Burgundy", "bordò": "Burgundy", "avorio": "Ivory",
    "panna": "Cream", "crema": "Cream", "cuoio": "Tan", "tortora": "Taupe",
    "senape": "Mustard", "corallo": "Coral", "kaki": "Khaki", "cachi": "Khaki",
    "turchese": "Turquoise", "antracite": "Anthracite", "sabbia": "Sand",
    "fantasia": "Multicolor", "trasparente": "Transparent",
}


def english_color(raw: str | None) -> str | None:
    c = re.sub(r"\s+", " ", (raw or "").strip())
    if not c:
        return None
    low = c.lower()
    if low in IT_COLORS:
        return IT_COLORS[low]
    # "nero/bianco", "blu - rosso"
    parts = re.split(r"\s*[/,-]\s*", low)
    if len(parts) > 1 and all(p in IT_COLORS for p in parts):
        return "/".join(IT_COLORS[p] for p in parts)
    return c if not c.islower() else c.title()


COLOR_LINE = re.compile(r"(?:^|\n)\s*(?:colou?r|colore)\s*:\s*([^\n]+)", re.I)


_EN_COLORS = sorted({*IT_COLORS.values(), "Black", "White", "Grey", "Gray", "Blue", "Navy", "Red",
                     "Green", "Brown", "Beige", "Pink", "Purple", "Yellow", "Orange", "Ecru",
                     "Cream", "Ivory", "Camel", "Khaki", "Burgundy", "Bordeaux", "Gold", "Silver",
                     "Multicolor", "Multicolour", "Light Blue", "Dark Blue", "Dark Grey",
                     "Light Grey", "Olive", "Taupe", "Sand", "Tan", "Off White", "Off-White",
                     "Petrol", "Lilac", "Fuchsia", "Turquoise", "Coral", "Mustard", "Bronze",
                     "Anthracite", "Military Green", "Army Green", "Nude", "Powder"},
                    key=len, reverse=True)
_TITLE_COLOR = re.compile(r"\b(" + "|".join(map(re.escape, _EN_COLORS)) + r")\b", re.I)


def color_from_title(title: str, brand: str = "") -> str | None:
    t = title or ""
    if brand and t.lower().startswith(brand.lower()):
        t = t[len(brand):]          # "Off-White" the brand is not a colour
    m = list(_TITLE_COLOR.finditer(t))
    if not m:
        return None
    c = m[-1].group(1)
    return "Grey" if c.lower() == "gray" else c.title()


def color_from_tags(tags: list[str]) -> str | None:
    """Supplier syncs often tag the colour ("Black", "grey", "Color_Green")."""
    for t in tags or []:
        v = re.sub(r"^(colou?r|colore)[_: ]+", "", t.strip(), flags=re.I)
        en = english_color(v) if v.lower() in IT_COLORS else v
        if en and _TITLE_COLOR.fullmatch(en):
            return "Grey" if en.lower() == "gray" else en.title()
    return None


def color_from_description(desc: str) -> str | None:
    m = COLOR_LINE.search(desc or "")
    return english_color(m.group(1).strip()[:60]) if m else None


# --------------------------------------------------------------------------
# Material
# --------------------------------------------------------------------------
MATERIAL_LINE = re.compile(r"(?:^|\n)\s*(?:material|materiale)\s*:\s*([^\n]+)", re.I)
COMPOSITION_LINE = re.compile(r"(?:^|\n)\s*(?:composition|composizione)\s*:\s*([^\n]+)", re.I)
PCT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \-]*?)(?=\s*\d|\s*[,;]|$)")


def material(desc: str) -> str | None:
    m = MATERIAL_LINE.search(desc or "")
    if m:
        return m.group(1).strip().title()[:200] or None
    m = COMPOSITION_LINE.search(desc or "")
    if not m:
        return None
    pairs = [(float(p.replace(",", ".")), name.strip()) for p, name in PCT.findall(m.group(1))]
    pairs = [(p, n) for p, n in pairs if n]
    if not pairs:
        return None
    pairs.sort(key=lambda x: -x[0])
    names: list[str] = []
    for _, n in pairs[:3]:
        n = n.title()
        if n not in names:
            names.append(n)
    return "/".join(names)[:200]


# --------------------------------------------------------------------------
# Size system
# --------------------------------------------------------------------------
SIZE_SYS = re.compile(r"\b(IT|EU|UK|US|FR)\b")
SIZE_FORMAT_LINE = re.compile(r"size format\s*:\s*(IT|EU|UK|US|FR)\b", re.I)


def size_system(size: str | None, desc: str) -> str | None:
    if size:
        m = SIZE_SYS.search(size)
        if m:
            return m.group(1)
    m = SIZE_FORMAT_LINE.search(desc or "")
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------
# Title
# --------------------------------------------------------------------------
PLURALS = {
    "Sweatshirts": "Sweatshirt", "T-Shirts": "T-Shirt", "T-shirts": "T-Shirt",
    "Polo Shirts": "Polo Shirt", "Shirts": "Shirt", "Jackets": "Jacket", "Coats": "Coat",
    "Dresses": "Dress", "Skirts": "Skirt", "Sweaters": "Sweater", "Tops": "Top",
    "Bags": "Bag", "Belts": "Belt", "Blazers": "Blazer", "Cardigans": "Cardigan",
    "Hoodies": "Hoodie", "Vests": "Vest", "Scarves": "Scarf", "Hats": "Hat", "Caps": "Cap",
    "Gloves": "Gloves", "Wallets": "Wallet", "Backpacks": "Backpack", "Pullovers": "Pullover",
}
_PLURAL_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, PLURALS), key=len, reverse=True)) + r")\b")
GENDER_WORDS = {"man": "Men's", "men": "Men's", "woman": "Women's", "women": "Women's",
                "junior": "Kids'", "kids": "Kids'"}
_GENDER_RE = re.compile(r"\b(man|men|woman|women|junior|kids)\b(?!['’])", re.I)
_HAS_GENDER = re.compile(r"\b(men's|women's|kids'|unisex|man|men|woman|women|junior|kids)\b", re.I)
SUFFIX = re.compile(r"\s*[|–-]\s*Siebentaschen\s*$", re.I)


_COMPOSITION = re.compile(r"\s*[–,-]?\s*(?:\d{1,3}\s*%\s*[A-Za-zÀ-ÿ]+[\s,/-]*)+(?:Made In\b)?", re.I)


def _clean_title(t: str) -> str:
    t = re.sub(r"\bMade In\b\s*$", "", t.strip(), flags=re.I)
    t = _COMPOSITION.sub(" ", t)                       # "– 56% Silk, 26% Polyur…"
    t = re.sub(r"\b\d{1,3}\s*%", " ", t)              # stray "100%"
    t = re.sub(r"\s*,\s*", " ", t)                     # commas add nothing in a title
    t = re.sub(r"(\s*[–-]\s*){2,}", " – ", t)           # "– –"
    t = re.sub(r"[\s,–-]+$", "", t).strip(" ,")        # dangling "–" / ","
    t = re.sub(r"[\s,]*\bMade In\s*$", "", t, flags=re.I)
    if t.isupper():
        t = " ".join(w.capitalize() for w in t.split())
    return re.sub(r"\s+", " ", t)


def feed_title(title: str, seo_title: str | None, brand: str, gender: str, age: str,
               color: str | None, mat: str | None) -> str:
    t = html.unescape(title or "").strip()
    seo = SUFFIX.sub("", html.unescape(seo_title or "")).strip()
    # "… Shirt – Women" -> "… Shirt" (gender is put after the brand below)
    seo = re.sub(r"\s*[–-]\s*(women|men|woman|man|kids|unisex)\b", "", seo, flags=re.I).strip()
    # SEO titles were written by hand / by the SEO job and are usually richer.
    if seo and brand and brand.lower()[:4] in seo.lower() and len(seo) > len(t):
        t = seo
    t = re.sub(r"\s+", " ", t)
    t = _clean_title(t)

    # "Man"/"Woman" in supplier titles -> "Men's"/"Women's" (first occurrence only)
    t = _GENDER_RE.sub(lambda m: GENDER_WORDS[m.group(1).lower()], t, count=1)
    if not _HAS_GENDER.search(t):
        label = {"male": "Men's", "female": "Women's"}.get(gender) if age == "adult" else "Kids'"
        if label:
            b = re.match(re.escape(brand), t, re.I) if brand else None
            t = f"{t[:b.end()]} {label}{t[b.end():]}" if b else f"{label} {t}"
    t = _PLURAL_RE.sub(lambda m: PLURALS[m.group(1)], t)

    low = t.lower()
    if color and color.lower() not in low and not any(
            it in low.split() for it, en in IT_COLORS.items() if en == color):
        t = f"{t} {color}"
    if mat and "/" not in mat and len(t) < 60 and mat.lower() not in t.lower():
        t = f"{t} – {mat}"
    return _clean_title(re.sub(r"\s+", " ", t).strip())[:150]
