#!/usr/bin/env python3
"""API tests: python3 test_api.py (starts a throwaway server on a free port)."""
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ["INVENTORY_QUIET"] = "1"
import server

PNG = "data:image/png;base64," + base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = server.make_server(port=0, data_dir=cls.tmp.name)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, expect=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        if expect is not None:
            self.assertEqual(status, expect, raw)
        return json.loads(raw) if raw else None

    def home(self, name="Main"):
        return self.call("POST", "/api/homes", {"name": name}, 201)

    def room(self, home_id, name="Den", **kw):
        return self.call("POST", "/api/homes/%d/rooms" % home_id,
                         dict(name=name, **kw), 201)

    def test_meta_and_static(self):
        m = self.call("GET", "/api/meta", expect=200)
        self.assertIn("Appliance", m["item_types"])
        self.assertEqual(len(m["room_templates"]["Kitchen"]), 5)
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Home Inventory", r.read())

    def test_home_crud_and_validation(self):
        h = self.home("Beach House")
        self.call("PUT", "/api/homes/%d" % h["id"], {"address": "1 Shore Rd"}, 200)
        got = self.call("GET", "/api/homes/%d" % h["id"], expect=200)
        self.assertEqual((got["name"], got["address"]), ("Beach House", "1 Shore Rd"))
        self.call("POST", "/api/homes", {"name": "  "}, 400)
        self.call("GET", "/api/homes/99999", expect=404)
        self.call("DELETE", "/api/homes/%d" % h["id"], expect=200)
        self.call("GET", "/api/homes/%d" % h["id"], expect=404)

    def test_template_prefill(self):
        h = self.home()
        k = self.room(h["id"], "Kitchen", prefill=True)
        items = self.call("GET", "/api/rooms/%d/items" % k["id"], expect=200)
        self.assertEqual(sorted(i["name"] for i in items),
                         ["Blender", "Crockpot", "Microwave", "Refrigerator", "Toaster"])
        self.assertTrue(all(i["source"] == "template" for i in items))
        b = self.room(h["id"], "Master Bedroom", prefill=True)
        names = [i["name"] for i in self.call("GET", "/api/rooms/%d/items" % b["id"])]
        self.assertEqual(names.count("Nightstand"), 2)
        self.call("POST", "/api/homes/%d/rooms" % h["id"],
                  {"name": "Attic", "prefill": True}, 400)
        rooms = self.call("GET", "/api/homes/%d/rooms" % h["id"])
        self.assertNotIn("Attic", [r["name"] for r in rooms])

    def test_item_crud_defaults(self):
        h = self.home()
        r = self.room(h["id"])
        base = "/api/rooms/%d/items" % r["id"]
        a = self.call("POST", base, {"name": "Lamp", "purchased_value": 40,
                                     "item_type": "Furniture"}, 201)
        self.assertRegex(a["inv_no"], r"^INV-\d{6}$")
        self.assertEqual(a["replacement_value"], 40)
        self.assertEqual(a["source"], "manual")
        b = self.call("POST", base, {"name": "TV", "purchased_value": "300",
                                     "replacement_value": 450}, 201)
        self.assertNotEqual(a["inv_no"], b["inv_no"])
        self.assertEqual(b["replacement_value"], 450)
        self.call("POST", base, {"name": "X", "item_type": "Bogus"}, 400)
        self.call("POST", base, {"name": "X", "purchased_value": -1}, 400)
        self.call("POST", base, {"purchased_value": 1}, 400)
        u = self.call("PUT", "/api/items/%d" % a["id"],
                      {"brand": "IKEA", "replacement_value": 55}, 200)
        self.assertEqual((u["brand"], u["replacement_value"], u["name"]),
                         ("IKEA", 55, "Lamp"))
        self.call("DELETE", "/api/items/%d" % a["id"], expect=200)
        self.call("GET", "/api/items/%d" % a["id"], expect=404)

    def test_transfer_room(self):
        h1, h2 = self.home("A"), self.home("B")
        r = self.room(h1["id"], "Garage", prefill=True)
        self.call("PUT", "/api/rooms/%d" % r["id"], {"home_id": h2["id"]}, 200)
        self.assertEqual(self.call("GET", "/api/homes/%d/rooms" % h1["id"]), [])
        moved = self.call("GET", "/api/homes/%d/rooms" % h2["id"])
        self.assertEqual(moved[0]["item_count"], 3)
        self.call("PUT", "/api/rooms/%d" % r["id"], {"home_id": 99999}, 404)

    def test_cascade_and_image_cleanup(self):
        h = self.home()
        r = self.room(h["id"])
        it = self.call("POST", "/api/rooms/%d/items" % r["id"], {"name": "Vase"}, 201)
        up = self.call("POST", "/api/items/%d/image?kind=photo" % it["id"],
                       {"data": PNG}, 200)
        path = os.path.join(self.tmp.name, "images", up["image"])
        self.assertTrue(os.path.exists(path))
        with urllib.request.urlopen(self.base + "/images/" + up["image"]) as resp:
            self.assertEqual(resp.headers["Content-Type"], "image/png")
        self.call("POST", "/api/items/%d/image?kind=similar" % it["id"], {"data": PNG}, 200)
        self.call("POST", "/api/items/%d/image?kind=nope" % it["id"], {"data": PNG}, 400)
        self.call("POST", "/api/items/%d/image" % it["id"], {"data": "junk"}, 400)
        self.call("DELETE", "/api/homes/%d" % h["id"], expect=200)
        self.call("GET", "/api/rooms/%d" % r["id"], expect=404)
        self.call("GET", "/api/items/%d" % it["id"], expect=404)
        self.assertFalse(os.path.exists(path))

    def test_totals_and_misc(self):
        h = self.home()
        r = self.room(h["id"])
        self.call("POST", "/api/rooms/%d/items" % r["id"],
                  {"name": "A", "purchased_value": 10, "replacement_value": 20}, 201)
        self.call("POST", "/api/rooms/%d/items" % r["id"],
                  {"name": "B", "purchased_value": 5}, 201)
        got = self.call("GET", "/api/homes/%d" % h["id"])
        self.assertEqual((got["item_count"], got["total_purchased"],
                          got["total_replacement"]), (2, 15, 25))
        self.call("POST", "/api/identify", {}, 501)
        self.call("GET", "/api/nothing", expect=404)
        self.call("DELETE", "/api/meta", expect=405)
        try:
            urllib.request.urlopen(self.base + "/images/..%2F..%2Fserver.py")
            self.fail("traversal served a file")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


    def test_price_updated_stamp(self):
        h = self.home()
        r = self.room(h["id"], "Kitchen", prefill=True)
        tmpl = self.call("GET", "/api/rooms/%d/items" % r["id"])[0]
        self.assertIsNone(tmpl["price_updated_at"])
        base = "/api/rooms/%d/items" % r["id"]
        plain = self.call("POST", base, {"name": "A", "purchased_value": 10}, 201)
        self.assertIsNone(plain["price_updated_at"])
        priced = self.call("POST", base, {"name": "B", "replacement_value": 30}, 201)
        self.assertRegex(priced["price_updated_at"], r"^\d{4}-\d\d-\d\dT")
        same = self.call("PUT", "/api/items/%d" % plain["id"], {"replacement_value": 10}, 200)
        self.assertIsNone(same["price_updated_at"])
        changed = self.call("PUT", "/api/items/%d" % plain["id"], {"replacement_value": 12}, 200)
        self.assertIsNotNone(changed["price_updated_at"])
        conf = self.call("PUT", "/api/items/%d" % tmpl["id"], {"price_confirmed": True}, 200)
        self.assertIsNotNone(conf["price_updated_at"])
        self.assertEqual(conf["replacement_value"], tmpl["replacement_value"])

    def test_similar_url_validation(self):
        h = self.home()
        r = self.room(h["id"])
        base = "/api/rooms/%d/items" % r["id"]
        self.call("POST", base, {"name": "X", "similar_url": "javascript:alert(1)"}, 400)
        ok = self.call("POST", base, {"name": "X", "similar_url": "https://shop.example/x"}, 201)
        self.call("PUT", "/api/items/%d" % ok["id"], {"similar_url": "ftp://x"}, 400)
        self.call("PUT", "/api/items/%d" % ok["id"], {"similar_url": ""}, 200)

    def test_room_totals(self):
        h = self.home()
        r = self.room(h["id"])
        base = "/api/rooms/%d/items" % r["id"]
        self.call("POST", base, {"name": "A", "purchased_value": 10, "replacement_value": 20}, 201)
        self.call("POST", base, {"name": "B", "purchased_value": 5}, 201)
        got = self.call("GET", "/api/rooms/%d" % r["id"])
        self.assertEqual((got["total_purchased"], got["total_replacement"]), (15, 25))

    def test_insurance(self):
        h = self.home()
        path = "/api/homes/%d/insurance" % h["id"]
        empty = self.call("GET", path, expect=200)
        self.assertFalse(empty["configured"])
        self.assertIsNone(empty["contents_coverage"])
        got = self.call("PUT", path, {"insurer": "Acme", "contents_coverage": 50000,
                                      "renewal_date": "2027-03-01"}, 200)
        self.assertTrue(got["configured"])
        got = self.call("PUT", path, {"deductible": "1000"}, 200)  # merge, not replace
        self.assertEqual((got["insurer"], got["contents_coverage"], got["deductible"]),
                         ("Acme", 50000, 1000))
        self.call("PUT", path, {"renewal_date": "03/01/2027"}, 400)
        self.call("PUT", path, {"contents_coverage": -5}, 400)
        self.call("PUT", path, {"deductible": ""}, 200)
        self.assertIsNone(self.call("GET", path)["deductible"])
        self.call("DELETE", path, expect=200)
        self.assertFalse(self.call("GET", path)["configured"])
        self.call("PUT", path, {"insurer": "Acme"}, 200)
        self.call("DELETE", "/api/homes/%d" % h["id"], expect=200)
        self.call("GET", path, expect=404)

    def test_migrates_old_database(self):
        import sqlite3
        d = tempfile.mkdtemp()
        c = sqlite3.connect(os.path.join(d, "inventory.db"))
        c.executescript("""CREATE TABLE homes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT);
          CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, home_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '');
          CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, inv_no TEXT UNIQUE, room_id INTEGER NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '', purchased_value REAL NOT NULL DEFAULT 0,
            replacement_value REAL NOT NULL DEFAULT 0, similar_url TEXT NOT NULL DEFAULT '', item_type TEXT, image TEXT,
            similar_image TEXT, source TEXT NOT NULL DEFAULT 'manual');
          INSERT INTO homes (name) VALUES ('Old'); INSERT INTO rooms (home_id, name) VALUES (1, 'R');
          INSERT INTO items (room_id, name) VALUES (1, 'Old thing');""")
        c.commit(); c.close()
        old = server.DATA_DIR
        try:
            server.DATA_DIR = d
            server.init_storage()
            conn = server.connect()
            row = conn.execute("SELECT name, price_updated_at FROM items").fetchone()
            conn.close()
            self.assertEqual((row["name"], row["price_updated_at"]), ("Old thing", None))
        finally:
            server.DATA_DIR = old


if __name__ == "__main__":
    unittest.main(verbosity=1)
