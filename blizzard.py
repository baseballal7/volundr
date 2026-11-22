"""Völundr — Battle.net API client for World of Warcraft: Forever (client-credentials).

Forever is realmless: characters are addressed as "{first}-{surname}" under the
profile-forever-{region} namespace. Nothing here is Blizzard-documented (as of
2026-11-22); the shapes were taken from community probes and are read defensively.
"""
import os, json, time, base64, urllib.request, urllib.parse, urllib.error

CID = os.environ.get("BNET_CLIENT_ID", "")
SEC = os.environ.get("BNET_CLIENT_SECRET", "")
REGION = os.environ.get("VOLUNDR_REGION", "us").lower()
LOCALE = os.environ.get("VOLUNDR_LOCALE", "en_US")
API = f"https://{REGION}.api.blizzard.com"
TOKEN_URL = "https://oauth.battle.net/token"
UA = "Volundr crafting tracker (personal homelab)"

NS_PROFILE = f"profile-forever-{REGION}"
NS_DYNAMIC = f"dynamic-forever-{REGION}"
# plain alias reportedly works since launch; a versioned one (static-1.60.1_70399-forever-us)
# can be pinned with BNET_STATIC_NS if the alias ever 403s again
NS_STATIC = os.environ.get("BNET_STATIC_NS", f"static-forever-{REGION}")


def configured() -> bool:
    return bool(CID and SEC)


class BnetError(Exception):
    def __init__(self, status, msg, path=""):
        super().__init__(f"{status} {path}: {msg[:300]}")
        self.status = status
        self.path = path


_tok = {"v": None, "exp": 0}


def token() -> str:
    if not configured():
        raise BnetError(0, "BNET_CLIENT_ID / BNET_CLIENT_SECRET not set")
    if _tok["v"] and time.time() < _tok["exp"] - 600:
        return _tok["v"]
    r = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    r.add_header("Authorization", "Basic " + base64.b64encode(f"{CID}:{SEC}".encode()).decode())
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    r.add_header("User-Agent", UA)
    try:
        d = json.loads(urllib.request.urlopen(r, timeout=30).read())
    except urllib.error.HTTPError as e:
        raise BnetError(e.code, e.read().decode(errors="replace"), "oauth/token")
    _tok["v"] = d["access_token"]
    _tok["exp"] = time.time() + d.get("expires_in", 86000)
    return _tok["v"]


def get(path: str, namespace: str, params: dict | None = None) -> dict:
    q = {"namespace": namespace, "locale": LOCALE}
    if params:
        q.update(params)
    url = f"{API}{path}?{urllib.parse.urlencode(q)}"
    r = urllib.request.Request(url)
    r.add_header("Authorization", "Bearer " + token())
    r.add_header("User-Agent", UA)
    try:
        return json.loads(urllib.request.urlopen(r, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise BnetError(e.code, e.read().decode(errors="replace"), path)


def status(path: str, namespace: str) -> tuple[int, dict | str]:
    """Probe helper: (http status, body-or-keys) without raising."""
    try:
        d = get(path, namespace)
        return 200, (sorted(d.keys()) if isinstance(d, dict) else str(d)[:200])
    except BnetError as e:
        return e.status, str(e)[:200]


# ---- profile (character) ---------------------------------------------------
def char_path(slug: str, sub: str = "") -> str:
    p = f"/profile/wow/character/{slug.lower()}"
    return f"{p}/{sub}" if sub else p


def character(slug):      return get(char_path(slug), NS_PROFILE)
def professions(slug):    return get(char_path(slug, "professions"), NS_PROFILE)
def collections(slug):    return get(char_path(slug, "collections"), NS_PROFILE)
def equipment(slug):      return get(char_path(slug, "equipment"), NS_PROFILE)
def media(slug):          return get(char_path(slug, "character-media"), NS_PROFILE)


def static_ns_from_profile(prof: dict) -> str | None:
    """The character doc links playable_class with ?namespace=static-<build>-forever-<region>;
    read it so the static namespace never has to be hardcoded."""
    for k in ("character_class", "race", "active_spec"):
        href = (((prof.get(k) or {}).get("key") or {}).get("href")) or ""
        if "namespace=" in href:
            return urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("namespace", [None])[0]
    return None


# ---- static (game data) ----------------------------------------------------
def profession_index(ns=None):          return get("/data/wow/profession/index", ns or NS_STATIC)
def profession(pid, ns=None):           return get(f"/data/wow/profession/{pid}", ns or NS_STATIC)
def skill_tier(pid, tid, ns=None):      return get(f"/data/wow/profession/{pid}/skill-tier/{tid}", ns or NS_STATIC)
def recipe(rid, ns=None):               return get(f"/data/wow/recipe/{rid}", ns or NS_STATIC)
def recipe_media(rid, ns=None):         return get(f"/data/wow/media/recipe/{rid}", ns or NS_STATIC)
def item(iid, ns=None):                 return get(f"/data/wow/item/{iid}", ns or NS_STATIC)


# ---- Armory fallback (no key needed) --------------------------------------
ARMORY = "https://worldofwarcraft.blizzard.com/en-us/forever/{region}/armory/character/{slug}"


def armory_profile(slug: str) -> dict:
    """The Forever armory page embeds `characterProfileInitialState` (name, level, class, race,
    guild, avatar). No professions or recipes there — this only feeds the character card when
    the API client is not configured yet."""
    import re
    r = urllib.request.Request(ARMORY.format(region=REGION, slug=slug.lower()))
    r.add_header("User-Agent", "Mozilla/5.0 (Volundr homelab tracker)")
    html = urllib.request.urlopen(r, timeout=30).read().decode("utf-8", errors="replace")
    m = re.search(r"characterProfileInitialState\s*=\s*(\{.*?\});\s*\n", html, re.S)
    if not m:
        raise BnetError(404, "armory page has no characterProfileInitialState", slug)
    return json.loads(m.group(1)).get("character", {})
