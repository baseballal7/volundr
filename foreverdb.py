"""Völundr — ForeverDB.net API client (recipe sources: trainers, vendors, quests, drops).

Terms (free tier): 5,000 req/day, 30/min, a visible "Data from ForeverDB.net" link wherever the
data is shown, a User-Agent naming the app, never ship the key. Key by request on
https://foreverdb.net/developers (Battle.net login). Field names below are read defensively —
the response shape was taken from the public docs, not from a live call.
"""
import os, json, time, urllib.request, urllib.parse, urllib.error

KEY = os.environ.get("FOREVERDB_API_KEY", "")
BASE = "https://foreverdb.net/api/v1"
UA = "Volundr/0.1 (personal homelab crafting tracker; +https://foreverdb.net credited in UI)"
SLUGS = ["alchemy", "blacksmithing", "enchanting", "engineering", "leatherworking", "tailoring",
         "herbalism", "mining", "skinning", "cooking", "first-aid", "fishing"]


def configured() -> bool:
    return bool(KEY)


class FdbError(Exception):
    def __init__(self, status, msg, path=""):
        super().__init__(f"{status} {path}: {msg[:300]}")
        self.status = status


def get(path: str, params: dict | None = None) -> dict:
    if not configured():
        raise FdbError(0, "FOREVERDB_API_KEY not set", path)
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    r = urllib.request.Request(url)
    r.add_header("X-API-Key", KEY)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "application/json")
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(r, timeout=60).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                continue
            raise FdbError(e.code, e.read().decode(errors="replace"), path)
    raise FdbError(429, "rate limited", path)


def profession_all(slug: str) -> dict:
    """Walk every page of /professions/{slug} and merge the recipe list.
    Returns {"data": <first page data with recipes merged>, "pages": n}."""
    first = get(f"/professions/{slug}", {"limit": 100, "offset": 0})
    data = first.get("data") or first
    recipes = _recipes_of(data)
    total = (first.get("meta") or {}).get("total")
    pages = 1
    offset = 100
    while total is not None and len(recipes) < int(total) and pages < 40:
        nxt = get(f"/professions/{slug}", {"limit": 100, "offset": offset})
        more = _recipes_of(nxt.get("data") or nxt)
        if not more:
            break
        recipes.extend(more)
        offset += 100
        pages += 1
        time.sleep(2.1)  # 30/min free tier
    _set_recipes(data, recipes)
    return {"data": data, "pages": pages, "meta": first.get("meta")}


def _recipes_of(d):
    if isinstance(d, dict):
        for k in ("recipes", "items", "results"):
            if isinstance(d.get(k), list):
                return list(d[k])
    if isinstance(d, list):
        return list(d)
    return []


def _set_recipes(d, recipes):
    if isinstance(d, dict):
        for k in ("recipes", "items", "results"):
            if k in d:
                d[k] = recipes
                return
        d["recipes"] = recipes


# ---- normalisation (tolerant of unknown field names) ------------------------
def _pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def norm_name(s: str) -> str:
    import re
    s = (s or "").lower()
    s = re.sub(r"^(pattern|recipe|formula|schematic|plans|manual|design):\s*", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def normalize_recipe(r: dict) -> dict:
    """Flatten one ForeverDB recipe into what the ledger needs; keep the raw object too."""
    makes = _pick(r, "makes", "creates", "product", "crafts", "result", default={}) or {}
    if isinstance(makes, list):
        makes = makes[0] if makes else {}
    skill = _pick(r, "skill", "skillColours", "skillColors", "colours", "colors", "skillLevels", default={}) or {}
    if not isinstance(skill, dict):
        skill = {"orange": skill}
    learned = _pick(r, "learnedFrom", "learned_from", "sources", default={}) or {}
    srcs = learned.get("sources") if isinstance(learned, dict) else learned
    if not isinstance(srcs, list):
        srcs = []
    out = {
        "fdb_id": _pick(r, "id", "recipeId", "spellId"),
        "spell_id": _int(_pick(r, "spellId", "spell_id")),
        "name": _pick(r, "name", "recipeName", "spellName", default=""),
        "item_id": _int(_pick(makes, "itemId", "item_id", "id")),
        "item_name": _pick(makes, "name", "itemName", default=""),
        "recipe_item_id": _int(_pick(_pick(r, "recipeItem", "taughtBy", "item", default={}) or {}, "id", "itemId")),
        "orange": _int(_pick(skill, "orange", "min", "learn", "learnedAt", "minSkill")),
        "yellow": _int(_pick(skill, "yellow")),
        "green": _int(_pick(skill, "green")),
        "grey": _int(_pick(skill, "grey", "gray")),
        "category": _pick(r, "category", "categoryName", default=""),
        "changed": bool(_pick(r, "changed", "changedInForever", "foreverChanges", default=False)),
        "new": bool(_pick(r, "new", "newInForever", default=False)),
        "reagents": _pick(r, "reagents", "materials", default=[]) or [],
        "sources": [normalize_source(s) for s in srcs],
    }
    return out


def normalize_source(s: dict) -> dict:
    t = str(_pick(s, "type", "kind", "sourceType", default="")).lower()
    if "train" in t: t = "trainer"
    elif "vend" in t or "merchant" in t: t = "vendor"
    elif "quest" in t: t = "quest"
    elif "drop" in t or "loot" in t or "creature" in t or "npc" in t and "drop" in json.dumps(s).lower(): t = "drop"
    elif "fish" in t: t = "fishing"
    elif "object" in t or "chest" in t: t = "object"
    npc = _pick(s, "npc", "creature", "trainer", "vendor", "giver", default={}) or {}
    loc = _pick(s, "location", "zone", "area", default=None)
    if isinstance(loc, dict):
        loc_name = _pick(loc, "name", "zone", "zoneName", default="")
    else:
        loc_name = loc or _pick(npc, "zone", "zoneName", "location", default="") or ""
    price = _pick(s, "price", "cost", "vendorPrice", "costCopper", default=None)
    if isinstance(price, dict):
        price = _pick(price, "copper", "perItem", "each", "total", "perPurchase")
    return {
        "type": t or "unknown",
        "name": _pick(s, "name", "npcName", "questName", "title", default="") or _pick(npc, "name", default=""),
        "zone": loc_name if isinstance(loc_name, str) else str(loc_name),
        "faction": (str(_pick(s, "faction", "side", default="") or _pick(npc, "faction", default="") or "")).lower(),
        "skill": _int(_pick(s, "skill", "requiredSkill", "skillRequired", "minSkill")),
        "price": _int(price),
        "limited": bool(_pick(s, "limited", "limitedStock", "isLimited", default=False)),
        "stock": _int(_pick(s, "stock", "maxStock")),
        "drop_chance": _pick(s, "dropChance", "chance", "dropRate", "rate"),
        "rep": _pick(s, "reputation", "repDiscountFaction", "discountFaction", default=None),
        "extra_cost": _pick(s, "extraCost", "extraCosts", "merchantsFavor", default=None),
        "seen": _pick(s, "seenInForever", "seen", "verified", default=None),
        "quest_id": _int(_pick(s, "questId", "quest_id")),
        "npc_id": _int(_pick(npc, "id", "npcId")),
    }
