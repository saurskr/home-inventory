// Photo reading from the browser: Ollama on your own computer, or Claude with your own key.
// A port of receipt_module (providers, classify, receipt extraction and clean-up).

export class ProviderError extends Error {
  constructor(message, kind = "http", status = null) { super(message); this.kind = kind; this.status = status; }
}

const jsonFetch = async (fetchImpl, url, payload, headers = {}, timeoutMs = 60000, method = "POST") => {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, {method, signal: ctl.signal, headers: payload === undefined ? headers : {"Content-Type": "application/json", ...headers},
      body: payload === undefined ? undefined : JSON.stringify(payload)});
    const body = await res.text();
    if (!res.ok) { const e = new Error(body.slice(0, 2000)); e.status = res.status; e.detail = body.slice(0, 2000); throw e; }
    return JSON.parse(body);
  } catch (e) {
    if (e.name === "AbortError") { const x = new Error("timed out"); x.timeout = true; throw x; }
    throw e;
  } finally { clearTimeout(t); }
};

export class OllamaProvider {
  constructor(fetchImpl, baseUrl = "http://localhost:11434", model = "qwen2.5vl:7b") {
    this.fetch = fetchImpl; this.baseUrl = baseUrl.replace(/\/+$/, ""); this.model = model;
  }
  async chatJson(prompt, schema, imageB64) {
    try {
      const out = await jsonFetch(this.fetch, this.baseUrl + "/api/chat", {
        model: this.model, stream: false, format: schema, options: {temperature: 0},
        messages: [{role: "user", content: prompt, images: [imageB64]}]}, {}, 300000);
      return (out.message || {}).content || "";
    } catch (e) {
      if (e.timeout) throw new ProviderError("Ollama took longer than 5 minutes. Try a smaller model such as qwen2.5vl:3b.", "timeout");
      if (e.status === undefined) throw new ProviderError(`Could not reach Ollama at ${this.baseUrl}. Is it running, and does it allow this website? Start it with OLLAMA_ORIGINS="*" ollama serve`, "unreachable");
      if (e.status === 404) throw new ProviderError(`Model '${this.model}' is not installed in Ollama. Run: ollama pull ${this.model}`, "model_missing", 404);
      if (e.status === 400 && /image|vision|multimodal/i.test(e.detail || "")) throw new ProviderError(`${this.model} can't read images. Choose a vision model (for example qwen2.5vl).`, "no_vision", 400);
      throw new ProviderError(`Ollama returned an error (HTTP ${e.status}).`, "http", e.status);
    }
  }
}

export class AnthropicProvider {
  constructor(fetchImpl, apiKey, model = "claude-haiku-5-5", baseUrl = "https://api.anthropic.com") {
    this.fetch = fetchImpl; this.apiKey = apiKey; this.model = model; this.baseUrl = baseUrl.replace(/\/+$/, "");
  }
  async chatJson(prompt, schema, imageB64, media = "image/jpeg") {
    try {
      const out = await jsonFetch(this.fetch, this.baseUrl + "/v1/messages", {
        model: this.model, max_tokens: 2000, tools: [{name: "record", description: "Record the answer.", input_schema: schema}],
        tool_choice: {type: "tool", name: "record"},
        messages: [{role: "user", content: [{type: "image", source: {type: "base64", media_type: media, data: imageB64}}, {type: "text", text: prompt}]}]},
        {"x-api-key": this.apiKey, "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true"}, 90000);
      const block = (out.content || []).find(c => c.type === "tool_use");
      return block ? block.input : null;
    } catch (e) {
      if (e.timeout || e.status === undefined) throw new ProviderError("Could not reach the Anthropic API. Check your internet connection.", "unreachable");
      if (e.status === 401 || e.status === 403) throw new ProviderError("Anthropic rejected the API key.", "auth", e.status);
      if (e.status === 404) throw new ProviderError(`Anthropic does not know the model '${this.model}'.`, "model_missing", 404);
      if (e.status === 429) throw new ProviderError("Anthropic rate limit or spending limit reached. Try again later.", "rate_limit", 429);
      throw new ProviderError(`Anthropic returned an error (HTTP ${e.status}).`, "http", e.status);
    }
  }
}

// ---------- model output ----------
export function parseJsonLoose(content) {
  if (content && typeof content === "object" && !Array.isArray(content)) return content;
  if (typeof content !== "string") return null;
  try { const o = JSON.parse(content); return o && typeof o === "object" && !Array.isArray(o) ? o : null; } catch (e) { /* fall through */ }
  const m = /\{[\s\S]*\}/.exec(content);
  if (!m) return null;
  try { const o = JSON.parse(m[0]); return o && typeof o === "object" && !Array.isArray(o) ? o : null; } catch (e) { return null; }
}

