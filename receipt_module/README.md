# receipt_module

Read receipts with a **local or cloud vision model**, and tell them apart from ordinary photos.
Standard library only, Python 3.9+, no app code inside. Copy this folder into any project.

```python
from receipt_module import OllamaProvider, classify_image, extract_receipt

provider = OllamaProvider("http://localhost:11434", "qwen2.5vl:7b")   # free, runs on your computer
kind = classify_image(provider, open("photo.jpg", "rb").read())        # "receipt" | "item" | "other"
if kind == "receipt":
    r = extract_receipt(provider, open("photo.jpg", "rb").read(),
                        categories=["Appliance", "Furniture", "Electronics", "Consumable", "Other"])
    for line in r["items"]:
        print(line["description"], line["quantity"], line["total_price"], line["category"])
```

Claude, bring your own key: `AnthropicProvider(api_key, "claude-haiku-5-5")`. Everything else is identical.

## Command line
```bash
python3 -m receipt_module receipt.jpg                       # classify, then read it if it is a receipt
python3 -m receipt_module receipt.jpg --force               # skip classification
python3 -m receipt_module receipt.jpg --classify-only
ANTHROPIC_API_KEY=... python3 -m receipt_module receipt.jpg --provider anthropic
```

## API
| Function | What it does |
|---|---|
| `classify_image(provider, image)` | `"receipt"`, `"item"` or `"other"` (anything unclear becomes `"other"`). |
| `extract_receipt(provider, image, categories=None)` | Reads the receipt and returns the dict below. |
| `normalize_receipt(raw, categories=None)` | The cleaning step on its own, for model output you already have. |
| `load_image(x)` | Accepts bytes, a data URL or base64 text; checks type and size (15 MB). |
| `to_money`, `normalize_date`, `parse_json_loose` | Small helpers. |

`image` may be bytes, a data URL (`data:image/jpeg;base64,...`) or base64 text.
`categories` is your own list; each line is forced onto one of them (unknown becomes `"Other"` if present).

### What `extract_receipt` returns
```json
{
  "store": "Target", "date": "2026-03-02", "currency": "USD",
  "subtotal": 474.97, "tax": 33.76, "total": 508.73,
  "items": [
    {"description": "Nightstand", "quantity": 2, "unit_price": 179.99, "total_price": 359.98, "category": "Furniture"}
  ],
  "adjustments": [{"description": "Coupon", "quantity": 1, "unit_price": -5.0, "total_price": -5.0, "category": "Other"}],
  "warnings": ["The items add up to 421.17 but the receipt shows 474.97. Some lines may be missing or misread; check them."]
}
```
- Numbers are plain floats; unknown ones are `None`. `date` is `YYYY-MM-DD` or `""`.
- Missing unit or line prices are derived from quantity. Subtotal, tax, total, payment and change lines are dropped.
  Negative lines (coupons, discounts) go to `adjustments`.
- `warnings` flags a total mismatch, unreadable prices, an unparseable date, or no items. **Always show them to the user.**

## Errors
- `ReceiptError(message, kind)`: unreadable image (`bad_image`) or a model answer that could not be used (`bad_output`).
- `ProviderError(message, kind)`: `unreachable`, `timeout`, `model_missing`, `auth`, `rate_limit`, `http`.
  Messages are written for end users and never contain the API key.

## Providers
`provider.chat_json(prompt, schema, image_b64, media_type)` is the whole interface, so another provider
(or a fake for tests) is a few lines. Ollama uses structured output (`format` = JSON schema, temperature 0);
Claude uses a forced tool call with the same schema.

## Practical notes
- **Photo quality decides accuracy.** Flat, well lit, receipt filling the frame. Send about 1600 px on the long side:
  smaller loses small print, much larger is slow on a small GPU. A very long receipt is best photographed in two parts.
- A 7B local model can misread digits. The totals check catches many errors; it is not a guarantee. Keep a human review step.
- Receipts hold personal data (card digits, addresses). With a cloud provider the image leaves the computer: ask first.
- Ollama must have the model: `ollama pull qwen2.5vl:7b`.

## Tests
```bash
python3 -m unittest discover -s receipt_module/tests -t .
```
They use a fake provider and fake local servers, so no model or internet is needed.
