# Home Inventory

Local-first app for cataloging household items. All data stays on your machine.
Python 3.9+ standard library only (http.server + sqlite3), vanilla JS frontend.

```bash
python3 server.py          # http://localhost:8000  (PORT=9000 to change)
python3 test_api.py        # app tests
python3 -m unittest discover -s receipt_module/tests -t .   # receipt module tests
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
- `GET|PUT|DELETE /homes/:id/insurance` (optional per-home policy: insurer, website (http/https), policy number, expiry date (`renewal_date`, YYYY-MM-DD), dwelling / contents / liability coverage, deductible, annual premium, up to 5 `riders` of `{name, limit}`, contact, notes; PUT merges the fields you send). `GET /homes` and `GET /homes/:id` include the home's `insurance`
- `GET|PUT|DELETE /rooms/:id` (PUT with `home_id` moves the room and its items)
- `GET|POST /rooms/:id/items`, `GET|PUT|DELETE /items/:id`
- `similar_url` must be http(s). Rooms and homes report purchased and replacement totals.
- `POST /items/:id/image?kind=photo|similar` with `{data: "<base64 data URL>"}`
- `POST /identify` with `{data: <image data URL>, provider: "ollama" | "anthropic"}` returns `{name, brand, item_type, description, search_query, provider, model}` for you to review before saving (save with `source: "vision"`)
- `POST /classify` `{data, provider?}` -> `{kind: receipt|item|other}`; `POST /receipt` `{data, provider?}` -> store, date, totals, `items`, `warnings` (see `receipt_module/`)
- `POST /link-info` `{url}` -> `{price, currency, title, host, source}` (price is `null` when the page does not publish one)
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

## Adding items
- **Add item:** a compact form with the photo on the left and the fields on the right. Choosing a photo runs the local model
  (a spinning "LLM working" wheel shows over the photo) and fills in name, brand, type and description for you to check.
- **Add items by image:** choose up to 20 photos at once. Each is classified as an item photo or a **receipt**. Item photos become
  one row each; receipts are read line by line (quantity, price, category). Everything lands in one review table where you edit,
  untick and set quantities; nothing is saved until you click *Add*. Food and supplies on a receipt start unticked.
  "Read as receipt / item" re-reads an image if it was classified wrongly.
- Receipt reading lives in a standalone, reusable package: [`receipt_module/`](receipt_module/README.md).

## Item screen and editing
The room page is split 50/50: item details on the left (photos beside the facts), the item list on the right. The list shows **10 or 25 items per
page** (your choice is remembered) with Prev / page numbers / Next; room totals always cover every item, not just the page. After you add or save an
item, the list jumps to the page it is on.

**Edit** (top of the item details) opens a drawer that slides in from the right, with Save, Cancel and Delete at the top. Esc, Cancel or a click
outside asks first if there are unsaved changes. The drawer is the shared tray component from the design system (`static/ds/components/tray/`).
To try the other styles, change `TRAY_VARIANT` in `static/index.html` to `"grow"` (widens out of the details panel) or `"instant"` (no motion).

## Find similar, and prices from links
In the item pane, **Find similar** asks a few questions (brand or any brand, model/size/color, new or used, max price) and opens the
store search you choose (Google Shopping, Amazon, Walmart, Target, Best Buy, eBay or the web) in a new tab. Only your search words leave
your computer, and only when you click. Price and condition filters are added to the address where the store supports them; I could not
check every store's parameters, so treat them as a convenience.

Paste a product link (in the pane, or into *Link to a similar item* in the form) and the app reads the page for its price
(`POST /api/link-info`). It uses structured data the site publishes (JSON-LD, Open Graph / product meta tags) and never guesses from
page text, so some sites return nothing, and many large retailers block automated reads or load prices with JavaScript; enter the price
by hand then. USD prices are offered as the replacement value; other currencies are shown but not used. The page fetch refuses
private/local addresses, re-checks every redirect, reads at most 1.5 MB and only HTML. It does not use a system proxy.

## Roadmap
1. Compare candidate vision models on your own photos (small bake-off script).
2. Phone access (PWA over LAN) with auth.
3. Search/filter, CSV export for insurance, per-home totals report.
