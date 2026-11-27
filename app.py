"""Völundr — WoW Forever crafting ledger.

Known recipes per character come from the Battle.net profile API (profile-forever-us, realmless
"{first}-{surname}" characters, level 10+). The recipe universe comes from the static skill-tier
docs. Where/how to learn each missing recipe comes from ForeverDB.net. Everything is cached in
one SQLite file; Blizzard docs are immutable per build, ForeverDB rows are refreshed weekly.
"""
import os, json, time, sqlite3, threading, traceback
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import blizzard as bz
import foreverdb as fdb
import questie

DB = os.environ.get("VOLUNDR_DB", "/data/volundr.db")
SYNC_HOURS = float(os.environ.get("VOLUNDR_SYNC_HOURS", "6"))
FDB_TTL_DAYS = float(os.environ.get("VOLUNDR_FDB_TTL_DAYS", "7"))
QUESTIE_TTL_DAYS = float(os.environ.get("VOLUNDR_QUESTIE_TTL_DAYS", "7"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "0.1.0"

app = FastAPI(title="Völundr")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# zone map art exported from the game client lives next to the DB (never in the repo)
MAPS_DIR = os.path.join(os.path.dirname(DB) or ".", "maps")
if os.path.isdir(MAPS_DIR):
    app.mount("/maps", StaticFiles(directory=MAPS_DIR), name="maps")
_maps_cache = {"t": 0, "idx": None}


def maps_index() -> dict:
    """{"by_area": {area: {name,file,w,h,uimap}}, "by_uimap": {uimap: area}} or empty."""
    p = os.path.join(MAPS_DIR, "index.json")
    if not os.path.exists(p):
        return {"by_area": {}, "by_uimap": {}}
    mt = os.path.getmtime(p)
    if _maps_cache["idx"] is None or mt != _maps_cache["t"]:
        raw = json.load(open(p, encoding="utf-8"))
        _maps_cache["idx"] = {"by_area": {int(k): v for k, v in raw.get("by_area", {}).items()},
                              "by_uimap": {int(k): int(v) for k, v in raw.get("by_uimap", {}).items()}}
        _maps_cache["t"] = mt
    return _maps_cache["idx"]


def map_for(area=None, uimap=None):
    idx = maps_index()
    if area is None and uimap is not None:
        area = idx["by_uimap"].get(int(uimap))
    if area is None:
        return None
    v = idx["by_area"].get(int(area))
    if not v:
        return None
    return {"area": int(area), "uimap": v.get("uimap"), "name": v.get("name"), "file": "/maps/" + v["file"], "w": v.get("w"), "h": v.get("h")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chars(
  slug TEXT PRIMARY KEY, name TEXT, surname TEXT, display TEXT, cls TEXT, race TEXT, level INT,
  guild TEXT, faction TEXT, ruleset TEXT, avatar TEXT, source TEXT, last_sync REAL, last_error TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS char_profs(
  slug TEXT, prof_id INT, prof_name TEXT, prof_slug TEXT, tier_id INT, tier_name TEXT,
  skill INT, max_skill INT, PRIMARY KEY(slug, prof_id, tier_id));
CREATE TABLE IF NOT EXISTS char_recipes(slug TEXT, prof_id INT, recipe_id INT, name TEXT, PRIMARY KEY(slug, recipe_id));
CREATE TABLE IF NOT EXISTS bnet_profs(prof_id INT PRIMARY KEY, name TEXT, slug TEXT, tiers TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS bnet_recipes(
  recipe_id INT PRIMARY KEY, prof_id INT, prof_slug TEXT, tier_id INT, tier_name TEXT, category TEXT,
  name TEXT, nname TEXT, item_id INT, item_name TEXT, reagents TEXT, detail_fetched REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS fdb_recipes(
  key TEXT PRIMARY KEY, prof_slug TEXT, name TEXT, nname TEXT, item_id INT, spell_id INT,
  orange INT, yellow INT, green INT, grey INT, category TEXT, sources TEXT, norm TEXT, raw TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS npc_locations(
  npc_id INT PRIMARY KEY, name TEXT, sub TEXT, min_lvl INT, max_lvl INT, area INT, zone TEXT, x REAL, y REAL,
  spawns INT, cluster INT, zones TEXT, points TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_bnet_prof ON bnet_recipes(prof_slug);
CREATE INDEX IF NOT EXISTS ix_fdb_prof ON fdb_recipes(prof_slug);
"""

_lock = threading.Lock()


@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(npc_locations)")}
        if "points" not in cols:
            con.execute("ALTER TABLE npc_locations ADD COLUMN points TEXT")


def meta_get(k, default=None):
    with db() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def meta_set(k, v):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


def prof_slug(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "-")


# ---- sync: character -------------------------------------------------------
def sync_char(slug: str) -> dict:
    """Pull profile + professions (known recipes) for one character. Falls back to the armory
    page for the card when the API client is not configured."""
    slug = slug.lower().strip()
    now = time.time()
    try:
        if bz.configured():
            prof = bz.character(slug)
            ns = bz.static_ns_from_profile(prof)
            if ns and ns != meta_get("static_ns"):
                meta_set("static_ns", ns)
            row = {
                "name": prof.get("name"), "surname": prof.get("surname"),
                "cls": (prof.get("character_class") or {}).get("name"),
                "race": (prof.get("race") or {}).get("name"), "level": prof.get("level"),
                "guild": (prof.get("guild") or {}).get("name"),
                "faction": ((prof.get("faction") or {}).get("name") or "").lower(),
                "ruleset": (prof.get("ruleset") or {}).get("name") if isinstance(prof.get("ruleset"), dict) else prof.get("ruleset"),
                "avatar": None, "source": "api", "raw": json.dumps(prof)[:20000],
            }
            try:
                med = bz.media(slug)
                for a in med.get("assets", []):
                    if a.get("key") == "avatar":
                        row["avatar"] = a.get("value")
            except bz.BnetError:
                pass
            pdoc = bz.professions(slug)
            profs, known = [], []
            for group in ("primaries", "secondaries"):
                for p in pdoc.get(group, []) or []:
                    pr = p.get("profession") or {}
                    pid, pname = pr.get("id"), pr.get("name")
                    tiers = p.get("tiers") or [{}]
                    for t in tiers:
                        tier = t.get("tier") or {}
                        profs.append((slug, pid, pname, prof_slug(pname), tier.get("id") or 0, tier.get("name") or "",
                                      t.get("skill_points"), t.get("max_skill_points")))
                        for r in t.get("known_recipes", []) or []:
                            known.append((slug, pid, r.get("id"), r.get("name")))
                    # some docs carry skill on the profession object instead of tiers
                    if not p.get("tiers") and p.get("skill_points") is not None:
                        profs[-1] = (slug, pid, pname, prof_slug(pname), 0, "", p.get("skill_points"), p.get("max_skill_points"))
            with db() as con:
                con.execute("DELETE FROM char_profs WHERE slug=?", (slug,))
                con.execute("DELETE FROM char_recipes WHERE slug=?", (slug,))
                con.executemany("INSERT OR REPLACE INTO char_profs VALUES(?,?,?,?,?,?,?,?)", profs)
                con.executemany("INSERT OR REPLACE INTO char_recipes VALUES(?,?,?,?)", known)
                # register the professions so the static sync knows which universes to pull
                con.execute("INSERT OR IGNORE INTO bnet_profs(prof_id,name,slug,tiers,fetched) "
                            "SELECT DISTINCT prof_id, prof_name, prof_slug, NULL, 0 FROM char_profs WHERE slug=?", (slug,))
        else:
            a = bz.armory_profile(slug)
            row = {
                "name": a.get("name"), "surname": a.get("surname"),
                "cls": (a.get("class") or {}).get("name"), "race": (a.get("race") or {}).get("name"),
                "level": a.get("level"), "guild": (a.get("guild") or {}).get("name"),
                "faction": ((a.get("faction") or {}).get("slug") or "").lower(),
                "ruleset": (a.get("ruleset") or {}).get("name") if isinstance(a.get("ruleset"), dict) else a.get("ruleset"),
                "avatar": (a.get("avatar") or {}).get("url"), "source": "armory", "raw": json.dumps(a)[:20000],
            }
        row["display"] = f"{row['name']} {row['surname']}".strip() if row.get("name") else slug
        with db() as con:
            con.execute("""INSERT INTO chars(slug,name,surname,display,cls,race,level,guild,faction,ruleset,avatar,source,last_sync,last_error,raw)
                           VALUES(:slug,:name,:surname,:display,:cls,:race,:level,:guild,:faction,:ruleset,:avatar,:source,:now,NULL,:raw)
                           ON CONFLICT(slug) DO UPDATE SET name=excluded.name,surname=excluded.surname,display=excluded.display,cls=excluded.cls,
                           race=excluded.race,level=excluded.level,guild=excluded.guild,faction=excluded.faction,ruleset=excluded.ruleset,
                           avatar=COALESCE(excluded.avatar,chars.avatar),source=excluded.source,last_sync=:now,last_error=NULL,raw=excluded.raw""",
                        dict(row, slug=slug, now=now))
        return {"ok": True, "slug": slug, "source": row["source"]}
    except Exception as e:  # noqa: BLE001 — recorded on the row, surfaced in the UI
        err = f"{type(e).__name__}: {e}"[:500]
        with db() as con:
            con.execute("INSERT INTO chars(slug,display,last_error) VALUES(?,?,?) ON CONFLICT(slug) DO UPDATE SET last_error=excluded.last_error",
                        (slug, slug, err))
        return {"ok": False, "slug": slug, "error": err}


# ---- sync: static universe + ForeverDB sources --------------------------------
SYNC = {"running": False, "phase": "", "detail": "", "started": 0, "done": 0, "errors": []}


def _ns():
    return meta_get("static_ns") or bz.NS_STATIC


def sync_static_profession(pid: int) -> dict:
    """Blizzard: profession -> skill tiers -> categories -> recipes; then each recipe's doc for the
    crafted item (the join key to ForeverDB). Immutable per build, so each recipe doc is fetched once."""
    ns = _ns()
    pdoc = bz.profession(pid, ns)
    name = pdoc.get("name")
    slug = prof_slug(name)
    tiers = pdoc.get("skill_tiers") or []
    with db() as con:
        con.execute("INSERT OR REPLACE INTO bnet_profs VALUES(?,?,?,?,?)", (pid, name, slug, json.dumps(tiers), time.time()))
    n = 0
    for t in tiers:
        SYNC["detail"] = f"{name}: {t.get('name')}"
        tdoc = bz.skill_tier(pid, t["id"], ns)
        rows = []
        for cat in tdoc.get("categories", []) or []:
            for r in cat.get("recipes", []) or []:
                rows.append((r["id"], pid, slug, t["id"], t.get("name"), cat.get("name"), r.get("name"), fdb.norm_name(r.get("name"))))
        with db() as con:
            con.executemany("""INSERT INTO bnet_recipes(recipe_id,prof_id,prof_slug,tier_id,tier_name,category,name,nname)
                               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(recipe_id) DO UPDATE SET tier_id=excluded.tier_id,
                               tier_name=excluded.tier_name,category=excluded.category,name=excluded.name,nname=excluded.nname""", rows)
        n += len(rows)
    # recipe details (crafted item) — only the ones never fetched
    with db() as con:
        todo = [r["recipe_id"] for r in con.execute("SELECT recipe_id FROM bnet_recipes WHERE prof_id=? AND detail_fetched IS NULL", (pid,))]
    for i, rid in enumerate(todo):
        SYNC["detail"] = f"{name}: recipe details {i+1}/{len(todo)}"
        try:
            d = bz.recipe(rid, ns)
            ci = d.get("crafted_item") or (d.get("alliance_crafted_item") or {})
            reag = [{"id": (x.get("reagent") or {}).get("id"), "name": (x.get("reagent") or {}).get("name"), "qty": x.get("quantity")}
                    for x in d.get("reagents", []) or []]
            with db() as con:
                con.execute("UPDATE bnet_recipes SET item_id=?, item_name=?, reagents=?, detail_fetched=?, raw=? WHERE recipe_id=?",
                            (ci.get("id"), ci.get("name"), json.dumps(reag), time.time(), json.dumps(d)[:4000], rid))
        except bz.BnetError as e:
            SYNC["errors"].append(f"recipe {rid}: {e}")
            with db() as con:
                con.execute("UPDATE bnet_recipes SET detail_fetched=? WHERE recipe_id=?", (time.time(), rid))
        time.sleep(0.05)
    return {"profession": name, "recipes": n, "details_fetched": len(todo)}


def sync_fdb_profession(slug: str) -> dict:
    """ForeverDB: every recipe of a profession with its sources, normalised + raw."""
    SYNC["detail"] = f"ForeverDB: {slug}"
    res = fdb.profession_all(slug)
    data = res["data"]
    recipes = fdb._recipes_of(data)
    rows = []
    for r in recipes:
        n = fdb.normalize_recipe(r)
        key = f"{slug}:{n['fdb_id'] or fdb.norm_name(n['name'])}"
        rows.append((key, slug, n["name"], fdb.norm_name(n["name"]), n["item_id"], n["spell_id"], n["orange"], n["yellow"], n["green"],
                     n["grey"], n["category"], json.dumps(n["sources"]), json.dumps(n), json.dumps(r), time.time()))
    with db() as con:
        con.execute("DELETE FROM fdb_recipes WHERE prof_slug=?", (slug,))
        con.executemany("INSERT OR REPLACE INTO fdb_recipes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # profession-level extras (trainers list, ranks) kept for the UI
    extras = {k: v for k, v in data.items() if k not in ("recipes", "items", "results")} if isinstance(data, dict) else {}
    meta_set(f"fdb_prof:{slug}", json.dumps(extras)[:20000])
    meta_set(f"fdb_fetched:{slug}", time.time())
    return {"profession": slug, "recipes": len(rows), "pages": res["pages"]}


def sync_questie() -> dict:
    """QuestieDB Forever NPC spawns -> npc_locations (drop sources get a zone and a /way)."""
    SYNC["detail"] = "QuestieDB: downloading NPC spawns"
    ddir = os.path.join(os.path.dirname(DB) or ".", "questie")
    os.makedirs(ddir, exist_ok=True)
    npc_lua = questie.fetch(questie.NPC_URL)
    zone_lua = questie.fetch(questie.ZONE_URL)
    with open(os.path.join(ddir, "foreverNpcDB.lua"), "w", encoding="utf-8") as fh:
        fh.write(npc_lua)
    with open(os.path.join(ddir, "areaIdToUiMapId.lua"), "w", encoding="utf-8") as fh:
        fh.write(zone_lua)
    SYNC["detail"] = "QuestieDB: parsing"
    rows = questie.build_locations(questie.parse_npcs(npc_lua), questie.parse_zone_names(zone_lua))
    with db() as con:
        con.execute("DELETE FROM npc_locations")
        con.executemany("INSERT OR REPLACE INTO npc_locations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    meta_set("area_uimap", json.dumps(questie.parse_area_uimap(zone_lua)))
    meta_set("questie_fetched", time.time())
    return {"npcs": len(rows)}


def run_static_sync(prof_slugs: list[str] | None, force_fdb=False):
    if SYNC["running"]:
        return
    SYNC.update(running=True, phase="start", detail="", started=time.time(), done=0, errors=[])
    try:
        with db() as con:
            profs = [dict(r) for r in con.execute("SELECT * FROM bnet_profs")]
        if prof_slugs:
            profs = [p for p in profs if p["slug"] in prof_slugs]
        if bz.configured():
            SYNC["phase"] = "blizzard static"
            for p in profs:
                try:
                    sync_static_profession(p["prof_id"])
                except Exception as e:  # noqa: BLE001
                    SYNC["errors"].append(f"static {p['slug']}: {e}")
        if fdb.configured():
            SYNC["phase"] = "foreverdb"
            for p in profs:
                last = float(meta_get(f"fdb_fetched:{p['slug']}", 0) or 0)
                if not force_fdb and time.time() - last < FDB_TTL_DAYS * 86400:
                    continue
                try:
                    sync_fdb_profession(p["slug"])
                except Exception as e:  # noqa: BLE001
                    SYNC["errors"].append(f"foreverdb {p['slug']}: {e}")
        last_q = float(meta_get("questie_fetched", 0) or 0)
        if force_fdb or time.time() - last_q > QUESTIE_TTL_DAYS * 86400:
            SYNC["phase"] = "questie"
            try:
                sync_questie()
            except Exception as e:  # noqa: BLE001
                SYNC["errors"].append(f"questie: {e}")
        SYNC["phase"] = "done"
    finally:
        SYNC.update(running=False, done=time.time())


def scheduler():
    """Characters every SYNC_HOURS; static + ForeverDB once a day (TTL-gated inside)."""
    time.sleep(20)
    last_static = 0
    while True:
        try:
            with db() as con:
                slugs = [r["slug"] for r in con.execute("SELECT slug FROM chars")]
            for s in slugs:
                sync_char(s)
            if time.time() - last_static > 86400:
                run_static_sync(None)
                last_static = time.time()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(SYNC_HOURS * 3600)


# ---- ledger -----------------------------------------------------------------
LEARN = ("trainer", "vendor", "auto")
FARM = ("drop", "quest", "fishing", "object")


def faction_ok(src, faction):
    f = (src.get("faction") or "").lower()
    return (not f) or f in ("both", "neutral", "any") or (faction and faction in f) or (f in faction if faction else False)


def classify(rec: dict, skill, max_skill, faction: str) -> dict:
    srcs = [s for s in rec.get("sources", []) if faction_ok(s, faction)]
    learn = [s for s in srcs if s["type"] in LEARN]
    farm = [s for s in srcs if s["type"] in FARM]
    reqs = [s["skill"] for s in learn if s.get("skill")]
    req = rec.get("orange") or (min(reqs) if reqs else None)
    if rec.get("speculation"):
        g = "unknown"          # ForeverDB: needs an item not in the Forever client — may not be craftable
    elif learn:
        if req is None or (skill is not None and req <= skill):
            g = "now"
        else:
            g = "later"
    elif farm:
        g = "farm"
    else:
        g = "unknown"
    needs_rank = bool(req and max_skill and req > max_skill)
    other_faction = bool(rec.get("sources")) and not srcs
    return {"group": g, "req": req, "needs_rank": needs_rank, "sources": srcs, "learn": learn, "farm": farm,
            "other_faction": other_faction}


def npc_lookup(ids: set) -> dict:
    if not ids:
        return {}
    with db() as con:
        q = ",".join("?" * len(ids))
        return {r["npc_id"]: dict(r) for r in con.execute(f"SELECT * FROM npc_locations WHERE npc_id IN ({q})", list(ids))}


def enrich_sources(rows: list):
    """Give drop sources (and anything else lacking coordinates) a zone and x/y from QuestieDB."""
    ids = {s.get("npc_id") for r in rows for s in r.get("sources", []) if s.get("npc_id") and (s.get("x") is None or not s.get("zone"))}
    loc = npc_lookup(ids)
    for r in rows:
        for s in r.get("sources", []):
            if s.get("map_id") and s.get("x") is not None:          # ForeverDB trainers/vendors carry a UiMap id
                m = map_for(uimap=s["map_id"])
                if m:
                    s["map"] = m
                    s["points"] = [[s["x"], s["y"]]]
            L = loc.get(s.get("npc_id"))
            if not L:
                continue
            if L.get("area") and not s.get("map"):
                m = map_for(area=L["area"])
                if m:
                    s["map"] = m
                    try:
                        s["points"] = json.loads(L.get("points") or "[]")
                    except ValueError:
                        s["points"] = []
            if not s.get("zone"):
                s["zone"] = L["zone"]
            if s.get("x") is None and L["x"] is not None:
                s["x"], s["y"] = L["x"], L["y"]
            s["spawns"] = L["spawns"] or None
            s["zones"] = json.loads(L["zones"] or "[]")
            if L.get("min_lvl") and not s.get("levels"):
                s["levels"] = [L["min_lvl"], L["max_lvl"]]


def ledger(slug: str, pslug: str) -> dict:
    with db() as con:
        ch = con.execute("SELECT * FROM chars WHERE slug=?", (slug,)).fetchone()
        if not ch:
            raise HTTPException(404, "unknown character")
        cp = [dict(r) for r in con.execute("SELECT * FROM char_profs WHERE slug=? AND prof_slug=?", (slug, pslug))]
        known = {r["recipe_id"]: r["name"] for r in con.execute(
            "SELECT recipe_id,name FROM char_recipes WHERE slug=? AND prof_id IN (SELECT prof_id FROM bnet_profs WHERE slug=?)", (slug, pslug))}
        universe = [dict(r) for r in con.execute("SELECT * FROM bnet_recipes WHERE prof_slug=? ORDER BY name", (pslug,))]
        fdb_rows = [dict(r) for r in con.execute("SELECT * FROM fdb_recipes WHERE prof_slug=?", (pslug,))]
        fdb_fetched = meta_get(f"fdb_fetched:{pslug}")
    skill = max([p["skill"] or 0 for p in cp], default=None) if cp else None
    max_skill = max([p["max_skill"] or 0 for p in cp], default=None) if cp else None
    faction = (ch["faction"] or "").lower()
    by_item = {r["item_id"]: r for r in fdb_rows if r["item_id"]}
    by_name = {r["nname"]: r for r in fdb_rows if r["nname"]}
    out, matched = [], 0
    universe_ids = {u["recipe_id"] for u in universe}
    # known recipes that the static universe does not list (e.g. universe not synced yet)
    for rid, name in known.items():
        if rid not in universe_ids:
            universe.append({"recipe_id": rid, "name": name, "nname": fdb.norm_name(name), "item_id": None, "item_name": None,
                             "category": "", "tier_name": "", "reagents": None})
    for u in universe:
        f = by_item.get(u.get("item_id")) or by_name.get(u.get("nname")) or by_name.get(fdb.norm_name(u.get("item_name") or ""))
        norm = json.loads(f["norm"]) if f else {}
        if f:
            matched += 1
        row = {
            "recipe_id": u["recipe_id"], "name": u["name"], "item_id": u.get("item_id"), "item_name": u.get("item_name"),
            "category": norm.get("category") or u.get("category") or "", "tier": u.get("tier_name") or "",
            "bnet_category": u.get("category") or "",
            "known": u["recipe_id"] in known,
            "orange": norm.get("orange"), "yellow": norm.get("yellow"), "green": norm.get("green"), "grey": norm.get("grey"),
            "reagents": json.loads(u["reagents"]) if u.get("reagents") else (norm.get("reagents") or []),
            "changed": norm.get("changed", False), "changed_fields": norm.get("changed_fields", []), "new": norm.get("new", False),
            "required_level": norm.get("required_level"), "recipe_item": norm.get("recipe_item_name") or "",
            "ways": norm.get("ways", []), "classic_vendors": norm.get("classic_vendors", 0), "url": norm.get("url"),
            "speculation": bool(norm.get("speculation")), "speculation_reasons": norm.get("speculation_reasons", []),
            "sources": norm.get("sources", []), "fdb": bool(f),
        }
        c = classify(row, skill, max_skill, faction)
        row.update(group="known" if row["known"] else c["group"], req=c["req"], needs_rank=c["needs_rank"], sources=c["sources"],
                   other_faction=c["other_faction"])
        out.append(row)
    enrich_sources(out)
    order = {"now": 0, "later": 1, "farm": 2, "unknown": 3, "known": 4}
    out.sort(key=lambda r: (order[r["group"]], r["req"] or 0, r["name"]))
    counts = {g: sum(1 for r in out if r["group"] == g) for g in order}
    return {
        "char": dict(ch, raw=None), "profession": pslug, "skill": skill, "max_skill": max_skill,
        "tiers": cp, "counts": counts, "total": len(out), "known": len(known), "fdb_matched": matched,
        "fdb_total": len(fdb_rows), "fdb_fetched": float(fdb_fetched) if fdb_fetched else None,
        "fdb_extras": json.loads(meta_get(f"fdb_prof:{pslug}", "{}") or "{}"),
        "recipes": out,
    }


def next_session(slug: str) -> dict:
    with db() as con:
        ch = con.execute("SELECT * FROM chars WHERE slug=?", (slug,)).fetchone()
        if not ch:
            raise HTTPException(404, "unknown character")
        pslugs = [r["prof_slug"] for r in con.execute("SELECT DISTINCT prof_slug FROM char_profs WHERE slug=?", (slug,))]
    farm = []
    # candidate stops: every trainer/vendor that offers a learnable recipe; one recipe may have many
    cand, offers = {}, {}   # stop key -> stop ; recipe key -> [(stop key, source)]
    for ps in pslugs:
        L = ledger(slug, ps)
        for r in L["recipes"]:
            if r["group"] == "now":
                rk = (ps, r["recipe_id"])
                for s in r["sources"]:
                    if s["type"] in ("trainer", "vendor"):
                        k = f"{s['type']}|{s['name']}|{s['zone']}"
                        st = cand.setdefault(k, {"type": s["type"], "name": s["name"], "title": s.get("title") or "", "zone": s["zone"],
                                                 "x": s.get("x"), "y": s.get("y"), "seen": bool(s.get("seen")), "map": s.get("map"),
                                                 "items": [], "cost": 0, "limited": False})
                        offers.setdefault(rk, []).append((k, s, r, ps))
            elif r["group"] == "farm":
                for s in r["sources"]:
                    farm.append({"recipe": r["name"], "prof": ps, "type": s["type"], "kind": s.get("kind"), "name": s["name"],
                                 "zone": s.get("zone") or "", "x": s.get("x"), "y": s.get("y"), "spawns": s.get("spawns"),
                                 "map": s.get("map"), "points": s.get("points"),
                                 "levels": s.get("levels"), "zones": s.get("zones"), "drop_chance": s.get("drop_chance"),
                                 "chance": s.get("drop_chance"), "req": r["req"], "npc_id": s.get("npc_id"), "quest_id": s.get("quest_id")})
    def chance(x):
        try:
            return float(str(x.get("chance") or "0").rstrip("%"))
        except ValueError:
            return 0.0
    farm.sort(key=lambda x: (-chance(x), x["recipe"]))
    # greedy set cover: fewest stops that teach everything; prefer NPCs seen in Forever, then capitals
    CAPITALS = ("Stormwind City", "Ironforge", "Darnassus", "Orgrimmar", "Undercity", "Thunder Bluff")
    uncovered = set(offers)
    stops = []
    while uncovered:
        best, best_score = None, None
        for k, st in cand.items():
            cov = [rk for rk in uncovered if any(o[0] == k for o in offers[rk])]
            if not cov:
                continue
            score = (len(cov), st["seen"], st["zone"] in CAPITALS, st["type"] == "trainer", -sum((o[1].get("price") or 0) for rk in cov for o in offers[rk] if o[0] == k))
            if best_score is None or score > best_score:
                best, best_score = k, score
        if best is None:
            break
        st = dict(cand[best], items=[], cost=0, limited=False)
        for rk in sorted(uncovered, key=lambda x: x[1]):
            os_ = [o for o in offers[rk] if o[0] == best]
            if not os_:
                continue
            _, s, r, ps = os_[0]
            costs = [c for c in (s.get("extra_cost") or []) if isinstance(c, dict)]
            st["items"].append({"recipe": r["name"], "prof": ps, "price": s.get("price"), "limited": s.get("limited"),
                                "skill": s.get("skill") or r["req"], "alternatives": len(offers[rk]) - 1,
                                "costs": [{"name": c.get("name"), "amount": c.get("amount") or c.get("count")} for c in costs]})
            st["cost"] += s.get("price") or 0
            for c in costs:
                cur = st.setdefault("currency", {})
                cur[c.get("name") or "?"] = cur.get(c.get("name") or "?", 0) + (c.get("amount") or c.get("count") or 0)
            st["limited"] = st["limited"] or bool(s.get("limited"))
            uncovered.discard(rk)
        st["items"].sort(key=lambda i: (i["prof"], i["skill"] or 0, i["recipe"]))
        stops.append(st)
    # by zone: every stop and every farm target with a known zone, for the route-by-area view
    zones = {}
    for st in stops:
        z = zones.setdefault(st["zone"] or "Unknown zone", {"zone": st["zone"] or "Unknown zone", "stops": [], "farm": []})
        z["stops"].append(st)
    seen_farm = set()
    for f_ in farm:
        key = (f_["name"], f_["zone"])
        if key in seen_farm:
            for z in zones.values():
                for t in z["farm"]:
                    if (t["name"], t["zone"]) == key:
                        t["recipes"].append({"recipe": f_["recipe"], "prof": f_["prof"], "chance": f_["chance"], "req": f_["req"]})
            continue
        seen_farm.add(key)
        z = zones.setdefault(f_["zone"] or "Unknown zone", {"zone": f_["zone"] or "Unknown zone", "stops": [], "farm": []})
        z["farm"].append(dict(f_, recipes=[{"recipe": f_["recipe"], "prof": f_["prof"], "chance": f_["chance"], "req": f_["req"]}]))
    for z in zones.values():
        z["map"] = next((o["map"] for o in z["stops"] + z["farm"] if o.get("map")), None)
    zone_list = sorted(zones.values(), key=lambda z: (z["zone"] == "Unknown zone", -(len(z["stops"]) + len(z["farm"])), z["zone"]))
    currency = {}
    for st in stops:
        for k, v in (st.get("currency") or {}).items():
            currency[k] = currency.get(k, 0) + v
    return {"char": dict(ch, raw=None), "stops": stops, "farm": farm[:60],
            "total_cost": sum(s["cost"] for s in stops), "currency": currency, "zones": zone_list,
            "recipes": sum(len(s["items"]) for s in stops), "professions": pslugs}


# ---- API ----------------------------------------------------------------------
class CharIn(BaseModel):
    slug: str


@app.on_event("startup")
def _startup():
    init_db()
    for s in [x.strip() for x in os.environ.get("VOLUNDR_CHARS", "").split(",") if x.strip()]:
        with db() as con:
            con.execute("INSERT OR IGNORE INTO chars(slug,display) VALUES(?,?)", (s.lower(), s))
    if os.environ.get("VOLUNDR_SCHEDULER", "1") == "1":
        threading.Thread(target=scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    with db() as con:
        n = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("chars", "char_recipes", "bnet_recipes", "fdb_recipes", "npc_locations")}
    return {"ok": True, "version": VERSION, "blizzard": bz.configured(), "foreverdb": fdb.configured(), "maps": len(maps_index()["by_area"]),
            "static_ns": _ns(), "region": bz.REGION, "counts": n, "sync": SYNC}


@app.get("/api/chars")
def chars():
    with db() as con:
        rows = [dict(r, raw=None) for r in con.execute("SELECT * FROM chars ORDER BY level DESC, display")]
        for r in rows:
            r["professions"] = [dict(p) for p in con.execute(
                "SELECT prof_id, prof_name, prof_slug, MAX(skill) skill, MAX(max_skill) max_skill FROM char_profs WHERE slug=? GROUP BY prof_id ORDER BY prof_name", (r["slug"],))]
            for p in r["professions"]:
                p["known"] = con.execute("SELECT COUNT(*) FROM char_recipes WHERE slug=? AND prof_id=?", (r["slug"], p["prof_id"])).fetchone()[0]
                p["universe"] = con.execute("SELECT COUNT(*) FROM bnet_recipes WHERE prof_id=?", (p["prof_id"],)).fetchone()[0]
    return rows


@app.post("/api/chars")
def add_char(c: CharIn):
    slug = c.slug.lower().strip().replace(" ", "-")
    if "-" not in slug:
        raise HTTPException(400, "use first-surname, e.g. covenna-ravenshroud")
    with db() as con:
        con.execute("INSERT OR IGNORE INTO chars(slug,display) VALUES(?,?)", (slug, slug))
    res = sync_char(slug)
    if res["ok"] and bz.configured() and not SYNC["running"]:
        threading.Thread(target=run_static_sync, args=(None,), daemon=True).start()
    return res


@app.delete("/api/chars/{slug}")
def del_char(slug: str):
    with db() as con:
        for t in ("chars", "char_profs", "char_recipes"):
            con.execute(f"DELETE FROM {t} WHERE slug=?", (slug,))
    return {"ok": True}


@app.post("/api/sync/char/{slug}")
def api_sync_char(slug: str):
    return sync_char(slug)


@app.post("/api/sync/static")
def api_sync_static(prof: str | None = None, force: bool = False):
    if SYNC["running"]:
        return {"ok": False, "running": True, "sync": SYNC}
    threading.Thread(target=run_static_sync, args=([prof] if prof else None, force), daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/sync/questie")
def api_sync_questie():
    if SYNC["running"]:
        return {"ok": False, "running": True}
    def run():
        SYNC.update(running=True, phase="questie", detail="", started=time.time(), errors=[])
        try:
            sync_questie()
        except Exception as e:  # noqa: BLE001
            SYNC["errors"].append(f"questie: {e}")
        finally:
            SYNC.update(running=False, phase="done", done=time.time())
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/sync/status")
def api_sync_status():
    return SYNC


@app.get("/api/char/{slug}")
def api_char(slug: str):
    for c in chars():
        if c["slug"] == slug:
            return c
    raise HTTPException(404, "unknown character")


@app.get("/api/char/{slug}/prof/{pslug}")
def api_ledger(slug: str, pslug: str):
    return ledger(slug, pslug)


@app.get("/api/char/{slug}/next")
def api_next(slug: str):
    return next_session(slug)


@app.get("/api/probe")
def probe(slug: str = "covenna-ravenshroud"):
    """Which Forever endpoints answer right now (community-documented, may drift)."""
    if not bz.configured():
        return {"blizzard": False, "note": "set BNET_CLIENT_ID / BNET_CLIENT_SECRET in the container's env file"}
    out = {"static_ns": _ns()}
    for sub in ("", "professions", "collections", "equipment", "appearance", "character-media"):
        out[f"character/{sub or 'profile'}"] = bz.status(bz.char_path(slug, sub), bz.NS_PROFILE)
    out["profession/index"] = bz.status("/data/wow/profession/index", _ns())
    out["profession/index@plain"] = bz.status("/data/wow/profession/index", bz.NS_STATIC)
    return out


@app.get("/api/debug/fdb/{pslug}")
def debug_fdb(pslug: str, n: int = 2):
    """First raw ForeverDB recipes of a profession + how they normalised (to tune foreverdb.py)."""
    if not fdb.configured():
        return {"foreverdb": False}
    page = fdb.get(f"/professions/{pslug}", {"limit": n, "offset": 0})
    data = page.get("data") or page
    recs = fdb._recipes_of(data)
    return {"meta": page.get("meta"), "data_keys": sorted(data.keys()) if isinstance(data, dict) else None,
            "raw": recs, "normalized": [fdb.normalize_recipe(r) for r in recs]}


@app.get("/api/debug/bnet/{slug}/{sub}")
def debug_bnet(slug: str, sub: str):
    if not bz.configured():
        return {"blizzard": False}
    try:
        return bz.get(bz.char_path(slug, "" if sub == "profile" else sub), bz.NS_PROFILE)
    except bz.BnetError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status or 500)
