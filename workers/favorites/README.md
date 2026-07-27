# busybar-favorites Worker

Cloudflare Worker that tracks public favorite counts per app using Workers KV. No accounts — the frontend stores which apps a visitor has favorited in localStorage.

## What it does

- `GET /counts` — returns all favorite counts as `{ "counts": { "<slug>": <int> } }`
- `POST /favorite` with `{ "slug": "...", "action": "add"|"remove" }` — increments or decrements the count for a slug; returns `{ "slug": "...", "count": <int> }`
- Rate limited to 10 POST requests per IP per 60 seconds (in-isolate, best-effort)
- CORS allowed for `https://maxswinkels.github.io` and `http://localhost:4321`

## One-time deploy

1. `cd workers/favorites && npx wrangler login`
2. `npx wrangler kv namespace create FAVORITES`
   → copy the printed `id` into `wrangler.toml` under `[[kv_namespaces]]`
3. `npx wrangler deploy`
   → note the printed URL, e.g. `https://busybar-favorites.<subdomain>.workers.dev`
4. Put that URL as the fallback const in `src/scripts/favorites.js`, commit, push
   (GitHub Pages redeploys automatically)

## Local dev

```sh
npm run worker:dev
# uses Miniflare's local KV — no Cloudflare account required
```

## Smoke tests

```sh
# All counts
curl https://busybar-favorites.<subdomain>.workers.dev/counts

# Add a favorite
curl -X POST https://busybar-favorites.<subdomain>.workers.dev/favorite \
  -H 'Content-Type: application/json' \
  -d '{"slug":"github-graph","action":"add"}'

# Remove a favorite
curl -X POST https://busybar-favorites.<subdomain>.workers.dev/favorite \
  -H 'Content-Type: application/json' \
  -d '{"slug":"github-graph","action":"remove"}'

# Bad request (expect 400)
curl -X POST https://busybar-favorites.<subdomain>.workers.dev/favorite \
  -H 'Content-Type: application/json' \
  -d '{"slug":"../evil","action":"add"}'
```