export const KINDS = ["receipt", "item", "other"];
export const CLASSIFY_SCHEMA = {type: "object", properties: {kind: {type: "string", enum: KINDS}}, required: ["kind"]};
export const CLASSIFY_PROMPT =
  "Look at this image and decide what it is.\n" +
  "- receipt: a store receipt, invoice, order confirmation or packing slip that lists purchased things with prices (paper or screenshot).\n" +
  "- item: a photo of a physical object, such as an appliance, furniture or electronics.\n" +
  "- other: anything else (a person, a landscape, a blank page, a document with no purchases).\n" +
  "Answer with the kind only.";

export function receiptSchema(categories) {
  const line = {type: "object", properties: {description: {type: "string"}, quantity: {type: "number"}, unit_price: {type: "number"},
    total_price: {type: "number"}, category: {type: "string", enum: categories}}, required: ["description", "quantity", "unit_price", "total_price", "category"]};
  return {type: "object", properties: {store: {type: "string"}, date: {type: "string"}, currency: {type: "string"}, subtotal: {type: "number"},
    tax: {type: "number"}, total: {type: "number"}, line_items: {type: "array", items: line}}, required: ["store", "date", "currency", "subtotal", "tax", "total", "line_items"]};
}
export const receiptPrompt = categories =>
  "This is a purchase receipt. Read it carefully and list every purchased line item.\n" +
  "- store: the merchant name as printed.\n- date: the purchase date as printed (any format), or '' if not visible.\n" +
  "- currency: ISO code such as USD (use USD if you see $ and nothing says otherwise).\n" +
  "- subtotal, tax, total: the printed amounts, or 0 if not shown.\n" +
  "- line_items: one entry per purchased product. Give the product name in plain words (expand abbreviations when you can). quantity is how many were bought (1 if not shown). unit_price is the price of one, total_price the line total, 0 if not shown. Do NOT include subtotal, tax, total, change, payment or discount lines as items.\n" +
  "- category: one of " + categories.join(", ") + ". Use Consumable for food, toiletries and other things used up; Service for labour, delivery or fees.\n" +
  "Copy numbers exactly. Do not invent items you cannot read.";

const NON_ITEM = /^\W*(sub\s*-?\s*total|total|tax|sales\s+tax|balance|amount\s+due|change|cash|visa|mastercard|amex|discover|debit|credit|card|tender|payment|thank\s+you|savings|you\s+saved|rounding)\b/i;

export function toMoney(v) {
  if (typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isFinite(v) ? Math.round(v * 100) / 100 : null;
  if (typeof v !== "string") return null;
  let s = v.trim().replace(/[^\d.,\-()]/g, "");
  if (!s || !/\d/.test(s)) return null;
  const neg = s.startsWith("-") || (s.startsWith("(") && s.endsWith(")"));
  s = s.replace(/^[-()]+|[-()]+$/g, "");
  if (s.includes(",") && s.includes(".")) s = s.lastIndexOf(".") > s.lastIndexOf(",") ? s.replace(/,/g, "") : s.replace(/\./g, "").replace(",", ".");
  else if (s.includes(",")) s = /,\d{1,2}$/.test(s) ? s.replace(",", ".") : s.replace(/,/g, "");
  const f = Number(s);
  if (!Number.isFinite(f)) return null;
  const r = Math.round(f * 100) / 100;
  return neg ? -r : r;
}
const toQty = v => { const q = toMoney(v); return q && q > 0 ? Math.max(1, Math.min(999, Math.round(q))) : 1; };

const MONTHS = {jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12};
export function normalizeDate(v, today = new Date()) {
  if (typeof v !== "string") return "";
  let s = v.trim().replace(/\s+/g, " ").split(/\s+\d{1,2}:\d{2}/)[0].trim();
  let y, m, d, r;
  if ((r = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(s))) [y, m, d] = [r[1], r[2], r[3]];
  else if ((r = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})$/.exec(s))) { [m, d, y] = [r[1], r[2], r[3]]; if (y.length === 2) y = "20" + y; }
  else if ((r = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s))) [d, m, y] = [r[1], r[2], r[3]];
  else if ((r = /^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$/.exec(s))) [m, d, y] = [MONTHS[r[1].slice(0, 3).toLowerCase()], r[2], r[3]];
  else if ((r = /^(\d{1,2}) ([A-Za-z]{3,9})\.? (\d{4})$/.exec(s))) [d, m, y] = [r[1], MONTHS[r[2].slice(0, 3).toLowerCase()], r[3]];
  else return "";
  y = +y; m = +m; d = +d;
  const dt = new Date(y, m - 1, d);
  if (!m || dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) return "";
  if (y < 1990 || y > today.getFullYear() + 1) return "";
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

