# Decision log

Every question asked while turning `zerodha.jpeg` into `strategy.py`, the answer
given, and what it became in code. Written so that six months from now the
"why is it like this?" question has an answer.

The flowchart specified the *shape* of the strategy but left roughly thirty
things undefined — instrument, timeframe, what "the box" is, how VWAP gets its
volume, what counts as a breakout, what resets the stop. This file records how
each gap was closed.

---

## 1. Data and access

**Q: How does the script talk to Zerodha?**
**A: Kite Connect, no Historical Data subscription.**

Means the script cannot ask Kite for candles. It subscribes to the tick
websocket and aggregates its own 5-minute bars in memory.

Consequence, and the biggest operational constraint in the project: **the script
must be running before 09:15**. VWAP is anchored to the session open and built
tick by tick. Start it at 09:20 and VWAP is anchored to 09:20 — a different line
than the one on your chart. There is no backfill.

→ `BarAggregator`, `SessionVWAP` in `strategy.py`.

> **Superseded 2026-09-03: candles now come from the historical endpoint.**
> Tick-built candles were measurably wrong — the 09:15 open was out by 32.45 pts
> on 2026-09-03 and a high by 48.45 pts on 2026-09-02, because the websocket
> sends throttled snapshots (~1/sec) rather than every trade. The 180-pt filter
> kills whole sessions off that number. `BarAggregator` is deleted; the session
> is backfilled from 09:15 at startup and each candle is polled ~8s after it
> closes. Ticks remain only for the live LTP target check. Live and `--replay`
> now read identical data.
>
> **The premise for this decision had already changed.** Zerodha cut Kite Connect
> from ₹2000 to ₹500/month, and the ₹500 tier now **bundles historical candle
> data** — there is no separate add-on. So the "no historical" constraint this
> whole design works around no longer applies.
>
> Reworking to backfill candles from the historical endpoint would remove the
> before-09:15 launch requirement, make VWAP correct on a late or restarted run,
> and make real backtesting possible. Not yet implemented — see
> [Still open](#still-open).

**Q: How does the daily Kite access token get minted?**
**A: Separate login helper.**

Kite tokens expire every morning. `login.py` prints the login URL, you paste the
`request_token` back once, it caches the `access_token` to `.kite_token.json` at
0600. `strategy.py` only reads that cache and refuses to start if the cached
date isn't today.

Rejected: full TOTP automation. It would have meant storing your Zerodha
password and 2FA seed on disk — anyone with read access to the machine would own
the trading account. Not worth twenty seconds a morning.

→ `login.py`, `load_token()`.

---

## 2. Instrument

**Q: Which instrument?**
**A: BANKNIFTY index.**

Signal is computed on `NSE:NIFTY BANK`. You place trades manually in whatever
you like.

**Q: The index feed carries no volume, but your VWAP formula needs volume. Where does it come from?**
**A: Index candles + futures volume.**

You also supplied the formula:

```
cum_pv  += ((high + low + close) / 3) * volume     # per candle
cum_vol += volume
vwap     = cum_pv / cum_vol                        # reset both each session
```

So OHLC comes from the index, `volume` is borrowed from the nearest-expiry
BANKNIFTY future. VWAP therefore lands on the *index* price scale and is
directly comparable to index closes — the futures data is only a weight.

Tradeoff worth remembering: the weight reflects futures activity, not spot
activity. Near expiry, when futures volume gets erratic, VWAP's shape shifts
slightly.

Rejected: running everything on the future (cleaner, but you wanted index
levels); tick-count pseudo-volume; dropping VWAP for a moving average.

**Q: Which futures contract?**
**A: Auto-resolve nearest expiry.**

Script pulls the NFO instrument dump at startup, picks the nearest non-expired
BANKNIFTY FUT, rolls automatically each month. No monthly config edit.

→ `resolve_instruments()`.

---

## 3. The box

**Q: What defines the box Low/High?**
**A: Manual daily input.**

Not an opening range, not previous-day levels. You decide the box and type it in.

**Q: How do you want to enter it?**
**A: JSON config file, with a date check.**

`config.json` carries `date`, `side`, `box_low`, `box_high`. The script refuses
to run if the date isn't today, so a stale box can't quietly trade yesterday's
levels. `--ignore-date` overrides when you mean it.

→ `load_config()`.

**Q: When is the box considered broken?**

This one needed a detour — you asked what "box breach" even meant. Three
readings were on the table: candle close outside, any tick outside, or close
outside by a margin.

**A: Close outside + buffer.**

**Q: How big a buffer?**
**A: 10 points.**

So BUY needs a candle closing above `box_high + 10`; SELL below `box_low − 10`.
Intrabar wicks poking out of the box are ignored entirely — consistent with
every other rule in the strategy being close-based.

> **Superseded 2026-09-01.** The buffer now applies to **both** edges, forming a
> no-trade zone, and a candle counts as inside if any part of its range overlaps
> it — a wick *is* enough. See section 7a.

→ `zone_low`, `zone_high` in `Strategy.__post_init__`.

---

## 4. Timeframe

**Q: Candle timeframe for C1 / C2 / L1 / L2?**
**A: 5 minute.**

Boundaries fall naturally on 09:15, 09:20, 09:25 … because 09:15 is itself a
multiple of 5 past the hour. No special anchoring needed.

→ `candle_minutes` in config, `BarAggregator._bucket_of()`.

---

## 5. The violence filter

The chart says *"Breakout candle moved > 180 pts?"* — Yes → "Too violent, no
trade". Two things were undefined: how the move is measured, and which candle.

**Q: Measured how?**
**A: Candle range — `|high − low| < 180` to trade.**

**Q: Which candle, and how far does a violation kill?**
**A: The 09:15 candle only. Kills the whole day.**

This is a **deliberate departure from the flowchart**. The chart tests the
*breakout* candle; you test the *first session* candle, before any breakout
hunting starts. Confirmed twice, once after I flagged the discrepancy
explicitly.

Practical effect: a wild opening five minutes stands the whole day down, even if
things calm right after. Later candles are never range-tested.

→ `Strategy._first_bar()`.

---

## 6. Direction flag

**Q: Who sets "Day flagged BUY or SELL"?**
**A: Manual, pre-market.** Lives in `config.json` as `side`.

**Q: Day is flagged BUY but price breaks DOWN out of the box. What happens?**
**A (2026-09-01, superseded): Day is dead.**

**A (revised 2026-09-01): the wrong-side kill is removed.**

You raised this yourself after watching it fire: *"there is no trade zone only in
the box"*. Re-reading `zerodha.jpeg` confirms it — the chart asks "price inside
the box?", then "day flagged BUY or SELL?", and **never compares breakout
direction against the flag**. The box is a no-trade zone, not a direction gate.
The original answer was mine-asked and yours-given, but it did not match the
chart, and it cost a live day: on 2026-09-01 the 09:35 candle closed below the
box and killed a session that was back inside the box and climbing two hours
later.

What replaced it is in section 7a.

→ `Strategy._hunt_c1()`.

---

## 7a. The box as a no-trade zone (revised 2026-09-01)

Replacing the wrong-side kill raised four sub-questions.

**Q: A candle "touches" the box — wick or close?**
**A: Any touch.** A candle is inside if any part of its range overlaps the zone.
Closing outside is not enough; the whole candle must be clear.

**Q: Where does the buffer sit?**
**A: Both edges.** The no-trade zone is `[box_low − buffer, box_high + buffer]`.
With a 54000–54300 box and a 10-pt buffer that is 53990–54310, and a candle is
clear above only if `low > 54310`.

**Q: Market gaps open entirely outside the box, never trading inside it. Valid?**
**A: Yes** — treat it as already broken out and hunt normally from the first
candle.

**Q: Price returns to the zone. What happens?**
**A: Day is dead** — and the killer arms on **the first candle fully clear of the
zone**, not on the first valid C1. So a candle can clear the zone, fail the VWAP
test, and still commit the day; a later touch then ends it.

**Q: Does "any touch" apply beyond C1?**
**A: Everywhere.** Both setup candles must be entirely clear, and a touch while a
position is open **closes the position immediately** — regardless of target or
the L1/L2 stop.

**Q: Does the box test care which side price is on?**
**A: No — and this was my error, corrected 2026-09-01.**

For several rounds the code required a BUY to break out *above* the box. That
constraint is nowhere in `zerodha.jpeg`. The chart asks only "price inside the
box?" then routes on the flag; the BUY/SELL conditions that follow are purely
**VWAP-relative**:

```
BUY : C1 close > VWAP,  C2 close > High(C1)
SELL: C1 close < VWAP,  C2 close < Low(C1)
```

So price trading *below* the box is a perfectly good BUY day, provided a clear
candle closes above VWAP. You caught this by pointing at a real 10:15 / 10:20
BUY entry that formed below the box while my code saw nothing.

Geometry worth knowing: a below-box BUY has limited headroom, because price
rising toward the box eventually touches the zone and closes the trade. The
mirror applies to an above-box SELL.

Consequence worth knowing, flagged at the time and chosen anyway: a breakout
candle usually *starts* inside the box, so its low sits in the zone and it is
disqualified as C1. In practice C1 is typically the candle *after* the visible
breakout. Combined with the re-entry kill, the strategy demands a decisive, clean
exit from the zone that never wicks back.

→ `Strategy.clear_above/clear_below/touches_zone`, `_hunt_c1`, `_hunt_c2`.

## 7. C1 and C2

The chart shows `C1 close > VWAP AND C2 close > High of C1` as a single diamond,
which hides the timing question entirely.

**Q: Must C2 be the candle immediately after C1?**
**A (your words):** *"Suppose C1 close above VWAP so we will wait till all the
candles are up above vwap it will hunt for C2. C2 means the close should be more
than high of C1. In case, as C1 closes above VWAP and suddenly the next candle
closes below VWAP or any of the candle closes below VWAP as it was already
hunting for C2 the C1 becomes invalid for BUY setup same goes for SELL."*

So:
- C2 is not the next candle — it is the **first later candle** closing beyond
  `C1.high` (BUY) / `C1.low` (SELL). No fixed expiry.
- Throughout the hunt, every candle must stay on the right side of VWAP. One
  close back through VWAP **invalidates C1**.

That raised two follow-ups about how much the day forgives.

**Q: C1 breaks out of the box but closes on the WRONG side of VWAP. What then?**
**A: Skip it, keep looking.**

Not fatal. Test every later candle until one closes both outside the box and on
the right side of VWAP.

**Q: C1 was valid, then a candle closed back through VWAP and killed it. What then?**
**A: Reset and re-hunt.**

Also not fatal. Discard C1, go back to hunting a fresh one. Only the 14:45
cutoff, a wrong-side breakout, or a violent open ends the day early.

→ `Strategy._hunt_c1()` / `_hunt_c2()`. Note `_hunt_c2` calls back into
`_hunt_c1` with the same bar after an invalidation, so a wrong-side breakout
still registers on the candle that killed C1.

---

## 8. Entry, target, stop

**Q: `Target = High(C2) − Low(C1)` is a distance. Where is it applied?**
**A: Entry + distance.**

```
BUY : entry = C2.close,  target = entry + (C2.high − C1.low)
SELL: entry = C2.close,  target = entry − (C1.high − C2.low)
```

**Q: Target hit detected on live price or candle close?**
**A: Live LTP touch.**

The one place the strategy uses ticks rather than closes. The instant price
trades at or beyond target, the email fires.

**Q: The stop is "L1 closes past VWAP, then L2 closes past L1's extreme". What resets it?**
**A: Reset on VWAP recovery.**

```
L1 armed   : a candle closes back through VWAP. Remember its low (BUY) / high (SELL).
L1 disarmed: a later candle closes on the right side of VWAP again. Position stands.
L2 / exit  : while L1 is armed, a candle closes past L1's remembered extreme.
```

L1 is not re-tightened by subsequent candles — once armed it keeps its original
extreme until VWAP recovery clears it.

→ `Strategy._enter()`, `_manage()`, `on_price()`.

---

## 9. Execution and alerts

**Q: How should the stop-loss and target be enforced?**
**A: Alert only — you click.**

The script places **no orders**. No quantity, product type, order type or margin
questions were needed as a result, and none are in the config.

Corollary: the script has no idea whether you actually took the trade. After
firing ENTER it assumes you did and keeps tracking.

**Q: After the ENTER alert, what else should it send?**
**A: All four** — target hit, stop triggered, 15:10 square-off, and L1
armed/disarmed early warnings.

**Q: Delivery channel?**
**A: Email.**

**Q: Transport?**
**A: Both, chosen by config.** `"transport": "smtp"` is Gmail / Workspace SMTP —
`smtp.gmail.com:587` STARTTLS with a **Google app password** (Workspace rejects
account passwords over SMTP), read from `ZERODHA_SMTP_PASSWORD`.

**Q: Why a second transport?**
**A: Render blocks outbound SMTP.** Port 25 is blocked on every Render instance
type, and since 26 September 2025 ports 465 and 587 are blocked on free web
services too, so no SMTP client can reach a mail server from there. Paying for
an instance would unblock 465/587, but an HTTPS email API costs nothing and
removes the dependency on the host's egress rules entirely.

`"transport": "http"` therefore POSTs to the SendGrid v3 API over port 443,
which no PaaS blocks — the same port the Kite API already uses, so it is proven
to work from the deployed container. Key read from `ZERODHA_SENDGRID_KEY`. Both
credentials come from the environment — never from `config.json`, never in code.

SendGrid over its own SMTP relay on port 2525 would also slip past the block,
but the HTTPS API needs no long-lived socket and gives a status code per send.

Either transport failing logs an error and is swallowed; it never kills the run.

→ `Emailer`, `Emailer._send_http`, `Emailer._send_smtp`.

---

## 10. Session handling

**Q: When does the script stop?**
**A: Stop hunting 14:45, square-off alert 15:10.**

**Q: After a position exits, what next?**
**A: Done for the day.** One trade maximum. The script reaches a terminal state
and exits.

Terminal states: too violent, wrong-side breakout, cutoff reached with no setup,
target hit, stop, square-off.

→ `Strategy.on_clock()`, `_finish()`.

---

## Consolidated spec

| Parameter | Value | Where |
|---|---|---|
| Signal instrument | `NSE:NIFTY BANK` | `config.index_symbol` |
| Volume source | nearest-expiry BANKNIFTY FUT, auto-rolled | `config.fut_name` |
| Candle | 5 min, built from ticks | `config.candle_minutes` |
| Box | manual, dated | `config.box_low/box_high/date` |
| Direction | manual | `config.side` |
| No-trade zone | box ± buffer on both edges; any wick overlap counts as inside | `config.breakout_buffer` |
| Breakout | candle entirely clear of the zone; direction not tested | `_hunt_c1` |
| Re-entry | after the first fully-clear candle, any touch → day dead | `_back_in_the_box` |
| Violence filter | 09:15 candle range > 180 → day dead | `config.violent_range` |
| VWAP | `Σ((H+L+C)/3 × V) / ΣV`, session-reset | `SessionVWAP` |
| C1 | first candle clear of the zone on the flagged side, also right side of VWAP | `_hunt_c1` |
| C2 | first later candle also clear of the zone, closing beyond C1's extreme | `_hunt_c2` |
| C1 invalidation | any close back through VWAP → re-hunt | `_hunt_c2` |
| Entry | C2 close | `_enter` |
| Target | entry ± (High(C2) − Low(C1)), LTP touch | `_enter`, `on_price` |
| Stop | L1 past VWAP → L2 past L1 extreme; VWAP recovery disarms | `_manage` |
| No new entries | 14:45 | `config.no_new_entry_after` |
| Square-off alert | 15:10 | `config.squareoff_alert_at` |
| Trades per day | 1 | terminal states |
| Orders placed | none | — |

---

## Decided without asking — flag if wrong

Two gaps were closed by choosing a default rather than sending another round of
questions. Both are called out in `README.md` too.

1. **`first_bar_eligible_as_c1` (default `true`).** Moving the 180-pt test to the
   09:15 candle left it open whether that candle can itself be C1. Currently it
   can — if it survives the violence test and closes outside the box on the right
   side of VWAP. Set `false` to make C1 hunting start at 09:20.

2. **Breakout with VWAP still undefined.** If a breakout candle arrives before
   any futures volume has accumulated, VWAP is `None`. The candle is skipped with
   a logged warning — treated as neither a pass nor a failure.

A third, smaller choice: **VWAP includes the bar being evaluated.** A bar closes,
VWAP is updated with it, *then* the bar's close is tested against the new VWAP.
Standard practice, but it is a choice — say the word to test against the prior
bar's VWAP instead.

---

## Still open

Not asked, not decided, not implemented. Raise these if they matter.

- **Silent death — fixed 2026-09-05.** A read timeout on the historical endpoint
  raised `SystemExit` from inside the polling loop and ended a live session at
  13:22 with no email, after four hours of hunting. Transient fetch failures now
  retry with backoff and only escalate after 60s (warn) and 300s (give up), and
  `run()` mails before dying on any unhandled exception. See `FetchError`.
- **Crash recovery.** No state is persisted. If the script dies mid-trade,
  restarting it starts a fresh day — it will not remember an open position, and
  its VWAP will be re-anchored to the restart time and therefore wrong. Currently
  the answer is "don't let it crash", which is not much of an answer.
- **Websocket gaps.** Partially addressed. On 2026-09-01 the feed died at ~11:50
  with the socket stuck in `CLOSE-WAIT` and no `on_close` callback — the script
  spun silently for 39 minutes making no decisions. There is now a watchdog: it
  warns and emails after 90s without a tick, forces a reconnect at 150s, and
  emails then exits at 420s rather than pretending to watch. A heartbeat line is
  logged every 5 minutes. What is still not handled: ticks lost *during* a short
  gap still leave the affected candle incomplete, and nothing flags that.
- **Hard max-loss.** The L1/L2 stop can sit a long way from entry. There is no
  rupee or point cap. Less pressing given alert-only operation, but it is absent.
- **Backtesting.** `simtest.py` replays nine hand-built scenarios against the
  state machine. It is a correctness harness, not a backtester — there is no
  historical data path — though the ₹500 Connect tier now includes historical
  candles, so one could be built. See the note in section 1.
- **Historical backfill.** Now that historical candles are bundled with the
  subscription, the script could fetch the session so far at startup instead of
  requiring a pre-09:15 launch. That would fix the late-start and
  crash-restart problems in one go. Biggest available improvement; not done.
- **Where it runs.** Assumed to be a machine that is awake and online from before
  09:15. No systemd unit, cron entry or supervision is included.
- **Trailing stop.** Not discussed. The stop is the L1/L2 rule only.
