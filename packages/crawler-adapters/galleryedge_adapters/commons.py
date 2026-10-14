"""Wikimedia Commons adapter (openly-licensed, SFW subset). The only real-site adapter shipped.

Policy (enforced in code, not configurable):
* Only ``/wiki/Category:<name>`` pages from an explicit SFW allow-list, and the ``/wiki/File:`` pages
  they link to. No recursion into sub-categories. ``crawler-core`` still checks robots.txt for every
  URL (Commons' robots.txt disallows ``/w/`` incl. ``api.php`` for generic crawlers, so we read the
  public HTML pages, which robots.txt allows, at >= 1 request/second with a contact User-Agent).
* Licenses: CC0, Public domain, CC BY, CC BY-SA only (no NC/ND, no GFDL-only, no "fair use").
* SFW: any category / description hit on the block-list => skipped (reason ``sfw-blocked``).
* Images are NOT downloaded: we store the Commons 500px thumbnail URL for hotlinking with attribution.
"""
from __future__ import annotations

import hashlib
import html as H
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from galleryedge_crawl import Link, Record, Response, Skip

HOST = "commons.wikimedia.org"
SFW_CATEGORIES = (
    # neutral subjects only: no country/region categories (they pull in flags, maps, politics)
    "Featured_pictures_of_landscapes", "Featured_pictures_of_mountains", "Featured_pictures_of_bodies_of_water",
    "Featured_pictures_of_forests", "Featured_pictures_of_coasts", "Featured_pictures_of_volcanoes",
    "Featured_pictures_of_caves", "Featured_pictures_of_gardens", "Featured_pictures_of_parks", "Featured_pictures_of_islands",
    "Featured_pictures_of_birds", "Featured_pictures_of_flowers", "Featured_pictures_of_lighthouses", "Featured_pictures_of_bridges",
    "Featured_pictures_of_cats", "Featured_pictures_of_dogs", "Featured_pictures_of_food", "Featured_pictures_of_fruit",
    "Featured_pictures_of_architecture", "Featured_pictures_of_architectural_elements", "Featured_pictures_of_clouds",
    "Featured_pictures_of_waterfalls", "Featured_pictures_of_beaches", "Featured_pictures_of_nature", "Featured_pictures_of_plants",
    "Featured_pictures_of_fungi", "Featured_pictures_of_Lepidoptera", "Featured_pictures_of_trees", "Featured_pictures_of_lakes",
    "Featured_pictures_of_sunsets", "Featured_pictures_of_reptiles",
    # polish-2: more neutral subjects (verified non-empty 2026-10-14)
    "Featured_pictures_of_Hymenoptera", "Featured_pictures_of_Odonata", "Featured_pictures_of_molluscs",
    "Quality_images_of_flowers", "Quality_images_of_birds", "Quality_images_of_insects", "Quality_images_of_fungi",
)
BLOCK = re.compile(r"nud|naked|erotic|sexual|\bsex\b|porn|genital|breast|nipple|lingerie|underwear|bikini|topless|corpse|dead bod|"
                   r"cadaver|shunga|bijin-?ga|bathing|bather|sweat in art|swimsuit|gore|blood|execution|wound|injur|war crime|massacre|autopsy|surgery|medical|penis|vagina|buttock|"
                   r"裸|色情|尸|血", re.I)
# politically sensitive (polish-9: whole words + context instead of substrings)
# HARD: always blocks, in any field (title, description, categories/tags)
HARD = re.compile(
    r"\bflags?\b|flag of|旗|coats? of arms|heraldry|\bemblems?\b|\bseals? of\b|\binsignia\b|徽|\bmaps?\b|地图|cartograph|"
    r"\bdisputed\b|\bsovereign|\bannex(ed|ation)?\b|\boccupied territor|\bpolitic(s|al|ian|ians)?\b|\bpresident\b|prime minister|"
    r"\bchairman\b|\bdictator|\bcommunis[mt]s?\b|communist party|\bnationalis[mt]s?\b|propaganda|\bparliament|\bcongress\b|"
    r"\belections?\b|\bprotest(s|ers?)?\b|\bdemonstrat(ion|ors?)s?\b(?! of)|\briots?\b|\brevolution(ary)?\b|\bwars?\b|\bbattle(s|field)?\b|"
    r"\bmilitary\b|\barmy\b(?! ants?)|\bnavy\b|\bsoldiers?\b|\bweapons?\b|\bmissiles?\b|\bwarships?\b|aircraft carrier|"
    r"memorial to|\bmartyrs?\b|genocide|\bregime\b|\bpope\b|dalai lama|\blamas?\b|\bbuddha\b|\bjesus\b|\bchrist\b|\ballah\b|"
    r"\breligio(n|ns|us)\b|\bmosques?\b|"
    r"中国|中华|台湾|台灣|香港|澳门|西藏|新疆|總統|总统|政治|宗教|军事|军队|解放军|战争|战役|抗战", re.I)
