# BANKNIFTY box-breakout alerter

Implements the flow in `zerodha.jpeg`. **Alert-only** — the script never places,
modifies or cancels an order. It reads 5-minute candles from Kite's historical
endpoint and emails you at each decision point; you place every trade yourself.

## Files

| File | Purpose |
|---|---|
| `webui.py` | Control panel: arm the day, one-click Kite login, stop button, 09:00 scheduler |
| `scheduler.py` | The arming / token / launch rules, kept testable without a browser |
| `login.py` | Terminal fallback for minting the token, if you would rather not use the UI |
| `strategy.py` | The alerter. Polls historical candles, runs the state machine, emails |
| `config.json` | Your box, side and date for today (copy from `config.example.json`) |
| `simtest.py` | Offline replay of 16 scenarios against the state machine, no network |
| `aggtest.py` | Offline checks on candle sourcing and VWAP accumulation, no network |
| `uitest.py` | Offline checks on arming, token and launch rules, no network |
| `state.json` | What is armed, stopped, and running. Written by the UI |
| `DECISIONS.md` | Every spec question asked, the answer given, and what it became in code |
| `RUNBOOK.md` | Step-by-step for running it against the live market, plus troubleshooting |

## Setup

First time out, follow [`RUNBOOK.md`](RUNBOOK.md) — it covers the Kite
subscription, the app password, how to verify the data path in the first ten
minutes, and how to force a test signal without waiting days for a real setup.
The short version:

```bash
pip install -r requirements.txt

export KITE_API_KEY=your_api_key
export KITE_API_SECRET=your_api_secret
export ZERODHA_SMTP_PASSWORD=your_google_app_password

cp config.example.json config.json
```

The SMTP password must be a **Google app password**, not your account password —
Workspace rejects plain passwords over SMTP. Keep it in the environment, never
in `config.json`.

## Daily routine

Start the control panel once and leave it running:

```bash
./venv/bin/python webui.py        # http://127.0.0.1:5000
```

Then:

| When | Do |
|---|---|
| Evening, before 22:00 | Fill the form — date, side, box_low, box_high, violent_range — and **Submit & arm**. This writes `config.json` and arms that date. |
| Morning, after 07:35 | Click **Log in to Kite**. Kite flushes every access token between 07:30 and 08:30, so last night's login is always dead by morning — this step cannot be done in advance. |
| 09:00 | The scheduler launches `strategy.py` by itself, but only if the day is armed *and* a token valid for today exists. |
| Any time | **Stop** disarms the day, kills a running strategy, and emails a record. |

If you have not logged in by 09:00 the scheduler keeps waiting and starts the
moment you do — the strategy backfills from 09:15, so a late start still
reconstructs the whole session. It gives up at `wait_for_token_until` (14:45).

**One-off setup:** in the Kite developer console set the app's Redirect URL to
exactly `http://127.0.0.1:5000/callback`, so the panel can catch the
`request_token` itself. If you would rather not change it, the panel has a
"paste a request_token manually" box that works with the current
`https://localhost` setting.

Timings live in `config.json` under `ui`:

```json
"ui": {
  "port": 5000,
  "arm_cutoff": "22:00",
  "launch_at": "09:00",
  "wait_for_token_until": "14:45"
}
```

Running `strategy.py` by hand still works and ignores all of the above.

`strategy.py` refuses to run if `config.json` is dated anything but today, so a
stale box can't quietly trade yesterday's levels. `--ignore-date` overrides.

Other flags: `--no-email` (console only), `--test-email` (verify SMTP and exit),
`--replay YYYY-MM-DD` (run a past session from history and exit), `--no-backfill`,
`--config PATH`.

## The rules as implemented

| Step | Rule |
|---|---|
| Violence filter | The **09:15–09:20** candle only. Range > 180 pts → no trade today |
| No-trade zone | The box widened by the buffer on **both** edges: `[box_low − 10, box_high + 10]`. A candle counts as inside if **any part of its range overlaps** — a wick is enough |
| Breakout | A candle **entirely clear** of the zone: `low > box_high + 10` (up) or `high < box_low − 10` (down). **Direction is not tested** — a BUY can form entirely below the box, because entry conditions are VWAP-relative, not box-relative |
| Re-entry | Once any candle has been fully clear, a later candle touching the zone → **day is dead** |
| C1 | First candle entirely clear of the zone (either side) closing on the right side of VWAP — BUY: `close > VWAP`, SELL: `close < VWAP`. Clear candles on the wrong side of VWAP are skipped, not fatal |
| C2 | First later candle that is **also entirely clear** and closes beyond `C1.high` (BUY) / `C1.low` (SELL) |
| Box touch in a trade | **Closes the position immediately**, whatever the target and stop are doing |
| C1 invalidation | While hunting C2, any candle closing back through VWAP kills C1. The day continues — a fresh C1 is hunted |
| Entry | C2's close |
| Target | BUY: `entry + (High(C2) − Low(C1))`. SELL: `entry − (High(C1) − Low(C2))`. Hit is detected on **live LTP touch** |
| Stop | L1 = a candle closing back through VWAP. L2 = a later candle closing past L1's low (BUY) / high (SELL) → exit. A close back on the right side of VWAP **disarms** L1 |
| Cutoff | No new entries after **14:45** |
| Square-off | If still in a trade at **15:10**, an exit email fires regardless |
| Trades per day | One. After any exit the script goes idle |