export const DEFAULT_CATEGORIES = ["Appliance", "Furniture", "Electronics", "Clothing", "Media", "Collectible", "Consumable", "Service", "Other"];

export function normalizeReceipt(rawIn, categories = DEFAULT_CATEGORIES) {
  const raw = parseJsonLoose(rawIn);
  if (!raw) { const e = new Error("The model did not return a usable receipt. Try again with a clearer, closer photo."); e.status = 502; throw e; }
  const warnings = [], items = [], adjustments = [];
  for (const ln of (Array.isArray(raw.line_items) ? raw.line_items : []).slice(0, 200)) {
    if (!ln || typeof ln !== "object") continue;
    const desc = String(ln.description || "").replace(/\s+/g, " ").trim().slice(0, 160);
    if (!desc || NON_ITEM.test(desc)) continue;
    const qty = toQty(ln.quantity); let unit = toMoney(ln.unit_price) || null, total = toMoney(ln.total_price) || null;
    if (total === null && unit !== null) total = Math.round(unit * qty * 100) / 100;
    else if (unit === null && total !== null) unit = Math.round(total / qty * 100) / 100;
    const c = String(ln.category || "").trim().toLowerCase();
    const cat = categories.find(x => x.toLowerCase() === c) || (categories.includes("Other") ? "Other" : categories[categories.length - 1]);
    const row = {description: desc, quantity: qty, unit_price: unit, total_price: total, category: cat};
    (total !== null && total < 0 ? adjustments : items).push(row);
  }
  const out = {store: String(raw.store || "").replace(/\s+/g, " ").trim().slice(0, 80), date: normalizeDate(raw.date),
    currency: (String(raw.currency || "USD").trim().toUpperCase().slice(0, 3) || "USD"),
    subtotal: toMoney(raw.subtotal) || null, tax: toMoney(raw.tax) || null, total: toMoney(raw.total) || null, items, adjustments, warnings};
  if (!items.length) warnings.push("No line items could be read from this receipt.");
  else {
    const got = Math.round((items.reduce((a, i) => a + (i.total_price || 0), 0) + adjustments.reduce((a, i) => a + (i.total_price || 0), 0)) * 100) / 100;
    const want = out.subtotal ? out.subtotal : (out.total ? Math.round((out.total - (out.tax || 0)) * 100) / 100 : null);
    if (want && Math.abs(got - want) > Math.max(1, 0.01 * want))
      warnings.push(`The items add up to ${got.toFixed(2)} but the receipt shows ${want.toFixed(2)}. Some lines may be missing or misread; check them.`);
    if (items.some(i => i.total_price === null)) warnings.push("Some line prices could not be read.");
  }
  if (raw.date && !out.date) warnings.push("The purchase date could not be understood.");
  return out;
}

// ---------- the three things the app asks for ----------
export const itemSchema = types => ({type: "object", properties: {name: {type: "string"}, brand: {type: "string"}, model_number: {type: "string"},
  item_type: {type: "string", enum: [...types, "Other"]}, description: {type: "string"}, search_query: {type: "string"}},
  required: ["name", "brand", "model_number", "item_type", "description", "search_query"]});

export const itemPrompt = (types, descMax = 250) =>
  "You are cataloging household belongings for a home inventory. Look at the photo and identify the single main item. Answer with the requested fields only.\n" +
  "- name: short common name, e.g. 'Stand Mixer'.\n" +
  "- brand: the manufacturer only if a logo or text shows it or you are certain; otherwise ''.\n" +
  "- model_number: the model number only if it is printed on the item or its label; otherwise ''.\n" +
  "- item_type: one of " + types.join(", ") + ", or Other.\n" +
  `- description: one factual sentence of at most ${descMax} characters (colour, material, size). Do not guess details you cannot see.\n` +
  "- search_query: a web search query that would find a comparable replacement.";

export function parseItem(content, types, descMax = 250) {
  const o = parseJsonLoose(content);
  const str = (v, n) => typeof v === "string" ? v.trim().slice(0, n) : "";
  if (!o || !str(o.name, 120)) { const e = new Error("The model did not return a usable item. Try again or use a clearer photo."); e.status = 502; throw e; }
  const t = str(o.item_type, 40).toLowerCase();
  return {name: str(o.name, 120), brand: str(o.brand, 80), model_number: str(o.model_number, 60),
    item_type: types.find(x => x.toLowerCase() === t) || null, description: str(o.description, descMax), search_query: str(o.search_query, 200)};
}