# China-related region names: block in title/description; ignored only inside geo-index categories ("Flora of Hong Kong")
REGION_CN = re.compile(r"\bchina\b|\bchinese\b|\btaiwan(ese)?\b|hong kong|\bmacau\b|\btibet(an)?\b|\bxinjiang\b", re.I)
# SOFT_REGION: other country names — fine inside geo-index categories ("Hymenoptera of Israel"); in title/desc need nature context
SOFT_REGION = re.compile(
    r"\bjapan(ese)?\b|\bkorea(n)?\b|\brussia(n)?\b|\bukrain(e|ian)\b|\bisrael(i)?\b|\bpalestin(e|ian)\b|\biran(ian)?\b|\bcrimea\b|"
    r"日本|朝鲜|韩国", re.I)
# SOFT_TOPIC: borders, parties, religious buildings... — block as a category topic; in title/desc need nature context
SOFT_TOPIC = re.compile(
    r"\bborders?\b|\bboundar(y|ies)\b|\bterritor(y|ies|ial)\b|\bparty\b|\bparades?\b|\bcathedrals?\b|\bchurch(es)?\b|"
    r"\btemples?\b|\bshrines?\b|\bmonaster(y|ies)\b|\bpagodas?\b", re.I)
NATURE = re.compile(
    r"\b(flowers?|flora|plants?|trees?|leaf|leaves|blossoms?|blooming|bloom|bouquet|garden|pine|maple|moss|ferns?|orchids?|"
    r"birds?|insects?|bees?|wasps?|ants?|butterfl(y|ies)|moths?|beetles?|dragonfl(y|ies)|spiders?|fung(i|us)|mushrooms?|lichens?|"
    r"cats?|kittens?|dogs?|fish|frogs?|lizards?|snakes?|turtles?|deer|squirrels?|"
    r"landscape|waterfalls?|lakes?|rivers?|mountains?|clouds?|sunset|sunrise|sky|beach|coast|sea|forest|snow|ice|autumn|spring)\b|"
    r"花|鸟|蝴蝶|昆虫|猫|狗|树|叶|山|湖|海|云", re.I)
GEO_CAT = re.compile(r"\b(of|in|from)\b", re.I)


def sensitive_reason(title: str, desc: str = "", cats=()) -> str | None:
    """None if OK, else a short reason. Hard terms block anywhere; region/soft terms are judged in context."""
    cats = [c.replace("_", " ") for c in cats]
    text = " ".join([title or "", desc or ""])
    for c in [text, *cats]:
        m = HARD.search(c)
        if m:
            return f"hard:{m.group(0).lower()}"
    for c in [text, *cats]:  # China-related regions: conservative, block anywhere
        m = REGION_CN.search(c)
        if m:
            return f"region:{m.group(0).lower()}"
    for c in cats:
        m = SOFT_TOPIC.search(c) or (SOFT_REGION.search(c) if not GEO_CAT.search(c) else None)
        if m:
            return f"category:{m.group(0).lower()}"
    m = SOFT_TOPIC.search(text) or SOFT_REGION.search(text)
    if m and not NATURE.search(text):
        return f"soft:{m.group(0).lower()}"
    return None


SENSITIVE = HARD  # backwards-compatible name (hard terms)

LICENSE_OK = re.compile(r"^(cc0( 1\.0)?|public domain|pd(-.*)?|cc by( \d\.\d)?( [a-z]{2,})?|cc by-sa( \d\.\d)?( [a-z]{2,})?)$", re.I)
# tiny glossary so Chinese queries work on English-language metadata (added as zh tags, marked in rights_note)
ZH = {"landscape": "风景", "mountain": "山", "lake": "湖", "river": "河流", "waterfall": "瀑布", "sea": "海", "coast": "海岸",
      "beach": "海滩", "forest": "森林", "tree": "树", "volcano": "火山", "cave": "洞穴", "garden": "花园", "park": "公园",
      "island": "岛屿", "bird": "鸟", "flower": "花", "lighthouse": "灯塔", "bridge": "桥", "snow": "雪",
      "sunset": "日落", "sunrise": "日出", "night": "夜景", "city": "城市", "desert": "沙漠", "glacier": "冰川", "valley": "山谷", "sky": "天空", "cloud": "云", "cat": "猫", "dog": "狗", "food": "美食", "fruit": "水果", "fungus": "真菌", "mushroom": "蘑菇",
      "butterfly": "蝴蝶", "moth": "蛾", "insect": "昆虫", "architecture": "建筑", "autumn": "秋天", "reptile": "爬行动物", "plant": "植物",
      "leaf": "叶子", "star": "星空", "milky way": "银河"}

