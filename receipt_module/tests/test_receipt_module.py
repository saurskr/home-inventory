"""Run from the folder that contains receipt_module/:  python3 -m unittest discover -s receipt_module/tests -t ."""
import base64
import contextlib
import http.server
import io
import json
import os
import threading
import unittest

from receipt_module import (AnthropicProvider, OllamaProvider, ProviderError, ReceiptError, classify_image,
                            extract_receipt, load_image, normalize_date, normalize_receipt, parse_json_loose,
                            to_money)
from receipt_module import __main__ as cli

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 24
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class FakeProvider:
    name = "fake"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat_json(self, prompt, schema, image_b64, media_type):
        self.calls.append((prompt, schema, image_b64, media_type))
        return self.reply


class Numbers(unittest.TestCase):
    def test_money(self):
        for raw, want in [(12.5, 12.5), ("$1,299.00", 1299.0), ("12,99", 12.99), ("1.299,00", 1299.0),
                          ("1,299", 1299.0), ("(4.50)", -4.5), ("-3", -3.0), ("abc", None), ("", None),
                          (None, None), (True, None), ("USD 7.5", 7.5)]:
            self.assertEqual(to_money(raw), want, raw)

    def test_dates(self):
        for raw, want in [("2026-03-02", "2026-03-02"), ("03/02/2026", "2026-03-02"), ("3/2/26", "2026-03-02"),
                          ("Mar 2, 2026", "2026-03-02"), ("March 2, 2026 14:31", "2026-03-02"),
                          ("2 Mar 2026", "2026-03-02"), ("yesterday", ""), ("", ""), (None, ""),
                          ("03/02/1850", "")]:
            self.assertEqual(normalize_date(raw), want, raw)


class Images(unittest.TestCase):
    def test_load(self):
        self.assertEqual(load_image(PNG)[0], "image/png")
        self.assertEqual(load_image(PNG_URL)[0], "image/png")
        self.assertEqual(load_image(base64.b64encode(b"\xff\xd8\xff\xe0" + b"1" * 20).decode())[0], "image/jpeg")
        for bad in (b"", "data:image/png;base64,@@@", b"not an image", 5):
            with self.assertRaises(ReceiptError):
                load_image(bad)
        with self.assertRaises(ReceiptError):
            load_image(b"\x89PNG\r\n\x1a\n" + b"0" * (16 * 2**20))


RAW = {"store": " Target  Store ", "date": "03/02/2026", "currency": "usd", "subtotal": 474.97, "tax": 33.76,
       "total": 508.73, "line_items": [
           {"description": "Toaster 2 slice", "quantity": 1, "unit_price": 29.99, "total_price": 29.99, "category": "appliance"},
           {"description": "Nightstand", "quantity": 2, "unit_price": "$179.99", "total_price": 0, "category": "Furniture"},
           {"description": "TV 55in", "quantity": 1, "unit_price": 0, "total_price": "32.00", "category": "Electronics"},
           {"description": "Bananas", "quantity": 1, "unit_price": 1.2, "total_price": 1.2, "category": "Consumable"},
           {"description": "SUBTOTAL", "quantity": 1, "unit_price": 421.97, "total_price": 421.97, "category": "Other"},
           {"description": "Coupon", "quantity": 1, "unit_price": -5, "total_price": -5, "category": "Other"},
           {"description": "", "quantity": 1, "unit_price": 1, "total_price": 1, "category": "Other"},
           {"description": "Mystery thing", "quantity": "x", "unit_price": 3, "total_price": 3, "category": "Weird"}]}


class Normalize(unittest.TestCase):
    def setUp(self):
        self.r = normalize_receipt(json.dumps(RAW), ["Appliance", "Furniture", "Electronics", "Consumable", "Other"])

    def test_header(self):
        self.assertEqual((self.r["store"], self.r["date"], self.r["currency"]), ("Target Store", "2026-03-02", "USD"))
        self.assertEqual((self.r["subtotal"], self.r["tax"], self.r["total"]), (474.97, 33.76, 508.73))

    def test_items_cleaned(self):
        names = [i["description"] for i in self.r["items"]]
        self.assertEqual(names, ["Toaster 2 slice", "Nightstand", "TV 55in", "Bananas", "Mystery thing"])
        by = {i["description"]: i for i in self.r["items"]}
        self.assertEqual(by["Toaster 2 slice"]["category"], "Appliance")                    # case fixed
        self.assertEqual((by["Nightstand"]["quantity"], by["Nightstand"]["unit_price"], by["Nightstand"]["total_price"]),
                         (2, 179.99, 359.98))                                                 # total derived
        self.assertEqual(by["TV 55in"]["unit_price"], 32.0)                                   # unit derived
        self.assertEqual(by["Mystery thing"]["category"], "Other")                            # unknown category
        self.assertEqual(by["Mystery thing"]["quantity"], 1)
        self.assertEqual([a["description"] for a in self.r["adjustments"]], ["Coupon"])       # discounts split out

    def test_total_check_warns(self):
        self.assertTrue(any("add up" in w for w in self.r["warnings"]), self.r["warnings"])
        ok = normalize_receipt({"store": "S", "line_items": [
            {"description": "A", "quantity": 1, "unit_price": 10, "total_price": 10, "category": "Other"}],
            "subtotal": 10, "total": 10})
        self.assertEqual(ok["warnings"], [])

    def test_bad_inputs(self):
        with self.assertRaises(ReceiptError):
            normalize_receipt("no json at all")
        with self.assertRaises(ReceiptError):
            normalize_receipt(None)
        empty = normalize_receipt({"store": "S", "line_items": []})
        self.assertEqual(empty["items"], [])
        self.assertTrue(any("No line items" in w for w in empty["warnings"]))
        self.assertEqual(normalize_receipt({"line_items": "oops"})["items"], [])

    def test_loose_json(self):
        self.assertEqual(parse_json_loose('Sure! {"a": 1} hope that helps'), {"a": 1})
        self.assertIsNone(parse_json_loose("[1,2]"))