const b64Of = url => { const m = /^data:([\w/+.-]+);base64,(.*)$/s.exec(url || ""); return m ? {media: m[1], b64: m[2]} : null; };

export function makeProvider(fetchImpl, settings, which) {
  if (which === "anthropic") {
    if (!settings.anthropic_key) { const e = new Error("Claude is not set up. Add your API key in Settings."); e.status = 400; throw e; }
    return [new AnthropicProvider(fetchImpl, settings.anthropic_key, settings.anthropic_model), settings.anthropic_model];
  }
  return [new OllamaProvider(fetchImpl, settings.ollama_url, settings.ollama_model), settings.ollama_model];
}

const wrapErr = async fn => {
  try { return await fn(); } catch (e) {
    if (e instanceof ProviderError) {
      const err = new Error(e.message); err.status = {auth: 400, no_vision: 400, model_missing: 400, rate_limit: 429}[e.kind] || 502; throw err;
    }
    throw e;
  }
};

export async function identify(fetchImpl, settings, body, types) {
  const which = body.provider || "ollama";
  if (!["ollama", "anthropic"].includes(which)) { const e = new Error("provider must be ollama or anthropic"); e.status = 400; throw e; }
  const img = b64Of(body.data);
  if (!img) { const e = new Error("data must be a base64 image data URL"); e.status = 400; throw e; }
  const [prov, model] = makeProvider(fetchImpl, settings, which);
  const raw = await wrapErr(() => prov.chatJson(itemPrompt(types), itemSchema(types), img.b64, img.media));
  return {...parseItem(raw, types), model, provider: which};
}
export async function classify(fetchImpl, settings, body) {
  const which = body.provider || "ollama"; const img = b64Of(body.data);
  if (!img) { const e = new Error("data must be a base64 image data URL"); e.status = 400; throw e; }
  const [prov, model] = makeProvider(fetchImpl, settings, which);
  const raw = await wrapErr(() => prov.chatJson(CLASSIFY_PROMPT, CLASSIFY_SCHEMA, img.b64, img.media));
  const kind = String((parseJsonLoose(raw) || {}).kind || "").trim().toLowerCase();
  return {kind: KINDS.includes(kind) ? kind : "other", provider: which, model};
}
export async function receipt(fetchImpl, settings, body, categories) {
  const which = body.provider || "ollama"; const img = b64Of(body.data);
  if (!img) { const e = new Error("data must be a base64 image data URL"); e.status = 400; throw e; }
  const [prov, model] = makeProvider(fetchImpl, settings, which);
  const raw = await wrapErr(() => prov.chatJson(receiptPrompt(categories), receiptSchema(categories), img.b64, img.media));
  return {...normalizeReceipt(raw, categories), provider: which, model};
}

export async function ollamaStatus(fetchImpl, settings) {
  const url = settings.ollama_url.replace(/\/+$/, "");
  const out = {url, model: settings.ollama_model, reachable: false, model_installed: false};
  try {
    const tags = await jsonFetch(fetchImpl, url + "/api/tags", undefined, {}, 3000, "GET");
    out.reachable = true;
    out.model_installed = (tags.models || []).some(m => [m.name, m.model].some(n => n === settings.ollama_model || n === settings.ollama_model + ":latest"));
  } catch (e) { /* unreachable */ }
  return out;
}
export async function ollamaModels(fetchImpl, settings) {
  const base = settings.ollama_url.replace(/\/+$/, "");
  const out = {url: base, reachable: false, current: settings.ollama_model, models: []};
  let tags;
  try { tags = await jsonFetch(fetchImpl, base + "/api/tags", undefined, {}, 3000, "GET"); } catch (e) { return out; }
  out.reachable = true;
  for (const m of (tags.models || []).slice(0, 40)) {
    const name = m.name || m.model; if (!name) continue;
    const d = m.details || {};
    const entry = {name, size: m.size, parameter_size: d.parameter_size || "", quantization: d.quantization_level || "", family: d.family || "", vision: null};
    try {
      const info = await jsonFetch(fetchImpl, base + "/api/show", {model: name, name}, {}, 3000);
      if (Array.isArray(info.capabilities)) entry.vision = info.capabilities.includes("vision");
      else { const fam = [(info.details || {}).family, ...((info.details || {}).families || [])].filter(Boolean).join(" ").toLowerCase();
        entry.vision = /clip|mllama|vl|llava|vision/.test(fam) || ("projector_info" in info) ? true : null; }
    } catch (e) { /* leave unknown */ }
    out.models.push(entry);
  }
  return out;
}
