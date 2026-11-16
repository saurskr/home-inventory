# Home Inventory

Local-first app for cataloging household items. All data stays on your machine.
Python 3.9+ standard library only (http.server + sqlite3), vanilla JS frontend.

```bash
python3 server.py          # http://localhost:8000  (PORT=9000 to change)
python3 test_api.py        # API tests
```

UI styling comes from the shared design system (`static/ds/`, a vendored copy of github.com/saurskr/design-system: Warm Paper / Midnight themes, Auto/Light/Dark switch in the sidebar).

Data lives in `data/inventory.db` and `data/images/` (git-ignored; override with `INVENTORY_DATA_DIR`).
The server binds to 127.0.0.1 only. Add authentication before exposing it on the LAN.

## Model
homes → rooms → items. Deleting a home or room cascades (and removes image files).
Items get an auto `INV-000001` number and a `price_updated_at` stamp (set when a replacement price is entered or changed, or by sending `price_confirmed: true`); `replacement_value` defaults to `purchased_value`.
Item types: Appliance, Furniture, Electronics, Memorabilia, Media, Collectible, Clothing.

## API (JSON under /api)
- `GET /meta`
- `GET|POST /homes`, `GET|PUT|DELETE /homes/:id` Homes take a US address: `street`, `street2`, `city`, `state` (2-letter code, see `us_states` in `GET /meta`), `zip` (12345 or 12345-6789); responses include a formatted `address`. Older free-text addresses still display until replaced.
- `GET|POST /homes/:id/rooms` (POST `prefill: true` creates template items; room name picks the template)
- `GET|PUT|DELETE /homes/:id/insurance` (optional per-home policy: insurer, policy number, dwelling and contents coverage, deductible, premium, renewal date, contact, notes; PUT merges the fields you send)
- `GET|PUT|DELETE /rooms/:id` (PUT with `home_id` moves the room and its items)
- `GET|POST /rooms/:id/items`, `GET|PUT|DELETE /items/:id`
- `similar_url` must be http(s). Rooms and homes report purchased and replacement totals.
- `POST /items/:id/image?kind=photo|similar` with `{data: "<base64 data URL>"}`
- `POST /identify` with `{data: <image data URL>, provider: "ollama" | "anthropic"}` returns `{name, brand, item_type, description, search_query, provider, model}` for you to review before saving (save with `source: "vision"`)
- `GET|PUT /settings` (Ollama address and model, Claude model, API key; the key is write-only and never returned), `GET /providers/status`

## Photo identification
Default: a local vision model through [Ollama](https://ollama.com) (`qwen2.5vl:7b`; use `qwen2.5vl:3b` on small GPUs).
```bash
ollama pull qwen2.5vl:7b
```
Optional: Claude with your own API key (Settings page). It is off until a key is saved, a photo is sent only when you click
*Identify with Claude* and confirm, and the key is stored in `data/settings.json` (owner-only permissions) or read from `ANTHROPIC_API_KEY`.
Environment overrides: `OLLAMA_HOST`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `INVENTORY_ALLOWED_HOSTS`.

Security: the API only accepts `Host: localhost|127.0.0.1|[::1]` (blocks DNS rebinding) and requires `Content-Type: application/json`
on writes (blocks cross-site form posts). Add more hosts with `INVENTORY_ALLOWED_HOSTS` and add authentication before LAN use.

## Roadmap
1. Compare candidate vision models on your own photos (small bake-off script).
2. Phone access (PWA over LAN) with auth.
3. Search/filter, CSV export for insurance, per-home totals report.