class Flows(unittest.TestCase):
    def test_classify(self):
        for reply, want in [({"kind": "Receipt"}, "receipt"), ('{"kind":"item"}', "item"), ({"kind": "dog"}, "other"),
                            (None, "other"), ("garbage", "other")]:
            self.assertEqual(classify_image(FakeProvider(reply), PNG), want, reply)

    def test_extract_sends_schema_and_image(self):
        p = FakeProvider(RAW)
        cats = ["Appliance", "Other"]
        out = extract_receipt(p, PNG_URL, cats)
        prompt, schema, b64, media = p.calls[0]
        self.assertEqual(media, "image/png")
        self.assertEqual(b64, base64.b64encode(PNG).decode())
        self.assertEqual(schema["properties"]["line_items"]["items"]["properties"]["category"]["enum"], cats)
        self.assertIn("Appliance", prompt)
        self.assertEqual(out["store"], "Target Store")
        with self.assertRaises(ReceiptError):
            extract_receipt(FakeProvider("nonsense"), PNG)


class FakeServer:
    def __init__(self):
        self.requests, self.status, self.reply = [], 200, {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, json.loads(self.rfile.read(n)), {k.lower(): v for k, v in self.headers.items()}))
                raw = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Providers(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self.addCleanup(self.fake.close)

    def test_ollama(self):
        self.fake.reply = {"message": {"content": '{"kind": "receipt"}'}}
        p = OllamaProvider(self.fake.url, "qwen2.5vl:7b")
        self.assertEqual(classify_image(p, PNG), "receipt")
        path, body, _ = self.fake.requests[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual((body["model"], body["stream"], body["options"]["temperature"]), ("qwen2.5vl:7b", False, 0))
        self.assertEqual(body["format"]["properties"]["kind"]["enum"], ["receipt", "item", "other"])
        self.assertEqual(body["messages"][0]["images"], [base64.b64encode(PNG).decode()])

    def test_ollama_errors(self):
        self.fake.status = 404
        with self.assertRaises(ProviderError) as c:
            OllamaProvider(self.fake.url, "m:1").chat_json("p", {}, "x")
        self.assertEqual(c.exception.kind, "model_missing")
        self.assertIn("ollama pull m:1", c.exception.message)
        with self.assertRaises(ProviderError) as c:
            OllamaProvider("http://127.0.0.1:9", "m").chat_json("p", {}, "x")
        self.assertEqual(c.exception.kind, "unreachable")

    def test_ollama_text_only_model(self):
        self.fake.status = 400
        self.fake.reply = {"error": "model does not support images"}
        with self.assertRaises(ProviderError) as c:
            OllamaProvider(self.fake.url, "llama3.2:3b").chat_json("p", {}, "x")
        self.assertEqual(c.exception.kind, "no_vision")
        self.assertIn("llama3.2:3b can't read images", c.exception.message)
        self.fake.reply = {"error": "bad request"}
        with self.assertRaises(ProviderError) as c:
            OllamaProvider(self.fake.url, "m").chat_json("p", {}, "x")
        self.assertEqual(c.exception.kind, "http")

    def test_anthropic(self):
        self.fake.reply = {"content": [{"type": "tool_use", "name": "record", "input": {"kind": "item"}}]}
        p = AnthropicProvider("sk-ant-secret-key-1234567890", "claude-haiku-5-5", self.fake.url)
        self.assertEqual(classify_image(p, PNG), "item")
        path, body, headers = self.fake.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-ant-secret-key-1234567890")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "record"})
        self.assertEqual(body["messages"][0]["content"][0]["source"]["media_type"], "image/png")

    def test_anthropic_errors_hide_key(self):
        for status, kind in ((401, "auth"), (429, "rate_limit"), (404, "model_missing"), (500, "http")):
            self.fake.status = status
            with self.assertRaises(ProviderError) as c:
                AnthropicProvider("sk-ant-secret-key-1234567890", "m", self.fake.url).chat_json("p", {}, "x")
            self.assertEqual(c.exception.kind, kind)
            self.assertNotIn("sk-ant", c.exception.message)


class Cli(unittest.TestCase):
    def test_cli_end_to_end(self):
        fake = FakeServer()
        self.addCleanup(fake.close)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(PNG)
        self.addCleanup(os.remove, fh.name)
        fake.reply = {"message": {"content": json.dumps(RAW)}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([fh.name, "--url", fake.url, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["receipt"]["items"][0]["description"], "Toaster 2 slice")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["/no/such/file.png"]), 2)
            self.assertEqual(cli.main([fh.name, "--url", "http://127.0.0.1:9"]), 1)


if __name__ == "__main__":
    unittest.main()
