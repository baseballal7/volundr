"""Völundr — creature names from the WoW client's creature cache.

ForeverDB (and QuestieDB) do not yet know the names of many Forever-new creatures, so drop sources
arrive as "NPC 246017". The game client keeps a cache of every creature the player has seen
(`<install>/_forever_/Cache/WDB/enUS/creaturecache.wdb`), which carries the id, name and title.
This reads that file as data and writes {id: {"name", "title"}} JSON for the app to overlay.

usage: python tools/creaturecache.py "<path to creaturecache.wdb>" <out.json>
Run it on the gaming PC after a play session, then copy the JSON next to the app's database.
"""
import json, re, struct, sys

TEXT = re.compile(rb"[\x20-\x7e\xc0-\xff][\x20-\x7e\x80-\xff]{2,}")


def records(b: bytes):
    """Yield (id, payload). The header length varies by build; find the first offset from which the
    (id, length) chain walks cleanly to the end of the file."""
    for start in range(16, 64, 4):
        off, out = start, []
        ok = True
        while off + 8 <= len(b):
            cid, ln = struct.unpack("<II", b[off:off + 8])
            if cid == 0 and ln == 0:
                break
            if ln == 0 or ln > 5000 or off + 8 + ln > len(b):
                ok = False
                break
            out.append((cid, b[off + 8:off + 8 + ln]))
            off += 8 + ln
        if ok and len(out) > 10:
            return out
    return []


def parse(b: bytes) -> dict:
    names = {}
    for cid, rec in records(b):
        strs = [s.decode("utf-8", "replace") for s in TEXT.findall(rec)]
        strs = [s for s in strs if "�" not in s and re.search(r"[A-Za-z]{3}", s)]
        if not strs:
            continue
        name = strs[0].strip()
        title = ""
        for s in strs[1:]:
            s = s.strip()
            if s != name and re.fullmatch(r"[A-Za-z' \-&]{3,40}", s):
                title = s
                break
        names[cid] = {"name": name, "title": title}
    return names


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    data = parse(open(src, "rb").read())
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    new = sum(1 for k in data if k >= 240000)
    print(f"{len(data)} creatures ({new} Forever-new ids) -> {out}")
