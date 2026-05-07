# Setup — Kite Connect data layer

One-time:

1. **Subscribe to Kite Connect** — go to https://kite.trade, sign in with your Zerodha account, and pay the ₹500/month fee for the Connect tier.

2. **Create an app** at https://developers.kite.trade/apps:
   - **App name:** anything (e.g. "Options Checklist")
   - **Redirect URL:** `http://127.0.0.1:5010` (must match exactly — no trailing slash)
   - **Postback URL:** leave blank
   - On submit you get an **API key** and **API secret**.

3. **Save credentials** in this folder. Create a file called `.env`:
   ```
   KITE_API_KEY=your_api_key_here
   KITE_API_SECRET=your_api_secret_here
   ```
   `.env` is gitignored — never commits.

4. **Install Python deps** (already done if you ran `pip3 install kiteconnect pandas`).

## Daily

Just run:

```
python3 compute.py
```

The first time each day, your browser will open the Kite login page. Login with your Zerodha credentials + TOTP, click Authorize. The script captures the redirect, stores the access_token in `.kite_session.json`, and continues fetching data.

Subsequent runs the same day reuse the cached token (no re-login).

Output: `./data.json` with current spot, indicators, and signals.

## Lunch / verification run

```
python3 kite_auth.py
```

Just authenticates and prints "OK — logged in as <user>". Useful to do the morning login proactively before 3:15 PM.
