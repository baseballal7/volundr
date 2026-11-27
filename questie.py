"""Völundr — NPC locations from QuestieDB (github.com/Questie/QuestieDB, Forever flavour).

ForeverDB's API has no NPC endpoint, so drop sources arrive as "Coastal Frenzy" with an npcId and
nothing else. QuestieDB's generated `foreverNpcDB.lua` carries every NPC's spawn coordinates per
area id; `support/Zones/areaIdToUiMapId.lua` carries the area names in trailing comments.

The Lua files are DATA, never executed: a small tokenizer/parser below reads the generated
`return { [id] = {...}, ... }` table. Record layout (npcKeys): 1 name, 4 minLevel, 5 maxLevel,
7 spawns {[areaId] = {{x, y}, ...}}, 9 zoneID (best-guess home zone), 14 subName.

Licensing note: QuestieDB's data is not under a clean open licence (per-contributor GPL consent,
otherwise all rights reserved). It is loaded into the private database only and is never
redistributed with this app.
"""
import os, re, json, time, urllib.request

RAW = "https://raw.githubusercontent.com/Questie/QuestieDB/master/"
NPC_URL = RAW + "data/Forever/foreverNpcDB.lua"
ZONE_URL = RAW + "support/Zones/areaIdToUiMapId.lua"
UA = "Volundr crafting tracker (personal homelab)"


def fetch(url: str) -> str:
    r = urllib.request.Request(url)
    r.add_header("User-Agent", UA)
    return urllib.request.urlopen(r, timeout=120).read().decode("utf-8", errors="replace")


# ---- tiny Lua table reader (only what the generated files use) ---------------
_TOK = re.compile(r"""
    \s+ | --[^\n]* |                                   # whitespace / comments (dropped)
    (?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) |
    (?P<sq>'(?:\\.|[^'\\])*') | (?P<dq>"(?:\\.|[^"\\])*") |
    (?P<kw>nil|true|false) |
    (?P<p>[{}\[\]=,])
""", re.X)


def _tokens(src: str):
    for m in _TOK.finditer(src):
        k = m.lastgroup
        if k is None:
            continue
        v = m.group(k)
        if k == "num":
            yield ("num", float(v) if ("." in v or "e" in v or "E" in v) else int(v))
        elif k in ("sq", "dq"):
            yield ("str", bytes(v[1:-1], "utf-8").decode("unicode_escape", errors="replace") if "\\" in v else v[1:-1])
        elif k == "kw":
            yield ("kw", None if v == "nil" else v == "true")
        else:
            yield ("p", v)


def parse_table(src: str):
    """Parse `{ ... }` into Python: tables with only positional entries -> list, else dict."""
    toks = list(_tokens(src))
    pos = 0

    def value():
        nonlocal pos
        t, v = toks[pos]
        if t == "p" and v == "{":
            pos += 1
            return table()
        pos += 1
        return v

    def table():
        nonlocal pos
        items, keyed = [], {}
        while True:
            t, v = toks[pos]
            if t == "p" and v == "}":
                pos += 1
                break
            if t == "p" and v == ",":
                pos += 1
                continue
            if t == "p" and v == "[":
                pos += 1
                key = value()
                assert toks[pos] == ("p", "]"), toks[pos]
                pos += 1
                assert toks[pos] == ("p", "="), toks[pos]
                pos += 1
                keyed[key] = value()
            else:
                items.append(value())
        if keyed:
            for i, it in enumerate(items, 1):
                keyed.setdefault(i, it)
            return keyed
        return items

    # skip to the first '{'
    while toks[pos] != ("p", "{"):
        pos += 1
    pos += 1
    return table()


