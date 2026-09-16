#!/usr/bin/env python3
"""Compare what the live run logged against what Kite's history says now.

The live run fetches each candle POLL_DELAY seconds after it closes. If Kite's
historical store is still settling at that moment - plausible at 09:15, the
busiest minute of the day - the candle it hands back can differ from the final
one. This script re-fetches those same candles days later, when they are
definitely settled, and diffs them.

    ./venv/bin/python verify_ohlc.py                # every log with a candle
    ./venv/bin/python verify_ohlc.py 2026-09-08 2026-09-09
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
BAR_RE = re.compile(
    r"^(?P<wall>\d{2}:\d{2}:\d{2}).*?bar (?P<hhmm>\d{2}:\d{2}) "
    r"O=(?P<o>[\d.]+) H=(?P<h>[\d.]+) L=(?P<l>[\d.]+) C=(?P<c>[\d.]+) "
    r"V=(?P<v>\d+)"
)


def logged_bars(path: Path):
    """Every candle the live run logged, with the wall-clock time it appeared."""
    out = []
    for line in path.read_text().splitlines():
        m = BAR_RE.search(line)
        if m:
            out.append({
                "wall": m["wall"], "hhmm": m["hhmm"],
                "o": float(m["o"]), "h": float(m["h"]),
                "l": float(m["l"]), "c": float(m["c"]), "v": int(m["v"]),
            })
    return out


def main() -> int:
    tok = json.loads((HERE / ".kite_token.json").read_text())
    kite = KiteConnect(api_key=tok["api_key"], timeout=20)
    kite.set_access_token(tok["access_token"])
    try:
        kite.profile()
    except Exception as exc:  # noqa: BLE001
        print(f"Token is dead ({exc}). Run `python login.py` first.", file=sys.stderr)
        return 1

    cfg = json.loads((HERE / "config.json").read_text())
    idx_token = kite.ltp([cfg["index_symbol"]])[cfg["index_symbol"]]["instrument_token"]
    minutes = int(cfg["candle_minutes"])

    days = sys.argv[1:] or sorted(
        p.stem for p in (HERE / "logs").glob("*.log")
    )

    worst = 0.0
    any_diff = False
    for day in days:
        path = HERE / "logs" / f"{day}.log"
        if not path.exists():
            print(f"\n{day}: no log")
            continue
        bars = logged_bars(path)
        if not bars:
            print(f"\n{day}: log has no candles")
            continue

        y, mo, d = map(int, day.split("-"))
        frm = datetime(y, mo, d, 9, 15, tzinfo=IST)
        to = datetime(y, mo, d, 15, 30, tzinfo=IST)
        try:
            hist = kite.historical_data(idx_token, frm, to, f"{minutes}minute")
        except Exception as exc:  # noqa: BLE001
            print(f"\n{day}: history fetch failed: {exc}")
            continue
        by_hhmm = {f"{c['date']:%H:%M}": c for c in hist}

        print(f"\n{'='*78}\n{day}  —  {len(bars)} logged candle(s)\n{'='*78}")
        print(f"{'candle':<8}{'fetched':<10}{'source':<12}"
              f"{'open':>11}{'high':>11}{'low':>11}{'close':>11}")
        for b in bars:
            ref = by_hhmm.get(b["hhmm"])
            if not ref:
                print(f"{b['hhmm']:<8}{b['wall']:<10}{'(no history)':<12}")
                continue
            lag = ""
            try:
                hh, mm = map(int, b["hhmm"].split(":"))
                closed = datetime(y, mo, d, hh, mm, tzinfo=IST) + timedelta(minutes=minutes)
                w = datetime.strptime(b["wall"], "%H:%M:%S").time()
                got = datetime(y, mo, d, w.hour, w.minute, w.second, tzinfo=IST)
                lag = f"+{(got - closed).total_seconds():.0f}s"
            except Exception:  # noqa: BLE001
                pass

            print(f"{b['hhmm']:<8}{b['wall']:<10}{'LOGGED live':<12}"
                  f"{b['o']:>11.2f}{b['h']:>11.2f}{b['l']:>11.2f}{b['c']:>11.2f}")
            print(f"{'':<8}{lag:<10}{'history now':<12}"
                  f"{ref['open']:>11.2f}{ref['high']:>11.2f}"
                  f"{ref['low']:>11.2f}{ref['close']:>11.2f}")
            do = ref["open"] - b["o"]; dh = ref["high"] - b["h"]
            dl = ref["low"] - b["l"];  dc = ref["close"] - b["c"]
            if any(abs(x) > 0.001 for x in (do, dh, dl, dc)):
                any_diff = True
                worst = max(worst, abs(do), abs(dh), abs(dl), abs(dc))
                print(f"{'':<8}{'':<10}{'DIFF':<12}"
                      f"{do:>+11.2f}{dh:>+11.2f}{dl:>+11.2f}{dc:>+11.2f}")
                lr, hr = b["h"] - b["l"], ref["high"] - ref["low"]
                v = float(cfg["violent_range"])
                if (lr > v) != (hr > v):
                    print(f"{'':<8}{'':<10}*** the 180 filter flips: "
                          f"logged range {lr:.1f} vs real {hr:.1f} ***")
            else:
                print(f"{'':<8}{'':<10}{'match':<12}")

    print()
    if any_diff:
        print(f"MISMATCH FOUND — worst single field off by {worst:.2f} points.")
        print("The live fetch is reading candles before Kite has settled them.")
        return 2
    print("Every logged candle matches history exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
