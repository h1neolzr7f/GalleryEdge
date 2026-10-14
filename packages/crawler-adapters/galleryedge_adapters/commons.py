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
from urllib.parse import quote, unquote, urlsplit

from galleryedge_crawl import Link, Record, Response, Skip

HOST = "commons.wikimedia.org"
SFW_CATEGORIES = (
    "Featured_pictures_of_landscapes", "Featured_pictures_of_mountains", "Featured_pictures_of_bodies_of_water",
    "Featured_pictures_of_forests", "Featured_pictures_of_coasts", "Featured_pictures_of_volcanoes",
    "Featured_pictures_of_caves", "Featured_pictures_of_gardens", "Featured_pictures_of_parks", "Featured_pictures_of_islands",
    "Featured_pictures_of_China", "Featured_pictures_of_Japan", "Featured_pictures_of_Taiwan", "Featured_pictures_of_Hong_Kong",
    "Featured_pictures_of_birds", "Featured_pictures_of_flowers", "Featured_pictures_of_lighthouses", "Featured_pictures_of_bridges",
)
BLOCK = re.compile(r"nud|naked|erotic|sexual|\bsex\b|porn|genital|breast|nipple|lingerie|underwear|bikini|topless|corpse|dead bod|"
                   r"cadaver|gore|blood|execution|wound|injur|war crime|massacre|autopsy|surgery|medical|penis|vagina|buttock|"
                   r"裸|色情|尸|血", re.I)
LICENSE_OK = re.compile(r"^(cc0( 1\.0)?|public domain|pd(-.*)?|cc by( \d\.\d)?( [a-z]{2,})?|cc by-sa( \d\.\d)?( [a-z]{2,})?)$", re.I)
# tiny glossary so Chinese queries work on English-language metadata (added as zh tags, marked in rights_note)
ZH = {"landscape": "风景", "mountain": "山", "lake": "湖", "river": "河流", "waterfall": "瀑布", "sea": "海", "coast": "海岸",
      "beach": "海滩", "forest": "森林", "tree": "树", "volcano": "火山", "cave": "洞穴", "garden": "花园", "park": "公园",
      "island": "岛屿", "bird": "鸟", "flower": "花", "lighthouse": "灯塔", "bridge": "桥", "temple": "寺庙", "snow": "雪",
      "sunset": "日落", "sunrise": "日出", "night": "夜景", "city": "城市", "china": "中国", "japan": "日本", "taiwan": "台湾",
      "hong kong": "香港", "desert": "沙漠", "glacier": "冰川", "valley": "山谷", "sky": "天空", "cloud": "云"}

CAT_FILE_RE = re.compile(r'<a href="(/wiki/File:[^"#?]+)"')
CATLINKS_RE = re.compile(r'id="mw-normal-catlinks".*?</div>', re.S)
TITLE_RE = re.compile(r'title="Category:([^"]+)"')


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _span(doc: str, cls: str) -> str:
    m = re.search(r'class="' + cls + r'"[^>]*>(.*?)</span>', doc, re.S)
    return _txt(m.group(1)) if m else ""


def _desc(doc: str, langs) -> str:
    for lang in langs:
        m = re.search(r'<div class="description ' + lang + r'"[^>]*>(.*?)</div>', doc, re.S)
        if m:
            return re.sub(r"^[^:：]{1,30}[:：]\s*", "", _txt(m.group(1)))[:600]
    return ""


def thumb_url(og_image: str, width: int = 500) -> str | None:
    u = H.unescape(og_image).split("?", 1)[0]
    m = re.match(r"https://(?:upload|thumb)\.wikimedia\.org/wikipedia/commons/thumb/(.+)/\d+px-([^/]+)$", u)
    if not m:
        return None
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{m.group(1)}/{width}px-{m.group(2)}"


class CommonsAdapter:
    name = "wikimedia-commons"

    def __init__(self, categories=SFW_CATEGORIES):
        bad = [c for c in categories if c not in SFW_CATEGORIES]
        if bad:
            raise ValueError(f"category not on the SFW allow-list: {bad}")
        self.categories = list(categories)

    def start_urls(self):
        return [f"https://{HOST}/wiki/Category:{c}" for c in self.categories]

    def parse(self, resp: Response):
        path = urlsplit(resp.url).path
        if path.startswith("/wiki/Category:"):
            for href in dict.fromkeys(CAT_FILE_RE.findall(resp.text)):
                yield Link(href)
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
        og = re.search(r'<meta property="og:image" content="([^"]+)"', doc)
        cover = thumb_url(og.group(1)) if og else None
        if not cover:
            yield Skip("no-thumbnail"); return
        am = re.search(r'id="fileinfotpl_aut"[^>]*>.*?</td>\s*<td[^>]*>(.*?)</td>', doc, re.S)
        author = _span(doc, "licensetpl_attr") or (_txt(am.group(1)) if am else "") or "unknown"
        lic_url = _span(doc, "licensetpl_link") or ""
        title = re.sub(r"\.(jpe?g|png|tiff?|webp|gif|svg)$", "", name, flags=re.I)
        hay = " ".join([title, desc_en, *cats]).lower()
        zh_tags = sorted({v for k, v in ZH.items() if re.search(r"\b" + re.escape(k) + r"s?\b", hay)})
        tags = list(dict.fromkeys([c.replace("_", " ") for c in cats if "," not in c and not c[:1].isdigit() and not c.lower().startswith(("featured", "quality", "valued", "pages ", "cc-", "self-published", "files ", "media ", "uploaded", "photographs taken"))][:20] + zh_tags))
        canon = f"https://{HOST}/wiki/File:{quote(name.replace(' ', '_'))}"
        lic_short = lic.strip()
        attribution = f"“{title}” by {author}, {lic_short}" + (f" ({lic_url})" if lic_url else "") + ", via Wikimedia Commons"
        yield Record({
            "id": "wmc-" + re.sub(r"[^A-Za-z0-9]+", "-", unquote(path.split(":", 1)[1]))[:90].strip("-").lower() + "-" + hashlib.sha1(name.encode()).hexdigest()[:8],
            "title": title[:200], "prompt": " ".join(x for x in (desc_zh, desc_en) if x), "artist": author[:120],
            "tags": tags, "license": lic_short, "license_url": lic_url, "attribution": attribution[:600], "author": author[:200],
            "rating": "general", "source_url": canon, "cover_url": cover, "image_count": 1,
            "rights_note": "Hotlinked Commons thumbnail; attribution shown on the work page." + (" zh tags from glossary." if zh_tags else ""),
        })
