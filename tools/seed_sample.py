"""Seed a LOCAL test DB with clearly-labelled sample rows so the ledger UI can be exercised
before the Battle.net / ForeverDB keys exist. Never run against the live /data/volundr.db.

usage: python tools/seed_sample.py <path-to-db>
"""
import json, sqlite3, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402  (for SCHEMA)

db = sys.argv[1]
con = sqlite3.connect(db)
con.executescript(app.SCHEMA)
now = time.time()
slug = "covenna-ravenshroud"
con.execute("INSERT OR REPLACE INTO chars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, "Covenna", "Ravenshroud", "Covenna Ravenshroud", "Mage", "High Order Skyborne", 49, "Diversity Hires",
             "alliance", "Normal", None, "sample", now, None, "{}"))
profs = [(197, "Tailoring", "tailoring", 223, 225), (333, "Enchanting", "enchanting", 276, 300), (185, "Cooking", "cooking", 225, 300)]
for pid, name, ps, sk, mx in profs:
    con.execute("INSERT OR REPLACE INTO char_profs VALUES(?,?,?,?,?,?,?,?)", (slug, pid, name, ps, 0, "Artisan " + name, sk, mx))
    con.execute("INSERT OR REPLACE INTO bnet_profs VALUES(?,?,?,?,?)", (pid, name, ps, "[]", now))
# SAMPLE recipes (names are real Classic tailoring recipes; sources are placeholders for layout only)
R = [  # recipe_id, name, item_id, orange, known, sources
    (1001, "Red Mageweave Headband", 10001, 225, True, [{"type": "trainer", "name": "SAMPLE Trainer", "zone": "Stormwind", "faction": "alliance", "skill": 225, "price": 2500}]),
    (1002, "Dreamweave Circlet", 10002, 225, False, [{"type": "trainer", "name": "SAMPLE Trainer", "zone": "Stormwind", "faction": "alliance", "skill": 225, "price": 2500}]),
    (1003, "Black Mageweave Boots", 10003, 230, False, [{"type": "trainer", "name": "SAMPLE Trainer", "zone": "Ironforge", "faction": "alliance", "skill": 230, "price": 3500}]),
    (1004, "Runecloth Belt", 10004, 250, False, [{"type": "trainer", "name": "SAMPLE Trainer", "zone": "Stormwind", "faction": "alliance", "skill": 250, "price": 5000}]),
    (1005, "Tuxedo Shirt", 10005, 225, False, [{"type": "vendor", "name": "SAMPLE Vendor", "zone": "Darnassus", "faction": "alliance", "price": 12000, "limited": True}]),
    (1006, "Robe of Winter Night", 10006, 285, False, [{"type": "drop", "name": "SAMPLE Mob", "zone": "Winterspring", "faction": "", "drop_chance": "0.8"}]),
    (1007, "Mooncloth", 10007, 250, False, [{"type": "quest", "name": "SAMPLE Quest", "zone": "Felwood", "faction": "", "quest_id": 1}]),
    (1008, "Stormcloth Boots", 10008, 265, False, []),
    (1009, "Flarecore Gloves", 10009, 300, False, [{"type": "vendor", "name": "SAMPLE Rep Vendor", "zone": "Blackrock Depths", "faction": "", "price": 90000, "rep": "Thorium Brotherhood"}]),
    (1010, "Orcish Wedding Dress", 10010, 225, False, [{"type": "vendor", "name": "SAMPLE Horde Vendor", "zone": "Orgrimmar", "faction": "horde", "price": 1000}]),
]
for rid, name, iid, orange, known, srcs in R:
    nn = app.fdb.norm_name(name)
    con.execute("INSERT OR REPLACE INTO bnet_recipes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, 197, "tailoring", 0, "Artisan Tailoring", "SAMPLE category", name, nn, iid, name, json.dumps([{"name": "Bolt of Mageweave", "qty": 2}, {"name": "Heavy Silken Thread", "qty": 1}]), now, "{}"))
    norm = {"name": name, "orange": orange, "yellow": orange + 15, "green": orange + 30, "grey": orange + 45, "category": "SAMPLE category",
            "changed": rid == 1006, "new": rid == 1009, "reagents": [], "sources": srcs}
    if rid != 1008:
        con.execute("INSERT OR REPLACE INTO fdb_recipes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"tailoring:{rid}", "tailoring", name, nn, iid, None, orange, orange + 15, orange + 30, orange + 45, "SAMPLE category",
                     json.dumps(srcs), json.dumps(norm), "{}", now))
    if known:
        con.execute("INSERT OR REPLACE INTO char_recipes VALUES(?,?,?,?)", (slug, 197, rid, name))
con.execute("INSERT OR REPLACE INTO meta VALUES('fdb_fetched:tailoring',?)", (str(now),))
con.commit()
print("seeded", db)
