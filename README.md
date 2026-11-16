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
Items get an auto `INV-000001` number; `replacement_value` defaults to `purchased_value`.
Item types: Appliance, Furniture, Electronics, Memorabilia, Media, Collectible, Clothing.

## API (JSON under /api)
- `GET /meta`
- `GET|POST /homes`, `GET|PUT|DELETE /homes/:id`
- `GET|POST /homes/:id/rooms` (POST `prefill: true` creates template items; room name picks the template)
- `GET|PUT|DELETE /rooms/:id` (PUT with `home_id` moves the room and its items)
- `GET|POST /rooms/:id/items`, `GET|PUT|DELETE /items/:id`
- `POST /items/:id/image?kind=photo|similar` with `{data: "<base64 data URL>"}`
- `POST /identify` → 501 (reserved for photo-to-item via a local Ollama vision model)

## Roadmap
1. Photo-to-item via Ollama (`qwen2.5vl` / `llama3.2-vision`), pre-filling the Add Item form, `source='vision'`.
2. Phone access (PWA over LAN) with auth.
3. Search/filter, CSV export for insurance, per-home totals report.
