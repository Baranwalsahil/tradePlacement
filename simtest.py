"""Offline exercise of the Strategy state machine (kiteconnect stubbed).

Box 54000-54300, buffer 10, so the NO-TRADE ZONE is 53990-54310.
A candle is "clear above" only if its LOW > 54310, "clear below" only if its
HIGH < 53990. Any overlap is a touch, and ANY touch ends the day outright -
including a session that merely opens inside the zone.
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
DAY = datetime(2026, 9, 4, tzinfo=IST)
failures = []
IN_BOX = "NO TRADE TODAY - price in the box"


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        print(f"    >>> ALERT: {subject}")


def bar(hh, mm, o, h, l, c, v=1000):
    st = DAY.replace(hour=hh, minute=mm)
    return S.Bar(st, st + timedelta(minutes=5), o, h, l, c, v)


BASE = dict(side="BUY", box_low=54000, box_high=54300, breakout_buffer=10,
            violent_range=180, candle_minutes=5, first_bar_eligible_as_c1=True,
            no_new_entry_after="14:45", squareoff_alert_at="15:10")


def scenario(name, cfg_over, steps, expect_state=None, expect_alerts=None):
    print(f"\n=== {name} ===")
    cfg = {**BASE, **cfg_over}
    m = FakeMailer()
    st = S.Strategy(cfg=cfg, notify=m)
    for kind, payload, vwap in steps:
        if kind == "bar":
            st.on_bar(payload, vwap)
        elif kind == "tick":
            st.on_price(DAY.replace(hour=13), payload)
        elif kind == "clock":
            st.on_clock(DAY.replace(hour=payload[0], minute=payload[1]))
    print(f"  state={st.state.value} alerts={m.sent}")
    if expect_state is not None and st.state.value != expect_state:
        print(f"  FAIL state: want {expect_state}")
        failures.append(f"{name}/state")
    if expect_alerts is not None and m.sent != expect_alerts:
        print(f"  FAIL alerts: want {expect_alerts}")
        failures.append(f"{name}/alerts")
    return st, m


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


# Shared candles. The opening candle must already be clear of the zone or the
# day ends at 09:15; it is also given a VWAP above its close so it is skipped as
# a C1 candidate and the interesting logic happens on later candles.
INSIDE      = bar(9, 15, 54100, 54180, 54080, 54150)   # wholly inside the zone
OPEN_ABOVE  = bar(9, 15, 54330, 54380, 54320, 54340)   # low 54320 > 54310: clear
OPEN_BELOW  = bar(9, 15, 53960, 53985, 53930, 53950)   # high 53985 < 53990: clear
WICKS_IN    = bar(9, 20, 54320, 54400, 54300, 54380)   # low 54300 <= 54310: touch
C1_ABOVE    = bar(9, 20, 54340, 54420, 54325, 54400)   # clear, high 54420
FILLER      = bar(9, 25, 54400, 54415, 54330, 54380)   # clear, below C1's high
C2_ABOVE    = bar(9, 30, 54380, 54500, 54370, 54480)   # clear, close > 54420

# --------------------------------------------------------------------------- #
# The filter is "> 180", so a range of exactly 180 survives - and this candle is
# clear of the zone and above VWAP, so it goes straight on to become C1.
scenario("range of exactly 180 survives the filter", {}, [
    ("bar", bar(9, 15, 54330, 54500, 54320, 54450), 54350.0),   # range 180.0
], expect_state="HUNT_C2", expect_alerts=[])

scenario("range just over 180 kills the day", {}, [
    ("bar", bar(9, 15, 54330, 54501, 54320, 54450), 54350.0),
], expect_state="DONE", expect_alerts=["NO TRADE TODAY - too violent"])

# --------------------------------------------------------------------------- #
scenario("a session that OPENS inside the zone dies at 09:15", {}, [
    ("bar", INSIDE, 54120.0),
], expect_state="DONE", expect_alerts=[IN_BOX])

scenario("a wick into the zone kills the day", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", WICKS_IN, 54355.0),        # closes 54380 but low 54300 touches
], expect_state="DONE", expect_alerts=[IN_BOX])

# --------------------------------------------------------------------------- #
st, m = scenario("BUY happy path, above the box", {}, [
    ("bar", OPEN_ABOVE, 54350.0),      # clear, close 54340 < vwap: skipped
    ("bar", C1_ABOVE,   54355.0),      # clear, close 54400 > vwap: C1
    ("bar", FILLER,     54360.0),      # clear, 54380 not > C1.high 54420
    ("bar", C2_ABOVE,   54370.0),      # clear, 54480 > 54420: C2
], expect_state="IN_TRADE", expect_alerts=["ENTER BUY @ 54480.00"])
check("entry", st.entry, 54480)
check("target = entry + (H(C2) - L(C1))", st.target, 54480 + (54500 - 54325))
st.on_price(DAY.replace(hour=13), st.target)
check("LTP touch books it", st.state.value, "DONE")

# --------------------------------------------------------------------------- #
# Direction is not part of the box test: a BUY forms entirely BELOW the box,
# because the entry conditions are VWAP-relative, not box-relative.
st, m = scenario("BUY forms below the box (VWAP-relative)", {}, [
    ("bar", OPEN_BELOW, 53960.0),                              # close 53950 < vwap: skip
    ("bar", bar(9, 20, 53950, 53980, 53920, 53970), 53955.0),  # close > vwap: C1, high 53980
    ("bar", bar(9, 25, 53970, 53989, 53960, 53985), 53960.0),  # close 53985 > 53980: C2
], expect_state="IN_TRADE", expect_alerts=["ENTER BUY @ 53985.00"])
check("below-box BUY entry", st.entry, 53985)
check("below-box BUY target", st.target, 53985 + (53989 - 53920))

# --------------------------------------------------------------------------- #
scenario("clear of the zone but wrong side of VWAP is skipped, day lives", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", bar(9, 20, 54340, 54420, 54325, 54360), 54400.0),  # close < vwap
], expect_state="HUNT_C1", expect_alerts=[])

# --------------------------------------------------------------------------- #
scenario("clear below, then a touch, kills the day", {}, [
    ("bar", OPEN_BELOW, 53960.0),
    ("bar", bar(9, 20, 53950, 54100, 53940, 54050), 53980.0),  # high 54100: touch
], expect_state="DONE", expect_alerts=[IN_BOX])

scenario("a touch while hunting C2 kills the day", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", C1_ABOVE,   54355.0),                              # C1
    ("bar", bar(9, 25, 54400, 54410, 54250, 54300), 54360.0),  # low dips into zone
], expect_state="DONE", expect_alerts=[IN_BOX])

# --------------------------------------------------------------------------- #
st, m = scenario("C1 killed by a VWAP cross, fresh C1, then entry", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", C1_ABOVE,   54355.0),                              # C1, high 54420
    ("bar", bar(9, 25, 54400, 54450, 54330, 54340), 54360.0),  # close < vwap: C1 dies
    ("bar", bar(9, 30, 54340, 54520, 54330, 54500), 54370.0),  # fresh C1, high 54520
    ("bar", bar(9, 35, 54500, 54600, 54480, 54560), 54380.0),  # close 54560 > 54520: C2
], expect_state="IN_TRADE", expect_alerts=["ENTER BUY @ 54560.00"])
check("re-hunted entry", st.entry, 54560)

# --------------------------------------------------------------------------- #
scenario("stop sequence: arm, disarm, re-arm, exit", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", C1_ABOVE,   54355.0),
    ("bar", FILLER,     54360.0),
    ("bar", C2_ABOVE,   54370.0),                              # entry 54480
    ("bar", bar(9, 35, 54480, 54490, 54420, 54430), 54450.0),  # < vwap: L1, low 54420
    ("bar", bar(9, 40, 54430, 54520, 54425, 54500), 54460.0),  # > vwap: disarm
    ("bar", bar(9, 45, 54500, 54505, 54400, 54410), 54470.0),  # < vwap: L1, low 54400
    ("bar", bar(9, 50, 54410, 54415, 54350, 54360), 54480.0),  # < 54400: STOP
], expect_state="DONE", expect_alerts=[
    "ENTER BUY @ 54480.00", "STOP WARNING - L1 armed", "STOP WARNING CLEARED",
    "STOP WARNING - L1 armed", "STOP - exit BUY now"])

# --------------------------------------------------------------------------- #
scenario("touching the box closes an open position", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", C1_ABOVE,   54355.0),
    ("bar", FILLER,     54360.0),
    ("bar", C2_ABOVE,   54370.0),                              # entry 54480
    ("bar", bar(9, 35, 54480, 54490, 54100, 54460), 54380.0),  # wick into the box
], expect_state="DONE", expect_alerts=[
    "ENTER BUY @ 54480.00", "CLOSE BUY - price back in the box"])

# --------------------------------------------------------------------------- #
scenario("square off at 15:10", {}, [
    ("bar", OPEN_ABOVE, 54350.0),
    ("bar", C1_ABOVE,   54355.0),
    ("bar", FILLER,     54360.0),
    ("bar", C2_ABOVE,   54370.0),
    ("clock", (15, 10), None),
], expect_state="DONE", expect_alerts=[
    "ENTER BUY @ 54480.00", "SQUARE OFF - session ending"])

scenario("cutoff with no setup", {}, [
    ("bar", OPEN_ABOVE, 54350.0),      # clear but below VWAP: never a C1
    ("clock", (14, 45), None),
], expect_state="DONE", expect_alerts=["NO TRADE TODAY - cutoff reached"])

# --------------------------------------------------------------------------- #
st, m = scenario("gap open clear of the zone: 09:15 itself becomes C1", {}, [
    ("bar", bar(9, 15, 54330, 54450, 54320, 54430), 54380.0),  # close > vwap: C1
    ("bar", bar(9, 20, 54430, 54560, 54420, 54520), 54400.0),  # close > 54450: C2
], expect_state="IN_TRADE", expect_alerts=["ENTER BUY @ 54520.00"])
check("gap-open entry", st.entry, 54520)

scenario("first_bar_eligible_as_c1=False still tests the zone at 09:15",
         {"first_bar_eligible_as_c1": False}, [
    ("bar", INSIDE, 54120.0),
], expect_state="DONE", expect_alerts=[IN_BOX])

# --------------------------------------------------------------------------- #
st, m = scenario("SELL mirror", {"side": "SELL"}, [
    ("bar", bar(9, 15, 53960, 53985, 53940, 53975), 53970.0),  # close > vwap: skip
    ("bar", bar(9, 20, 53975, 53980, 53930, 53940), 53965.0),  # close < vwap: C1, low 53930
    ("bar", bar(9, 25, 53940, 53950, 53880, 53900), 53950.0),  # close < 53930: C2
], expect_state="IN_TRADE", expect_alerts=["ENTER SELL @ 53900.00"])
check("SELL entry", st.entry, 53900)
check("SELL target = entry - (H(C1) - L(C2))", st.target, 53900 - (53980 - 53880))

# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all strategy checks passed")
