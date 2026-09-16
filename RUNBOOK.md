# Runbook — testing against the live market

How to get from a fresh checkout to a running alerter on a real trading day.

Nothing here places an order. The worst case is a wrong email.

- [A. One-time setup](#a-one-time-setup)
- [B. Every trading morning](#b-every-trading-morning)
- [C. First ten minutes — is the data path healthy?](#c-first-ten-minutes--is-the-data-path-healthy)
- [D. Forcing a signal on day one](#d-forcing-a-signal-on-day-one)
- [E. Troubleshooting](#e-troubleshooting)
- [F. Gotchas](#f-gotchas)

---

## A. One-time setup

### 1. Kite Connect subscription

₹500/month per app at [developers.kite.trade](https://developers.kite.trade).
Create an app and note the `api_key` and `api_secret`.

Billing is a **flat monthly subscription, not metered per API call.** Credits are
deducted only when you create an app and when its subscription renews each
month, so running the script harder never costs more. Top up credits in the
billing section of the developer console; link a Zerodha client ID there for
automatic monthly renewal, or leave it unlinked and top up by hand.

**Set a redirect URL on the app.** Any URL works — `http://127.0.0.1` is fine,
nothing needs to be listening on it. Login fails outright if the app has no
redirect URL registered.

The ₹500 tier bundles realtime WebSocket streaming **and** historical candle
data — there is no separate add-on to buy any more. This script does not
currently use the historical endpoint; it builds its own candles from the tick
stream, which is why it must be started before 09:15. See the note in
`DECISIONS.md` about reworking that now the data is available.

### 2. Install dependencies

```bash
cd /home/sahil/scripts/zerodha
pip3 install -r requirements.txt
```

### 3. Gmail app password

[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
Requires 2FA on the account. If that page 404s, your Workspace admin has
disabled app passwords — you'll need them to allow it, or switch the config to a
different SMTP provider.

A normal account password will **not** work over SMTP.

### 4. Environment variables

Put these in your shell profile so they survive a new terminal:

```bash
export KITE_API_KEY=xxxxx
export KITE_API_SECRET=xxxxx
export ZERODHA_SMTP_PASSWORD=xxxx_xxxx_xxxx_xxxx
```

### 5. Verify email before you need it

Needs neither a live market nor a Kite token:

```bash
cp config.example.json config.json
python3 strategy.py --test-email
```

A message titled `[BNF] test` should arrive. Sort this out well before a
trading morning — SMTP problems are the most common first-run failure.

### 6. Log directory

```bash
mkdir -p logs
```

---

## B. Every trading morning

### ~09:00 — mint the access token

```bash
python3 login.py
```

It prints a login URL. Log in with your Zerodha credentials and 2FA. The browser
lands on your redirect URL with `?request_token=abc123...` in the address bar —
copy that value and paste it back at the prompt.

Success looks like:

```
Logged in as Your Name (AB1234).
Token cached at /home/sahil/scripts/zerodha/.kite_token.json for 2026-08-31.
```

Kite tokens expire around 07:30 each morning, so this is a daily step.

### ~09:05 — set today's box

Edit `config.json`:

```json
{
  "date": "2026-08-31",
  "side": "BUY",
  "box_low": 54000,
  "box_high": 54300
}
```

The `date` must be today. The script refuses to start otherwise, so yesterday's
box can never quietly trade today. `--ignore-date` overrides when you mean it.

### Before 09:15 — launch

```bash
python3 strategy.py 2>&1 | tee logs/2026-08-31.log
```

**Start before 09:15.** VWAP is anchored to the session open and built tick by
tick — there is no historical backfill. Launch at 09:20 and your VWAP is
anchored to 09:20, which is a different line from the one on your chart. This is
the one hard timing rule in the whole system.

Console-only, no email:

```bash
python3 strategy.py --no-email
```

---

## C. First ten minutes — is the data path healthy?

A healthy startup log:

```
09:0x INFO  index NSE:NIFTY BANK token=260105 | volume from BANKNIFTY26SEPFUT (expiry 2026-09-24, token 1234567)
09:0x INFO  armed: BUY box 54000-54300 buffer 10 violent>180 cutoff 14:45 squareoff 15:10
09:0x INFO  websocket connected, subscribed to 2 instruments
09:20 INFO  bar 09:15 O=54120.30 H=54198.75 L=54111.35 C=54163.80 V=41250 range=87.4 vwap=54163.22 state=FIRST_BAR
09:20 INFO  opening candle range 87.4 <= 180, day is live
```

Check three things on that first bar and you have validated the entire data
path:

| Check | Why it matters |
|---|---|
| **`V=` is non-zero** | Volume comes from the future. Zero means futures ticks aren't arriving, VWAP will never compute, and nothing will ever trigger |
| **`vwap=` is a number, not `n/a`** | Same root cause. `n/a` means `cum_vol` is still zero |
| **OHLC matches your chart** | Confirms the index feed and the 5-min bucketing agree with reality |

If all three look right, the plumbing works and everything after it is just the
state machine — which `simtest.py` already covers offline.

---

## D. Forcing a signal on day one

Waiting for a genuine setup can take days. To exercise the full path in about
fifteen minutes, set a **deliberately tiny box around the live price**:

```json
{
  "date": "2026-08-31",
  "side": "BUY",
  "box_low": 54150,
  "box_high": 54160
}
```

Price breaks out almost immediately. If it also closes above VWAP you get a real
`ENTER BUY` email carrying entry, target, C1 and C2 — the whole chain, end to
end, with nothing at risk because the script places no orders.

**Ignore that signal. Do not trade it.** It is a plumbing test, not a setup.

Run it once to prove the pipe, then switch back to your real box.

Tip: a tiny box on a `BUY` day is also the fastest way to see the *day is dead*
path — if price happens to break downward first, you'll get the wrong-side
breakout email instead.

---

## E. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No token at .kite_token.json` | Never logged in | `python3 login.py` |
| `Cached token is from 2026-08-28, not 2026-08-31` | Token expired overnight | `python3 login.py` again |
| `config.json is dated ... but today is ...` | Stale box | Update `date`, or `--ignore-date` |
| `SMTP password not found` | Env var missing in this shell | `export ZERODHA_SMTP_PASSWORD=...` |
| `email failed: ... Username and Password not accepted` | Using account password, not an app password | Generate an app password |
| `Kite did not recognise NSE:NIFTY BANK` | Symbol typo in config | Restore `"index_symbol": "NSE:NIFTY BANK"` |
| `No live BANKNIFTY future found` | Instrument dump fetch failed or name wrong | Check connectivity; `"fut_name": "BANKNIFTY"` |
| Bars print but `V=0` and `vwap=n/a` | Futures ticks not arriving | Check the subscribe line named a FUT token; check the websocket didn't drop |
| No bars at all | Outside market hours, or websocket never connected | Look for the `websocket connected` line |
| Token exchange fails in `login.py` | Redirect URL not registered, or `request_token` already used | Register a redirect URL; each token is single-use, so log in again |

---

## F. Gotchas

- **Three websocket connections per `api_key`.** Kill old runs before starting a
  new one, or you'll hit the cap.
- **Startup pulls the full NFO instrument dump** (~2 MB) to resolve the futures
  contract. A few seconds of silence at launch is normal.
- **Outside 09:15–15:30 the script sits idle** with no ticks. Nothing is wrong.
  Pre-open ticks (09:00–09:15) are deliberately discarded.
- **The script assumes you took the trade** after `ENTER`. It places no orders
  and cannot tell — it will keep emailing stop, target and square-off regardless
  of what you actually did.
- **One trade per day.** The script exits at the first terminal state: too
  violent, wrong-side breakout, cutoff with no setup, target, stop, or
  square-off.
- **No crash recovery.** Nothing is persisted. Restarting mid-session re-anchors
  VWAP to the restart time and forgets any open position. If it dies after an
  `ENTER`, manage that trade yourself and don't trust a restarted instance.
- **Websocket gaps are silent.** `kiteconnect` reconnects on its own, but ticks
  during the gap are lost and the affected candle is incomplete. Nothing detects
  this — watch the log for `websocket closed` lines.
- **`.kite_token.json` is a live trading credential.** Written 0600. Don't
  commit it, don't sync it, don't paste it anywhere.

---

## Flags

| Flag | Effect |
|---|---|
| `--config PATH` | Use a different config file (default `config.json`) |
| `--no-email` | Log alerts to the console only |
| `--test-email` | Send one test email and exit |
| `--ignore-date` | Run even though the config date isn't today |
