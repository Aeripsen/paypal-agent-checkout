# Agent Checkout

An AI agent should be able to buy things for you, but only inside limits you set. Agent Checkout takes a plain-English
purchase request, uses Gemini to turn it into a structured order, checks it against a spending policy on the server,
and only then creates a PayPal order in the sandbox. The buyer approves on PayPal and the server captures the payment.

## What it does

1. You type a request, for example "Buy $15 of cloud credits, max $20".
2. Gemini extracts item, amount, currency and your stated limit as JSON.
3. The server applies the policy before any PayPal call:
   - **BLOCK** if the amount is over the budget cap ($30 by default) or over the limit you wrote in the request.
     No PayPal order is ever created.
   - **REVIEW** if the item is not on the approved list or the amount is over the auto-approve line ($20).
     A human must click Approve. Approval cannot override a BLOCK; the hard limits are checked again.
   - **ALLOW** otherwise.
4. Allowed requests become a PayPal Orders v2 order (intent CAPTURE). You approve it on the PayPal sandbox page.
5. On return the server captures the order and logs the capture id. Every step lands in the audit log on the page.

## PayPal technology used

- OAuth2 client credentials (`/v1/oauth2/token`)
- Orders v2: create (`/v2/checkout/orders`) and capture (`/v2/checkout/orders/{id}/capture`)
- Sandbox buyer approval flow

## AI used

- Google Gemini (`gemini-flash-latest`, falling back to other Flash models if one is overloaded) through the Generative Language REST API, in JSON mode, to parse the request.
  If no `GEMINI_API_KEY` is set, a regex fallback runs and the log says so.
- Claude Code (Anthropic) helped write the code.

## Run it

Python 3.9+ only, no packages to install.

```
set PAYPAL_CLIENT_ID=your-sandbox-client-id
set PAYPAL_SECRET=your-sandbox-secret
set GEMINI_API_KEY=your-gemini-key
python app.py
```

Open http://localhost:5000. Get sandbox credentials at https://developer.paypal.com (Apps & Credentials, Sandbox).
Approve payments with a sandbox personal account from Testing Tools > Sandbox Accounts.
Alternatively put the client id and secret on two lines of a file and set `PAYPAL_CREDS_FILE` to its path.

Optional: `BUDGET_CAP` (default 30), `REVIEW_OVER` (default 20), `PORT` (default 5000).

## Try these

- "Buy $15 of cloud credits, max $20" gives ALLOW, then PayPal approval and capture.
- "Buy $250 of cloud credits, max $30" gives BLOCK. No PayPal order is created.
- "Buy $12 of concert tickets" gives REVIEW. Approve it and it goes to PayPal; reject it and nothing is created.

## Limits

Sandbox only. The policy and audit log live in memory and reset on restart. Built during the PayPal AI Hackathon.

## License

MIT, see LICENSE.