# only the MEDIA pager (#mw-category-media); the subcategory pager also carries filefrom= but never advances it
NEXT_RE = re.compile(r'<a href="(/w/index\.php\?title=Category:[^"]*filefrom=[^"#]*#mw-category-media)"[^>]*>next page</a>')
CAT_FILE_RE = re.compile(r'<a href="(/wiki/File:[^"#?]+)"')
CATLINKS_RE = re.compile(r'id="mw-normal-catlinks".*?</div>', re.S)
TITLE_RE = re.compile(r'title="Category:([^"]+)"')


def _txt(s: str) -> str:
    s = re.sub(r"<(style|script)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _span(doc: str, cls: str) -> str:
    m = re.search(r'class="' + cls + r'"[^>]*>(.*?)</span>', doc, re.S)
    return _txt(m.group(1)) if m else ""


def _desc(doc: str, langs) -> str:
    for lang in langs:
        m = re.search(r'<div class="description ' + lang + r'"[^>]*>(.*?)</div>', doc, re.S)
        if m:
            return re.sub(r"^[^:：]{1,30}[:：]\s*", "", _txt(m.group(1)))[:600]
    return ""


def clean_author(s: str) -> str:
    """licensetpl_attr often carries the whole licence blurb; keep only the credit line."""
    s = re.split(r"\s+You are free|\s+This file (?:is licensed|was uploaded)", s or "", maxsplit=1)[0]
    s = re.sub(r"\(via Wikimedia Commons\)", "", s)
    s = re.split(r"\s*\.mw-parser-output\b", s, maxsplit=1)[0]  # leaked TemplateStyles CSS
    s = re.sub(r"\s*/\s*(CC[- ]BY[-A-Z ]*[\d.]*|CC0[\d. ]*|Public domain)\s*$", "", s.strip(), flags=re.I)
    s = re.sub(r"^©\s*", "", s).strip(" ,;/")
    return (s[:120] or "unknown")


def zh_tags_for(text: str) -> list[str]:
    hay = text.lower()
    return sorted({v for k, v in ZH.items() if re.search(r"\b" + re.escape(k) + r"(?:e?s)?\b", hay)})


def attribution_for(title: str, author: str, lic: str, lic_url: str) -> str:
    return (f"“{title}” by {author}, {lic}" + (f" ({lic_url})" if lic_url else "") + ", via Wikimedia Commons")[:600]


def normalize_record(r: dict) -> dict:
    """Re-apply the current cleaning rules to an already-collected record (no network)."""
    r = dict(r)
    author = clean_author(r.get("author") or r.get("artist") or "")
    base = [t for t in r.get("tags", []) if not re.search(r"[\u4e00-\u9fff]", t)]
    r.update(author=author, artist=author, tags=list(dict.fromkeys(base + zh_tags_for(" ".join([r.get("title", ""), r.get("prompt", ""), *base])))),
             attribution=attribution_for(r["title"], author, r["license"], r.get("license_url", "")))
    return r


def sfw_ok(record: dict) -> bool:
    """Re-check an already-collected record against the (possibly updated) block-list."""
    hay = " ".join([record.get("title", ""), record.get("prompt", ""), *record.get("tags", [])])
    return not BLOCK.search(hay) and sensitive_reason(record.get("title", ""), record.get("prompt", ""), record.get("tags", [])) is None


def thumb_url(og_image: str, width: int = 500) -> str | None:
    u = H.unescape(og_image).split("?", 1)[0]
    m = re.match(r"https://(?:upload|thumb)\.wikimedia\.org/wikipedia/commons/thumb/(.+)/\d+px-([^/]+)$", u)
    if not m:
        return None
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{m.group(1)}/{width}px-{m.group(2)}"


class CommonsAdapter:
    name = "wikimedia-commons"

    def __init__(self, categories=SFW_CATEGORIES, max_category_pages: int = 1):
        bad = [c for c in categories if c not in SFW_CATEGORIES]
        if bad:
            raise ValueError(f"category not on the SFW allow-list: {bad}")
        self.categories = list(categories)
        self.max_category_pages = max(1, max_category_pages)
        self._cat_pages: dict[str, int] = {}

    def start_urls(self):
        return [f"https://{HOST}/wiki/Category:{c}" for c in self.categories]

    def parse(self, resp: Response):
        path = urlsplit(resp.url).path
        if path.startswith("/wiki/Category:"):
            files = list(dict.fromkeys(CAT_FILE_RE.findall(resp.text)))
            if not files:  # misspelt/empty category: make it visible in skip reasons instead of silently yielding nothing
                yield Skip("empty-category"); return
            for href in files:
                yield Link(href)
            # category pagination: MediaWiki links "next page" via /w/index.php (robots-disallowed), so we
            # rewrite it to the equivalent /wiki/Category:X?filefrom=... URL, bounded per category.
            cat = unquote(path[len("/wiki/Category:"):])
            m = NEXT_RE.search(resp.text)
            seen = self._cat_pages.get(cat, 1)
            if m and seen < self.max_category_pages:
                self._cat_pages[cat] = seen + 1
                ff = parse_qs(urlsplit(H.unescape(m.group(1))).query).get("filefrom", [""])[0]
                if ff:
                    yield Link(f"https://{HOST}/wiki/Category:{quote(cat)}?" + urlencode({"filefrom": ff}))
            return
        if path.startswith("/wiki/File:"):
            yield from self._file(resp, path)

    def _file(self, resp: Response, path: str):
        doc = H.unescape(resp.text)
        lic = _span(doc, "licensetpl_short")
        if not lic or "licensetpl_nonfree" in doc or not LICENSE_OK.match(lic):
            yield Skip("license-not-allowed"); return
        cats = [H.unescape(c) for c in TITLE_RE.findall((CATLINKS_RE.search(doc) or re.match("", "")).group(0) or "")]
        desc_en, desc_zh = _desc(doc, ("en",)), _desc(doc, ("zh-hans", "zh-cn", "zh", "zh-hant", "zh-tw", "zh-hk"))
        name = unquote(path[len("/wiki/File:"):]).replace("_", " ")
        if BLOCK.search(" ".join([name, desc_en, desc_zh, *cats])):
            yield Skip("sfw-blocked"); return
        if sensitive_reason(name, " ".join([desc_en, desc_zh]), cats):
            yield Skip("sensitive-blocked"); return
        og = re.search(r'<meta property="og:image" content="([^"]+)"', doc)
        cover = thumb_url(og.group(1)) if og else None
        if not cover:
            yield Skip("no-thumbnail"); return
        am = re.search(r'id="fileinfotpl_aut"[^>]*>.*?</td>\s*<td[^>]*>(.*?)</td>', doc, re.S)
        author = clean_author(_span(doc, "licensetpl_attr") or (_txt(am.group(1)) if am else ""))
        lic_url = _span(doc, "licensetpl_link") or ""
        title = re.sub(r"\.(jpe?g|png|tiff?|webp|gif|svg)$", "", name, flags=re.I)
        hay = " ".join([title, desc_en, *cats]).lower()
        zh_tags = zh_tags_for(hay)
        tags = list(dict.fromkeys([c.replace("_", " ") for c in cats if "," not in c and not c[:1].isdigit() and not c.lower().startswith(("featured", "quality", "valued", "pages ", "cc-", "self-published", "files ", "media ", "uploaded", "photographs taken"))][:20] + zh_tags))
        canon = f"https://{HOST}/wiki/File:{quote(name.replace(' ', '_'))}"
        lic_short = lic.strip()
        attribution = attribution_for(title, author, lic_short, lic_url)
        yield Record({
            "id": "wmc-" + re.sub(r"[^A-Za-z0-9]+", "-", unquote(path.split(":", 1)[1]))[:90].strip("-").lower() + "-" + hashlib.sha1(name.encode()).hexdigest()[:8],
            "title": title[:200], "prompt": " ".join(x for x in (desc_zh, desc_en) if x), "artist": author[:120],
            "tags": tags, "license": lic_short, "license_url": lic_url, "attribution": attribution[:600], "author": author[:200],
            "rating": "general", "source_url": canon, "cover_url": cover, "image_count": 1,
            "rights_note": "Hotlinked Commons thumbnail; attribution shown on the work page." + (" zh tags from glossary." if zh_tags else ""),
        })
