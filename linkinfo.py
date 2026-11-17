"""Read a product page and pull out its price. Standard library only.

    from linkinfo import link_info
    link_info("https://shop.example/item")  # -> {"url", "host", "title", "price", "currency", "source"}

The server fetches a URL it was handed, so this module is deliberately defensive:
 - only http/https, and no redirects to anything else (no ftp:, file:)
 - every address the host resolves to must be a public one (no localhost, home network or
   cloud-metadata addresses), checked again for each redirect, and the connection goes to
   the address that was checked (no DNS-rebinding window)
 - at most 3 redirects, 10 s timeout, 1.5 MB read, HTML only
Prices come from structured data the site publishes (JSON-LD, Open Graph / product meta tags,
itemprop="price"). It never guesses from page text, so it may find nothing.
"""
import html.parser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 1_500_000
TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; HomeInventory/1.0; personal price lookup)"


class LinkError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message, self.status = message, status


def _check_public(host, allow_private):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise LinkError("That address could not be found. Check the link.", 400)
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not allow_private and not ip.is_global:
            raise LinkError("That link points to a private or local address, so it was not opened.", 400)
        ips.append(ip)
    if not ips:
        raise LinkError("That address could not be found. Check the link.", 400)
    return str(sorted(ips, key=lambda i: i.version)[0])


def _make_opener(allow_private):
    ctx = ssl.create_default_context()

    class HTTPConn(http.client.HTTPConnection):
        def connect(self):
            ip = _check_public(self.host, allow_private)
            self.sock = socket.create_connection((ip, self.port), self.timeout)

    class HTTPSConn(http.client.HTTPSConnection):
        def connect(self):
            ip = _check_public(self.host, allow_private)
            sock = socket.create_connection((ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class HTTP(urllib.request.AbstractHTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConn, req)
        http_request = urllib.request.AbstractHTTPHandler.do_request_

    class HTTPS(urllib.request.AbstractHTTPHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConn, req, context=ctx)
        https_request = urllib.request.AbstractHTTPHandler.do_request_

    class Redirect(urllib.request.HTTPRedirectHandler):
        max_redirections = 3

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not re.match(r"^https?://", newurl, re.I):
                raise LinkError("The link redirects somewhere that is not a web page.", 400)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    op = urllib.request.OpenerDirector()   # only these handlers: no ftp/file/proxy
    for h in (urllib.request.UnknownHandler(), urllib.request.HTTPDefaultErrorHandler(), Redirect(),
              urllib.request.HTTPErrorProcessor(), HTTP(), HTTPS()):
        op.add_handler(h)
    return op


def fetch_page(url, allow_private=False):
    if not re.match(r"^https?://[^\s/]+", url or "", re.I):
        raise LinkError("Paste a full link starting with http:// or https://", 400)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity"})
    try:
        with _make_opener(allow_private).open(req, timeout=TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ("text/html", "application/xhtml+xml", ""):
                raise LinkError("That link is not a web page.", 400)
            body = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
            final = r.geturl()
    except LinkError:
        raise
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 503):
            raise LinkError("That site did not allow the page to be read automatically. "
                            "Enter the price by hand.")
        if e.code in (404, 410):
            raise LinkError("That page was not found. Check the link.", 400)
        raise LinkError("The site returned an error (HTTP %s)." % e.code)
    except (socket.timeout, TimeoutError):
        raise LinkError("The site took too long to answer.")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, "reason", None), LinkError):
            raise e.reason
        raise LinkError("Could not open that link.")
    try:
        return final, body.decode(charset, "replace")
    except LookupError:
        return final, body.decode("utf-8", "replace")


# ---------- price extraction ----------

def parse_price(v):
    """12.5, '$1,299.00', '12,99' -> float, or None (zero and negatives are rejected)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = re.sub(r"[^\d.,]", "", str(v))
        if not re.search(r"\d", s):
            return None
        if "," in s and "." in s:
            s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
        try:
            f = float(s)
        except ValueError:
            return None
    return round(f, 2) if 0 < f < 10_000_000 else None


class _Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.meta, self.itemprop, self.title = [], {}, [], ""
        self._script = self._title = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._script, self._buf = True, []
        elif tag == "title":
            self._title, self._buf = True, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        if a.get("itemprop", "").lower() == "price" and a.get("content"):
            self.itemprop.append(a["content"])
        if a.get("itemprop", "").lower() == "pricecurrency" and a.get("content"):
            self.meta.setdefault("pricecurrency", a["content"])

    def handle_data(self, data):
        if self._script or self._title:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script:
            self.ld.append("".join(self._buf))
            self._script = False
        elif tag == "title" and self._title:
            self.title = " ".join("".join(self._buf).split())
            self._title = False


def _walk(node, out):
    if isinstance(node, list):
        for n in node:
            _walk(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types or "ProductGroup" in types:
            out.append(node)
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk(v, out)


def _offer_price(offers):
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        spec = o.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        for cand, cur in ((o.get("price"), o.get("priceCurrency")), (o.get("lowPrice"), o.get("priceCurrency")),
                          ((spec or {}).get("price") if isinstance(spec, dict) else None,
                           (spec or {}).get("priceCurrency") if isinstance(spec, dict) else None)):
            p = parse_price(cand)
            if p:
                return p, (cur or "").upper()
        nested = o.get("offers")
        if nested:
            r = _offer_price(nested)
            if r:
                return r
    return None


def extract_price(doc):
    """Returns {"price": float|None, "currency": str, "title": str, "source": str}."""
    page = _Page()
    try:
        page.feed(doc)
    except Exception:  # malformed HTML: use what was gathered
        pass
    title = page.meta.get("og:title") or page.title
    for blob in page.ld:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        found = []
        _walk(data, found)
        for prod in found:
            r = _offer_price(prod.get("offers"))
            if r:
                return {"price": r[0], "currency": r[1], "title": str(prod.get("name") or title)[:200],
                        "source": "json-ld"}
    cur = (page.meta.get("product:price:currency") or page.meta.get("og:price:currency")
           or page.meta.get("pricecurrency") or "").upper()
    for key in ("product:price:amount", "og:price:amount", "og:price", "twitter:data1"):
        p = parse_price(page.meta.get(key))
        if p:
            if key == "twitter:data1" and not re.search(r"[$€£]|\d[.,]\d\d", page.meta.get(key, "")):
                continue
            return {"price": p, "currency": cur, "title": title[:200], "source": "meta"}
    for content in page.itemprop:
        p = parse_price(content)
        if p:
            return {"price": p, "currency": cur, "title": title[:200], "source": "itemprop"}
    return {"price": None, "currency": cur, "title": title[:200], "source": ""}


def link_info(url, allow_private=False):
    final, doc = fetch_page(url, allow_private)
    out = extract_price(doc)
    out.update(url=final, host=re.sub(r"^www\.", "", urllib.parse.urlparse(final).hostname or ""))
    return out
