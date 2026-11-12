"""Agent Checkout: a plain-English purchase request becomes a capped PayPal sandbox order.

Flow: the user types a request -> Gemini extracts item, amount and currency as JSON ->
a server-side policy decides ALLOW / REVIEW / BLOCK -> on ALLOW (or after a human approves
a REVIEW) the server creates a PayPal Orders v2 order in the sandbox -> the buyer approves
on PayPal -> the server captures it and shows the capture id.

Standard library only. Run: python app.py, then open http://localhost:5000
"""
import base64
import html
import json
import os
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PAYPAL = "https://api-m.sandbox.paypal.com"
PORT = int(os.environ.get("PORT", "5000"))
BASE_URL = f"http://localhost:{PORT}"

# Policy. Enforced on the server before any PayPal call.
BUDGET_CAP = float(os.environ.get("BUDGET_CAP", "30"))
REVIEW_OVER = float(os.environ.get("REVIEW_OVER", "20"))
KNOWN_SELLERS = {"cloud credits", "domain name", "api credits", "ebook", "coffee"}

LOG = []          # audit trail, newest first
PENDING = {}      # review_id -> parsed request awaiting a human decision


def load_creds():
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_SECRET")
    path = os.environ.get("PAYPAL_CREDS_FILE")
    if (not cid or not secret) and path and os.path.exists(path):
        lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
        cid, secret = lines[0], lines[1]
    return cid, secret


