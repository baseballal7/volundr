# Völundr — WoW Forever crafting ledger

FastAPI + one vanilla-JS `static/index.html` with hash routes, SQLite cache at `VOLUNDR_DB`.
Container `volundr-web` on `:8007`; secrets in an env file next to the compose (`.env.example` is the template).

## What it answers
Per character: which recipes each profession knows, which it lacks, and where/how to get each one
(trainer at skill X in city Y, vendor + price + limited stock, drop + chance, quest). "Next session"
reduces the learnable set to a route: stops grouped by trainer/vendor with total cost, plus a farm list.

## Data sources (all three are needed for the full picture)
| Source | Gives | Auth | Notes |
|---|---|---|---|
| Battle.net profile API, namespace `profile-forever-{region}` | character card, professions with skill + **known recipes** | client credentials (`BNET_CLIENT_ID/SECRET`) | realmless: `/profile/wow/character/{first}-{surname}`; level 10+ only; profile refreshes on logout. **Undocumented by Blizzard** (community-probed, 2026-11); `/api/probe` shows what answers today |
| Battle.net static API, `static-forever-{region}` (alias of a versioned `static-<build>-forever-{region}`) | the recipe universe: profession → skill tiers → categories → recipes; each recipe's crafted item | same | namespace auto-read from the character doc's `character_class.key.href` and stored in `meta.static_ns`; pin with `BNET_STATIC_NS` if the alias 403s |
| ForeverDB.net API `/api/v1/professions/{slug}` | per-recipe skill colours + `learnedFrom.sources` (trainer/vendor/quest/drop, prices, `seenInForever`) | `FOREVERDB_API_KEY` (request on foreverdb.net/developers) | free tier 5k/day, 30/min, **credit link required in the UI** (footer), custom User-Agent (default urllib UA gets a Cloudflare 403). Refreshed every `VOLUNDR_FDB_TTL_DAYS` (7) |
| Armory page `characterProfileInitialState` | character card only | none | fallback when no Battle.net key; carries no professions |

## Join + classification
Blizzard recipe ↔ ForeverDB recipe is matched by crafted `item_id` first, then by normalised name
(`foreverdb.norm_name`: lower, strip "Pattern:/Recipe:/Formula:" prefixes and punctuation). The ledger
header shows `ForeverDB matched n/total` — if that number is low, hit `/api/debug/fdb/tailoring` and
adjust `normalize_recipe` / `normalize_source` (they read field names defensively because the
ForeverDB shape was taken from its docs, not a live call).

Groups (`app.classify`): sources filtered to the character's faction (neutral kept) →
`now` = trainer/vendor source and required skill ≤ current skill; `later` = trainer/vendor above
current skill (`needs_rank` when above the tier cap); `farm` = drop/quest/fishing only; `unknown` =
no usable source (`other_faction` flag when the only sources are the other faction). Required skill =
ForeverDB orange, else the lowest trainer/vendor skill requirement.

## Sync
- Characters: every `VOLUNDR_SYNC_HOURS` (6) in a daemon thread, plus `POST /api/sync/char/{slug}`.
- Static + ForeverDB: once a day (TTL-gated), plus `POST /api/sync/static?prof=tailoring&force=1`.
  Adding a character via `POST /api/chars` kicks a static sync for its professions. Blizzard recipe
  docs are fetched once each (hundreds per profession, 0.05 s apart) and never refetched.
- `GET /api/sync/status` / the header chip show progress; errors accumulate in `SYNC.errors`.

## Routes
`#/` characters · `#/c/{slug}` professions · `#/c/{slug}/p/{prof}` ledger (groups, filter, reagents on
click) · `#/c/{slug}/next` route · `#/probe` endpoint probe.

## Roadmap
- **Phase 2 — appearances.** The profile API exposes `character/{slug}/collections/transmogs`
  (collected appearance ids per slot + appearance sets) and the static namespace serves
  `item-appearance/{id}`, `item-appearance/slot/index` and `search/item-appearance`, so the missing-
  appearance diff can be built server-side like recipes.
- A chat nudge when new recipes become learnable (weekly diff of the `now` group).
- `tools/seed_sample.py <db>` seeds a local DB with SAMPLE rows for UI work; never against live data.

## Public mirror rule
This repository is public. Nothing deployment-specific belongs in it: no hostnames, usernames,
LAN addresses, absolute paths or keys. Keep those in the private deployment copy.
