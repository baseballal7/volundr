# Völundr — The Smith's Ledger

A small self-hosted crafting ledger for **World of Warcraft: Forever** characters.
It shows, per character and profession, which recipes you know, which you lack, and the
cheapest way to learn each one (trainer and city, vendor and price, drop and chance, quest),
then reduces the learnable set to a "next session" route.

Named for Völundr, the smith of Norse legend. Personal homelab project, used by two people.

## Data

- **Character data** © Blizzard Entertainment, read through the Battle.net API
  (`profile-forever-{region}`: character, professions with known recipes; `static-forever-{region}`:
  the recipe universe). Forever's API is not yet documented by Blizzard; `/api/probe` reports what
  answers today.
- **Recipe sources** (trainers, vendors, quests, drops, prices, skill colours):
  **[Data from ForeverDB.net](https://foreverdb.net)** — shown with that credit link in the app footer,
  per their API terms. Fetched roughly once a week per profession and cached locally.

## Run

FastAPI + one vanilla-JS page, SQLite cache, no build step.

```bash
cp .env.example /path/to/.volundr.env   # Battle.net client credentials + ForeverDB key
docker compose up -d --build            # serves on :8007
```

Without keys it still boots: the character card falls back to the public Armory page and the
ledger stays empty. See `AGENTS.md` for the data model, sync behaviour and roadmap.

## Status

v0.1 — recipes. Appearances (transmog collection vs. the full appearance universe) are next;
the Forever API exposes both `collections/transmogs` and `item-appearance` static docs.
