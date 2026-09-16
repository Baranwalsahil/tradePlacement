"""Offline checks on candle sourcing and VWAP (kiteconnect stubbed, no network).

Candles come from Kite's historical endpoint, not from ticks: the websocket
sends throttled snapshots (~1/sec) which put the 09:15 open out by 32 pts and a
high by 48 pts on measured days, and the 180-pt filter kills whole sessions off
that number. These tests pin the merge (index OHLC + futures volume), the
exclusion of half-formed candles, VWAP accumulation, and the retry/escalation
behaviour that replaced the SystemExit which silently ended a live session on
2026-09-04.
"""
import sys, types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

stub = types.ModuleType("kiteconnect")
stub.KiteConnect = object
stub.KiteTicker = object
sys.modules["kiteconnect"] = stub
sys.path.insert(0, "/home/sahil/scripts/zerodha")

import strategy as S

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 9, 3, tzinfo=IST)
failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


class FakeKite:
    """Serves canned 5-min candles: index has no volume, the future has volume."""

    IDX, FUT = 260105, 17507842

    def __init__(self, index_rows, fut_rows):
        self.index_rows, self.fut_rows = index_rows, fut_rows
        self.calls = []

    def historical_data(self, token, frm, to, interval):
        self.calls.append((token, frm, to, interval))
        rows = self.index_rows if token == self.IDX else self.fut_rows
        out = []
        for hh, mm, o, h, l, c, v in rows:
            ts = DAY.replace(hour=hh, minute=mm)
            if ts < frm or ts > to:
                continue
            out.append({"date": ts, "open": o, "high": h, "low": l,
                        "close": c, "volume": v})
        return out


# Real BANKNIFTY index candles for 2026-09-03, with matching futures volume.
IDX_ROWS = [
    (9, 15, 57497.85, 57661.40, 57467.05, 57577.15, 0),
    (9, 20, 57577.15, 57620.00, 57540.00, 57600.00, 0),
    (9, 25, 57600.00, 57650.00, 57580.00, 57610.00, 0),
]
FUT_ROWS = [
    (9, 15, 57800.00, 57960.00, 57770.00, 57880.00, 91680),
    (9, 20, 57880.00, 57920.00, 57840.00, 57900.00, 41000),
    (9, 25, 57900.00, 57950.00, 57880.00, 57910.00, 33000),
]

# --------------------------------------------------------------------------- #
print("\n=== index OHLC merged with futures volume ===")
k = FakeKite(IDX_ROWS, FUT_ROWS)
bars = S.fetch_candles(k, k.IDX, k.FUT,
                       DAY.replace(hour=9, minute=15),
                       DAY.replace(hour=9, minute=30), 5)
for b in bars:
    print(f"    {b}")
check("candle count", len(bars), 3)
check("OHLC is the index, not the future",
      (bars[0].open, bars[0].high, bars[0].low, bars[0].close),
      (57497.85, 57661.40, 57467.05, 57577.15))
check("volume is the future's", bars[0].volume, 91680)
check("09:15 range", round(bars[0].range, 2), 194.35)
check("two historical calls (index + future)", len(k.calls), 2)
check("interval string", k.calls[0][3], "5minute")

# --------------------------------------------------------------------------- #
print("\n=== the tick-built version of that same candle was wrong ===")
# What the live tick aggregator produced on 2026-09-03, from logs/2026-09-03.log
tick_built = (57530.30, 57660.75, 57472.10, 57577.15)
real = (bars[0].open, bars[0].high, bars[0].low, bars[0].close)
print(f"    tick-built O={tick_built[0]} H={tick_built[1]} L={tick_built[2]}")
print(f"    historical O={real[0]} H={real[1]} L={real[2]}")
check("open was off by 32.45", round(real[0] - tick_built[0], 2), -32.45)
check("tick range under-read the 180 filter",
      round((tick_built[1] - tick_built[2]), 1) < 194.3, True)

# --------------------------------------------------------------------------- #
print("\n=== half-formed candles are excluded ===")
k2 = FakeKite(IDX_ROWS, FUT_ROWS)
# 'to' lands mid-way through the 09:25 candle, so only 09:15 and 09:20 qualify.
partial = S.fetch_candles(k2, k2.IDX, k2.FUT,
                          DAY.replace(hour=9, minute=15),
                          DAY.replace(hour=9, minute=27), 5)
check("only closed candles returned", [f"{b.start:%H:%M}" for b in partial],
      ["09:15", "09:20"])

