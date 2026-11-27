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
    """Flatten one ForeverDB recipe (live shape, 2026-11-27) into what the ledger needs."""
    makes = r.get("makes") or {}
    skill = r.get("skill") or {}
    lf = r.get("learnedFrom") or {}
    src = lf.get("sources") or {}
    if not isinstance(src, dict):
        src = {}
    cat = r.get("category") or {}
    ritem = src.get("recipeItem") or (lf.get("recipeItems") or [{}])[0] or {}
    trainer_cost = _int((lf.get("trainer") or {}).get("cost")) or _int(src.get("trainerCost"))
    sources = []
    sources += _npc_sources(src.get("trainers"), "trainer", trainer_cost, src)
    sources += _npc_sources(src.get("vendors"), "vendor", None, src)
    for d in src.get("drops") or []:
        kind = str(d.get("kind") or "drop")
        lv = d.get("creatureLevels")
        sources.append({
            "type": "drop", "kind": kind, "name": d.get("from") or ("World drop" if "world" in kind else kind.title()),
            "zone": "", "faction": "", "skill": None, "price": None, "limited": False, "stock": None,
            "drop_chance": d.get("chancePercent"), "rep": None, "extra_cost": None, "seen": None,
            "quest_id": None, "npc_id": _int(d.get("npcId")), "levels": lv if isinstance(lv, list) else None,
        })
    for q in src.get("quests") or []:
        sources.append({
            "type": "quest", "name": q.get("name") or "", "zone": "", "faction": str(q.get("faction") or "").lower(),
            "skill": None, "price": None, "limited": False, "stock": None, "drop_chance": None, "rep": None,
            "extra_cost": None, "seen": q.get("inForeverClient"), "quest_id": _int(q.get("questId")), "npc_id": None,
        })
    if lf.get("automatic") or "auto" in (src.get("ways") or []):
        sources.append({"type": "auto", "name": "learned with the profession rank", "zone": "", "faction": "",
                        "skill": _int(src.get("learnAtSkill")), "price": None, "limited": False, "stock": None,
                        "drop_chance": None, "rep": None, "extra_cost": None, "seen": None, "quest_id": None, "npc_id": None})
    return {
        "fdb_id": r.get("spellId") or r.get("id"),
        "spell_id": _int(r.get("spellId")),
        "name": r.get("name") or "",
        "item_id": _int(makes.get("itemId")),
        "item_name": makes.get("name") or "",
        "item_count": makes.get("count"),
        "recipe_item_id": _int(ritem.get("itemId")),
        "recipe_item_name": ritem.get("name") or "",
        "orange": _int(skill.get("orange")) or _int(src.get("learnAtSkill")),
        "yellow": _int(skill.get("yellow")), "green": _int(skill.get("green")), "grey": _int(skill.get("grey")),
        "learn_at": _int(src.get("learnAtSkill")),
        "required_level": _int(r.get("requiredLevel")),
        "category": cat.get("name") if isinstance(cat, dict) else str(cat or ""),
        "changed": bool(r.get("changedFromClassic")),
        "changed_fields": r.get("changedFromClassic") or [],
        "new": bool(r.get("newInForever")),
        "leftover_of": r.get("leftoverOf"),
        "speculation": bool(r.get("speculation")),
        "speculation_reasons": r.get("speculationReasons") or [],
        "camp_tier": r.get("campTier"),
        "same_name": len(r.get("sameNameRecipes") or []),
        "ways": src.get("ways") or [],
        "classic_vendors": _int(lf.get("classicVendors")) or 0,
        "reagents": [{"id": x.get("itemId"), "name": x.get("name"), "qty": x.get("count")} for x in (r.get("reagents") or [])],
        "url": r.get("url"),
        "sources": sources,
    }


def _npc_sources(groups: dict | None, typ: str, cost, src: dict) -> list:
    """trainers/vendors come as {"alliance": [...], "horde": [...]}; an NPC with faction "Both" is listed
    under each. Collapse to one record per NPC with faction alliance / horde / both."""
    out, byid = [], {}
    for fac in ("alliance", "horde"):
        for n in (groups or {}).get(fac) or []:
            key = n.get("npcId") or (n.get("name"), n.get("zone"))
            if key in byid:
                byid[key]["faction"] = "both"
                continue
            rep = n.get("reputationFaction")
            rec = {
                "type": typ, "name": n.get("name") or "", "title": n.get("title") or "",
                "zone": n.get("zone") or "", "x": n.get("x"), "y": n.get("y"), "map_id": n.get("mapId"),
                "faction": "both" if str(n.get("faction") or "").lower() == "both" else fac,
                "skill": _int(src.get("learnAtSkill")),
                "price": _int(n.get("price")) if typ == "vendor" else cost,
                "limited": bool(n.get("limitedStock")), "stock": _int(n.get("limitedStockCount")),
                "not_on_list": bool(n.get("notOnListPlayersSaw")),
                "drop_chance": None,
                "rep": rep.get("name") if isinstance(rep, dict) else rep,
                "extra_cost": n.get("costs") or None,
                "seen": n.get("seenInForever"),
                "quest_id": None, "npc_id": _int(n.get("npcId")),
            }
            byid[key] = rec
            out.append(rec)
    return out