Emails sent: `ENTER`, `STOP WARNING - L1 armed`, `STOP WARNING CLEARED`,
`STOP - exit now`, `CLOSE - price back in the box`, `TARGET HIT`, `SQUARE OFF`,
`FEED STALLED` / `FEED DEAD`, and the various `NO TRADE TODAY` terminal states.

## Where the data comes from — read this

**Candles come from Kite's historical endpoint, not from the tick stream.** The
websocket sends throttled snapshots (roughly one per second), not every trade,
so candles built from ticks miss the opening print and any extreme that falls
between snapshots. Measured on real days: the 09:15 open was out by **32.45
points** on 2026-09-03, and the high by **48.45 points** on 2026-09-02. The
180-point filter kills an entire session off that number, so approximate candles
were not acceptable there.

Two instruments are fetched and merged, because the index has no volume of its
own:

- **NSE:NIFTY BANK** — OHLC, the box test, the 180 filter
- **Nearest-expiry BANKNIFTY future** — volume only, auto-resolved from the NFO
  instrument dump at startup and rolled automatically each month

VWAP is `Σ((H+L+C)/3 × V) / ΣV` over index prices and futures volume, reset each
session. Ticks are still subscribed, but only to drive the live LTP target check.

Consequences:

1. **Signals arrive ~8s after a candle closes.** Kite published a just-closed
   candle within **2 seconds** on both legs when measured on 2026-09-03, so
   `POLL_DELAY` is 8s with `POLL_RETRY` 5s. A miss just retries, so there is no
   reason to pad it — every second here is entry drift. Measured drift in the 60s
   after a close: median 7.3 pts, 90th percentile 22.6 pts, against a median
   target of 40.2 pts. It is **not** directionally biased (mean +0.15 pts on 649
   BUY confirmations, worse entry 48% of the time), so it adds variance, not a
   systematic cost.
   **The target in the alert is computed from C2's close, not your fill** — if you
   fill 10 pts higher on a BUY, your real profit at that target is 10 pts less.
2. **Start time no longer matters.** On startup the whole session is backfilled
   from 09:15, so VWAP is correctly anchored whether you launch at 09:00 or
   14:00. Earlier is still better — a late start means you miss alerts that
   already fired, not that the numbers are wrong.
3. **Live and `--replay` read identical data.** What you test on a past day is
   exactly what runs live.
4. **Scale mismatch.** VWAP sits on the index price scale with futures volume as
   the weight. That weight reflects futures activity, not spot — near expiry,
   when futures volume gets erratic, the VWAP shape shifts slightly.
5. **VWAP includes the candle being evaluated.** A candle closes, VWAP is updated
   with it *first*, then its close is tested against the new VWAP. Standard, but
   it is a choice — say the word to test against the prior candle's VWAP.

## Two things I chose, flag them if wrong

- **`first_bar_eligible_as_c1` (default `true`).** You said the 180-pt violence
  test applies to the 09:15 candle only, which departs from the chart's
  "breakout candle" wording. That left it open whether the 09:15 candle can
  itself be C1. It can, currently — if it survives the 180 test and closes
  outside the box on the right side of VWAP. Set the flag to `false` to make C1
  hunting start at 09:20.
- **VWAP with no volume yet.** If a breakout candle arrives before any futures
  volume has accumulated, VWAP is undefined and the candle is skipped with a
  warning rather than treated as a pass or a failure.

## Testing without a live market

```bash
python simtest.py     # strategy state machine
python aggtest.py     # candle sourcing and VWAP
python uitest.py      # arming, token and launch rules
```

`simtest.py` stubs out `kiteconnect` and replays 16 synthetic days: violent
open, BUY happy path, a wick into the zone rejected as C1, a BUY forming entirely
below the box, re-entry killing the day from both hunt states, C1 killed by a
VWAP cross then re-hunted, the full L1-arm/disarm/re-arm/stop sequence, a box
touch closing an open position, square-off, cutoff, gap-open, and the SELL
mirror.

`aggtest.py` covers candle sourcing with a stubbed Kite: the index/futures
merge, exclusion of half-formed candles, pre-open filtering, VWAP accumulation,
and a regression pinning the tick-vs-historical OHLC gap that made this change
necessary.

To check the strategy against a real past session:

```bash
python strategy.py --replay 2026-09-01
```

## Operational notes

- Timezone is hard-coded to `Asia/Kolkata` throughout; the machine's own clock
  can be anything.
- Pre-open candles (09:00–09:15) are discarded.
- The script exits as soon as the day reaches a terminal state — there is no
  second trade.
- **Nothing exits silently.** Two independent failure paths are covered:
  - *Websocket dead* — warning email at 90s without a tick, forced reconnect at
    150s, email and exit at 420s. Candles are unaffected (they come from
    history), so this only costs the live target check.
  - *Historical fetch failing* — retries with backoff (5s → 60s), warning email
    after 60s of failure, gives up and emails after 300s (~9 attempts). A single
    dropped request no longer ends the day; on 2026-09-04 one read timeout did
    exactly that, four hours in, with no notification.
  - Any unhandled exception, and Ctrl-C while a position is open, also send mail
    before the process dies.
  - A heartbeat line logs every 5 minutes.
- Kite's HTTP timeout is raised from its 7s default to 20s.
- `.kite_token.json` holds a live trading token. It is written 0600. Don't
  commit it, don't sync it.