# --------------------------------------------------------------------------- #
print("\n=== pre-open candles are discarded ===")
k3 = FakeKite([(9, 0, 1.0, 2.0, 0.5, 1.5, 0)] + IDX_ROWS,
              [(9, 0, 1.0, 2.0, 0.5, 1.5, 99)] + FUT_ROWS)
pre = S.fetch_candles(k3, k3.IDX, k3.FUT,
                      DAY.replace(hour=9, minute=0),
                      DAY.replace(hour=9, minute=30), 5)
check("09:00 dropped", [f"{b.start:%H:%M}" for b in pre],
      ["09:15", "09:20", "09:25"])

# --------------------------------------------------------------------------- #
print("\n=== a candle with no futures volume gets volume 0 ===")
k4 = FakeKite(IDX_ROWS, FUT_ROWS[:1])   # future only has the 09:15 candle
sparse = S.fetch_candles(k4, k4.IDX, k4.FUT,
                         DAY.replace(hour=9, minute=15),
                         DAY.replace(hour=9, minute=30), 5)
check("volumes", [b.volume for b in sparse], [91680, 0, 0])

# --------------------------------------------------------------------------- #
print("\n=== VWAP accumulates across candles and ignores zero-volume ones ===")
vw = S.SessionVWAP()
check("no volume yet -> None", vw.value, None)
vw.update(bars[0])
tp0 = (bars[0].high + bars[0].low + bars[0].close) / 3
check("one candle -> its typical price", round(vw.value, 4), round(tp0, 4))
vw.update(bars[1])
vw.update(bars[2])
exp_pv = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
exp_vol = sum(b.volume for b in bars)
check("three candles", round(vw.value, 6), round(exp_pv / exp_vol, 6))
check("cum_vol", vw.cum_vol, exp_vol)
zero = S.Bar(DAY, DAY + timedelta(minutes=5), 1, 2, 0.5, 1.5, 0)
before = vw.value
vw.update(zero)
check("zero-volume candle changes nothing", vw.value, before)

# --------------------------------------------------------------------------- #
print("\n=== interval strings ===")
check("1 min", S._interval(1), "minute")
check("5 min", S._interval(5), "5minute")
check("15 min", S._interval(15), "15minute")

# --------------------------------------------------------------------------- #
print("\n=== a failed fetch raises FetchError, not SystemExit ===")
# Regression: on 2026-09-04 a single read timeout raised SystemExit from inside
# the polling loop and ended a live session silently, four hours in.


class BrokenKite(FakeKite):
    def historical_data(self, token, frm, to, interval):
        raise Exception(
            "HTTPSConnectionPool(host='api.kite.trade', port=443): "
            "Read timed out. (read timeout=7)")


broke = BrokenKite([], [])
try:
    S.fetch_candles(broke, broke.IDX, broke.FUT, DAY, DAY, 5)
    check("raised", "nothing", "FetchError")
except SystemExit:
    check("exception type", "SystemExit", "FetchError")
except S.FetchError as e:
    check("exception type", type(e).__name__, "FetchError")
    check("message carries the cause", "Read timed out" in str(e), True)
except Exception as e:
    check("exception type", type(e).__name__, "FetchError")

check("FetchError is not a SystemExit",
      issubclass(S.FetchError, SystemExit), False)
check("FetchError is catchable as Exception",
      issubclass(S.FetchError, Exception), True)

print("\n=== retry/escalation thresholds are ordered sanely ===")
check("warn before fatal", S.FETCH_WARN_AFTER < S.FETCH_FATAL_AFTER, True)
check("backoff starts at POLL_RETRY", S.POLL_RETRY < S.FETCH_BACKOFF_MAX, True)
check("http timeout beats kite's 7s default", S.HTTP_TIMEOUT > 7, True)
backoffs = [min(S.POLL_RETRY * 2 ** (n - 1), S.FETCH_BACKOFF_MAX)
            for n in range(1, 8)]
print(f"    backoff sequence: {backoffs}")
check("backoff is monotonic and capped",
      backoffs == sorted(backoffs) and max(backoffs) == S.FETCH_BACKOFF_MAX, True)
# Time to reach the fatal threshold must be long enough to ride out a blip.
total, t = 0, []
for n in range(1, 30):
    total += min(S.POLL_RETRY * 2 ** (n - 1), S.FETCH_BACKOFF_MAX)
    t.append(total)
attempts = next(i + 1 for i, v in enumerate(t) if v >= S.FETCH_FATAL_AFTER)
print(f"    ~{attempts} attempts spanning {S.FETCH_FATAL_AFTER}s before giving up")
check("gives up only after several attempts", attempts >= 5, True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all candle-sourcing, VWAP and crash-handling checks passed")