def parse_npcs(lua: str) -> dict:
    """{npc_id: {"name","min","max","sub","zone_id","spawns": {area_id: [(x,y),...]}}}"""
    m = re.search(r"npcData\s*=\s*\[\[\s*return\s*(\{.*\})\s*\]\]", lua, re.S)
    if not m:
        raise ValueError("npcData block not found")
    tbl = parse_table(m.group(1))
    out = {}
    for nid, rec in tbl.items():
        if not isinstance(nid, int) or not isinstance(rec, (list, dict)):
            continue
        g = (lambda i: rec[i - 1] if isinstance(rec, list) and len(rec) >= i else rec.get(i) if isinstance(rec, dict) else None)
        spawns = g(7) or {}
        sp = {}
        if isinstance(spawns, dict):
            for area, pts in spawns.items():
                if isinstance(area, int) and isinstance(pts, list):
                    sp[area] = [(p[0], p[1]) for p in pts if isinstance(p, list) and len(p) >= 2 and all(isinstance(c, (int, float)) for c in p[:2])]
        out[nid] = {"name": g(1), "min": g(4), "max": g(5), "sub": g(14), "zone_id": g(9), "spawns": sp}
    return out


def parse_zone_names(lua: str) -> dict:
    """area id -> name from lines like `[148] = 1439, -- Darkshore` (comments are the only name source)."""
    names = {}
    for m in re.finditer(r"^\s*\[(\d+)\]\s*=\s*(-?\d+)\s*,\s*--\s*(.+?)\s*$", lua, re.M):
        names[int(m.group(1))] = m.group(3).strip()
    return names


def parse_area_uimap(lua: str) -> dict:
    """area id -> UiMap id (the id ForeverDB calls mapId on trainers/vendors)."""
    out = {}
    for m in re.finditer(r"^\s*\[(\d+)\]\s*=\s*(\d+)\s*,", lua, re.M):
        out[int(m.group(1))] = int(m.group(2))
    return out


def best_spot(points: list) -> tuple:
    """Representative coordinate for a mob with many spawns: the centre of its densest cluster,
    so a /way lands in the middle of a pack rather than between two packs."""
    if not points:
        return None, None, 0
    if len(points) <= 2:
        return round(sum(p[0] for p in points) / len(points), 1), round(sum(p[1] for p in points) / len(points), 1), len(points)
    R2 = 8.0 ** 2
    best, best_n = None, -1
    for p in points:
        nb = [q for q in points if (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 <= R2]
        if len(nb) > best_n:
            best, best_n = nb, len(nb)
    return round(sum(q[0] for q in best) / len(best), 1), round(sum(q[1] for q in best) / len(best), 1), len(best)


def build_locations(npcs: dict, zone_names: dict) -> list:
    """One row per NPC: home zone (zoneID, else the area with most spawns), representative x/y,
    and every zone it appears in with spawn counts."""
    rows = []
    zn = lambda a: (zone_names.get(a) or f"area {a}").split(" -- ")[0].strip()   # "Blackrock Depths - Dungeon -- Shadowforge City" -> zone only
    for nid, n in npcs.items():
        sp = {a: [p for p in pts if p[0] >= 0 and p[1] >= 0] for a, pts in n["spawns"].items()}   # (-1,-1) = inside an instance, no map position
        sp = {a: pts for a, pts in sp.items() if pts}
        if sp:
            home = n["zone_id"] if n["zone_id"] in sp else max(sp, key=lambda a: len(sp[a]))
            x, y, cl = best_spot(sp[home])
            nsp = len(sp[home])
        elif n["zone_id"] or n["spawns"]:
            home = n["zone_id"] or next(iter(n["spawns"]))
            x = y = None
            cl = nsp = 0
        else:
            continue
        zones = [{"area": a, "zone": zn(a), "spawns": len(p)} for a, p in sorted((sp or n["spawns"]).items(), key=lambda kv: -len(kv[1]))]
        pts = [[round(p[0], 1), round(p[1], 1)] for p in (sp.get(home) or [])][:300]
        rows.append((nid, n["name"], n["sub"], n["min"], n["max"], home, zn(home), x, y, nsp, cl, json.dumps(zones), json.dumps(pts)))
    return rows


if __name__ == "__main__":  # local check: python questie.py <foreverNpcDB.lua> <areaIdToUiMapId.lua> [npcId]
    import sys
    npcs = parse_npcs(open(sys.argv[1], encoding="utf-8").read())
    zn = parse_zone_names(open(sys.argv[2], encoding="utf-8").read())
    rows = build_locations(npcs, zn)
    print(len(npcs), "npcs parsed,", len(rows), "with spawns,", len(zn), "zone names")
    for r in rows:
        if len(sys.argv) > 3 and r[0] == int(sys.argv[3]):
            print(r)