def http_json(url, data=None, headers=None, method=None):
    body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def paypal_token():
    cid, secret = load_creds()
    if not cid:
        raise RuntimeError("PayPal sandbox credentials missing (PAYPAL_CLIENT_ID/PAYPAL_SECRET or PAYPAL_CREDS_FILE)")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = http_json(f"{PAYPAL}/v1/oauth2/token", b"grant_type=client_credentials",
                  {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    return r["access_token"]


def create_order(item, amount, currency):
    tok = paypal_token()
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{"description": item[:120],
                            "amount": {"currency_code": currency, "value": f"{amount:.2f}"}}],
        "application_context": {"return_url": f"{BASE_URL}/return", "cancel_url": f"{BASE_URL}/",
                                "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"},
    }
    r = http_json(f"{PAYPAL}/v2/checkout/orders", body,
                  {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    approve = next(l["href"] for l in r["links"] if l["rel"] in ("approve", "payer-action"))
    return r["id"], approve


def capture_order(order_id):
    tok = paypal_token()
    r = http_json(f"{PAYPAL}/v2/checkout/orders/{order_id}/capture", {},
                  {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    cap = r["purchase_units"][0]["payments"]["captures"][0]
    return r["status"], cap["id"], cap["amount"]["value"], cap["amount"]["currency_code"]


def ai_parse(text):
    """Gemini turns free text into {item, amount, currency, max_amount}. Falls back to a regex if no key."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        prompt = ("Extract the purchase request as JSON with keys item (string), amount (number), "
                  "currency (ISO code, default USD), max_amount (number or null, the user's stated limit). "
                  "Return only JSON.\nRequest: " + text)
        url = ("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key="
               + urllib.parse.quote(key))
        try:
            r = http_json(url, {"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"responseMimeType": "application/json"}},
                          {"Content-Type": "application/json"})
            out = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
            if isinstance(out, list):
                out = out[0]
            out["parser"] = "gemini-2.5-flash"
            return out
        except Exception as e:  # keep the demo alive, but say so
            fallback = regex_parse(text)
            fallback["parser"] = f"regex (Gemini failed: {type(e).__name__})"
            return fallback
    out = regex_parse(text)
    out["parser"] = "regex (no GEMINI_API_KEY)"
    return out


def regex_parse(text):
    nums = [float(n) for n in re.findall(r"\$?(\d+(?:\.\d+)?)", text)]
    m = re.search(r"of (.+?)(?:,|$| max| for)", text, re.I)
    return {"item": (m.group(1) if m else text)[:80], "amount": nums[0] if nums else 0,
            "currency": "USD", "max_amount": nums[1] if len(nums) > 1 else None}


def decide(req):
    """Server-side policy. Returns (decision, reason)."""
    amt = float(req.get("amount") or 0)
    cap = req.get("max_amount")
    item = str(req.get("item", "")).lower()
    if amt <= 0:
        return "BLOCK", "no valid amount"
    if amt > BUDGET_CAP:
        return "BLOCK", f"${amt:.2f} is over the ${BUDGET_CAP:.2f} budget cap"
    if cap is not None and amt > float(cap):
        return "BLOCK", f"${amt:.2f} is over the user's own limit of ${float(cap):.2f}"
    if not any(s in item for s in KNOWN_SELLERS):
        return "REVIEW", f"'{req.get('item')}' is not on the approved list, a human must approve"
    if amt > REVIEW_OVER:
        return "REVIEW", f"${amt:.2f} is over the ${REVIEW_OVER:.2f} auto-approve line"
    return "ALLOW", "within budget, approved item"


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Agent Checkout</title>
<style>body{{font-family:system-ui,sans-serif;max-width:820px;margin:32px auto;padding:0 16px;background:#f7f7f5;color:#1b1d24}}
input{{width:75%;padding:10px;font-size:16px}}button{{padding:10px 16px;font-size:16px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px;margin:12px 0}}
.ALLOW{{color:#167a3e}}.REVIEW{{color:#a86b00}}.BLOCK{{color:#b3261e}}code{{background:#eee;padding:1px 4px}}</style></head>
<body><h1>Agent Checkout</h1><p>Type what the agent should buy. AI reads it, the server checks the policy
(cap ${cap}, human review over ${rev} or for unknown items), then PayPal sandbox handles payment.</p>
<form method=post action=/request><input name=text placeholder="Buy $15 of cloud credits, max $20" autofocus>
<button>Send</button></form>{body}<h2>Audit log</h2>{log}</body></html>"""


def render(body=""):
    log = "".join(f"<div class=card>{l}</div>" for l in LOG) or "<p>Nothing yet.</p>"
    return PAGE.format(cap=f"{BUDGET_CAP:.0f}", rev=f"{REVIEW_OVER:.0f}", body=body, log=log).encode()


class H(BaseHTTPRequestHandler):
    def send(self, content, code=200, location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content)

    def form(self):
        n = int(self.headers.get("Content-Length", 0))
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    def pay(self, req):
        oid, approve = create_order(req["item"], float(req["amount"]), req.get("currency") or "USD")
        LOG.insert(0, f"PayPal order <code>{oid}</code> created for {html.escape(str(req['item']))}, "
                      f"${float(req['amount']):.2f}. Waiting for buyer approval.")
        self.send(b"", 303, approve)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/return":
            oid = urllib.parse.parse_qs(u.query).get("token", [""])[0]
            try:
                status, cid, val, cur = capture_order(oid)
                LOG.insert(0, f"<b class=ALLOW>CAPTURED</b> order <code>{oid}</code>, capture id <code>{cid}</code>, "
                              f"{val} {cur}, status {status}")
            except Exception as e:
                LOG.insert(0, f"<b class=BLOCK>Capture failed</b> for {oid}: {html.escape(str(e))}")
            return self.send(b"", 303, "/")
        self.send(render())

    def do_POST(self):
        f = self.form()
        try:
            if self.path == "/request":
                text = f.get("text", "")
                req = ai_parse(text)
                decision, reason = decide(req)
                LOG.insert(0, f"Request: <i>{html.escape(text)}</i><br>Parsed by {html.escape(req['parser'])}: "
                              f"<code>{html.escape(json.dumps({k: req.get(k) for k in ('item','amount','currency','max_amount')}))}</code>"
                              f"<br><b class={decision}>{decision}</b>: {html.escape(reason)}")
                if decision == "ALLOW":
                    return self.pay(req)
                if decision == "REVIEW":
                    rid = str(len(PENDING) + 1)
                    PENDING[rid] = req
                    body = (f"<div class=card><b class=REVIEW>Needs your approval:</b> {html.escape(reason)}"
                            f"<form method=post action=/review><input type=hidden name=id value={rid}>"
                            f"<button name=ok value=1>Approve and pay</button> <button name=ok value=0>Reject</button></form></div>")
                    return self.send(render(body))
                return self.send(b"", 303, "/")
            if self.path == "/review":
                req = PENDING.pop(f.get("id", ""), None)
                if req and f.get("ok") == "1":
                    # Re-check the hard limits server-side: review can't override a BLOCK.
                    if decide(req)[0] == "BLOCK":
                        LOG.insert(0, "<b class=BLOCK>BLOCK</b>: approval refused, request breaks a hard limit")
                        return self.send(b"", 303, "/")
                    LOG.insert(0, f"Human approved {html.escape(str(req['item']))}")
                    return self.pay(req)
                LOG.insert(0, "Human rejected the request. No payment created.")
                return self.send(b"", 303, "/")
        except Exception as e:
            LOG.insert(0, f"<b class=BLOCK>Error</b>: {html.escape(str(e))}")
            return self.send(b"", 303, "/")
        self.send(b"not found", 404)


if __name__ == "__main__":
    print(f"Agent Checkout on {BASE_URL}  cap=${BUDGET_CAP} review_over=${REVIEW_OVER}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
