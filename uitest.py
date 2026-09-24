"""Offline checks on the arming / token / launch rules (no network, no browser).

Runs against temporary copies of config.json, state.json and .kite_token.json so
the real ones are never touched.
"""
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/sahil/scripts/zerodha")
import scheduler as S

IST = ZoneInfo("Asia/Kolkata")
failures = []

tmp = Path(tempfile.mkdtemp(prefix="bnf-uitest-"))
S.CONFIG = tmp / "config.json"
S.STATE = tmp / "state.json"
S.TOKEN = tmp / ".kite_token.json"

BASE_CFG = {
    "date": "2026-09-17", "side": "BUY", "box_low": 56000, "box_high": 56300,
    "breakout_buffer": 10, "violent_range": 180, "candle_minutes": 5,
    "first_bar_eligible_as_c1": True,
    "no_new_entry_after": "14:45", "squareoff_alert_at": "15:10",
    "index_symbol": "NSE:NIFTY BANK", "fut_name": "BANKNIFTY",
    "email": {"smtp_host": "x", "smtp_port": 587, "user": "u",
              "password_env": "Z", "from": "u", "to": ["u"]},
    "ui": {"port": 5000, "form_open": "07:00", "form_close": "15:45", "launch_at": "09:00",
           "wait_for_token_until": "14:45"},
}

THU = date(2026, 9, 17)          # a Thursday
SAT = date(2026, 9, 19)


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


def reset(cfg_over=None, state=None, token_date="skip"):
    S.save_config({**BASE_CFG, **(cfg_over or {})})
    S.save_state(state or {"armed_date": None, "armed_at": None,
                           "stopped_date": None, "run": None})
    if token_date == "skip":
        S.TOKEN.unlink(missing_ok=True)
    else:
        S.TOKEN.write_text(json.dumps(
            {"access_token": "x", "api_key": "k", "date": token_date,
             "user_id": "AB1234"}))


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# --------------------------------------------------------------------------- #
print("\n=== the form is open 07:00-15:45 on weekdays ===")
reset()
cfg = S.load_config()
check("06:59 closed", S.arm_window_open(cfg, at(THU, 6, 59))[0], False)
check("07:00 open", S.arm_window_open(cfg, at(THU, 7, 0))[0], True)
check("10:45 open", S.arm_window_open(cfg, at(THU, 10, 45))[0], True)
check("15:45 open", S.arm_window_open(cfg, at(THU, 15, 45))[0], True)
check("15:46 closed", S.arm_window_open(cfg, at(THU, 15, 46))[0], False)
check("21:00 closed", S.arm_window_open(cfg, at(THU, 21, 0))[0], False)
check("saturday closed", S.arm_window_open(cfg, at(SAT, 10, 0))[0], False)
cfg2 = S.load_config(); cfg2["ui"]["form_close"] = "12:00"; S.save_config(cfg2)
check("close is editable", S.arm_window_open(S.load_config(), at(THU, 13, 0))[0],
      False)

print("\n=== Submit & arm needs a login first ===")
reset()
cfg = S.load_config()
check("no token -> login error", S.arm_blocker(cfg, at(THU, 10, 45)),
      "Log in to Kite first, then Submit & arm.")
reset(token_date="2026-09-16")
check("yesterday's token -> login error", S.arm_blocker(cfg, at(THU, 10, 45)),
      "Log in to Kite first, then Submit & arm.")
reset(token_date=S.now().date().isoformat())
check("today's token, window open -> allowed",
      S.arm_blocker(cfg, S.now().replace(hour=10, minute=45)) if
      S.now().weekday() < 5 else None, None)
check("window closed beats login", S.arm_blocker(cfg, at(THU, 20, 0)).startswith(
      "Cannot arm: the form is open"), True)

# --------------------------------------------------------------------------- #
print("\n=== form validation ===")
reset()
cfg = S.load_config()
good = {"date": "2026-09-17", "side": "buy", "box_low": "56000",
        "box_high": "56300", "violent_range": "180"}
clean, errs = S.validate_form(cfg, good, at(THU, 10, 0))
check("valid form accepted", errs, [])
check("side upper-cased", clean["side"], "BUY")
check("numbers parsed", clean["box_low"], 56000.0)

_, errs = S.validate_form(cfg, {**good, "date": "2026-09-19"}, at(SAT, 10, 0))
check("saturday rejected", any("Saturday" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "date": "2026-09-10"}, at(THU, 21, 0))
check("past date rejected", any("must be today" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "date": "2026-09-18"}, at(THU, 10, 0))
check("tomorrow rejected", any("must be today" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "box_low": "57000"}, at(THU, 21, 0))
check("inverted box rejected", any("below box_high" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "side": "LONG"}, at(THU, 21, 0))
check("bad side rejected", any("BUY or SELL" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "violent_range": "0"}, at(THU, 21, 0))
check("zero violent_range rejected", any("positive" in e for e in errs), True)
_, errs = S.validate_form(cfg, {**good, "box_high": "abc"}, at(THU, 21, 0))
check("non-numeric rejected", any("must be a number" in e for e in errs), True)

# --------------------------------------------------------------------------- #
print("\n=== arming writes config and state ===")
reset()
clean, _ = S.validate_form(S.load_config(), good, at(THU, 10, 0))
S.arm(S.load_config(), clean)
saved = json.loads(S.CONFIG.read_text())
check("config date written", saved["date"], "2026-09-17")
check("config side written", saved["side"], "BUY")
check("state armed", S.load_state()["armed_date"], "2026-09-17")
check("armed for THU", S.is_armed_for(THU), True)
check("not armed for SAT", S.is_armed_for(SAT), False)

# --------------------------------------------------------------------------- #
print("\n=== token must be dated today ===")
reset(token_date="skip")
check("no token file", S.token_status()["ok"], False)
yesterday = (S.now().date() - timedelta(days=1)).isoformat()
reset(token_date=yesterday)
check("yesterday's token rejected", S.token_status()["ok"], False)
reset(token_date=S.now().date().isoformat())
check("today's token accepted", S.token_status()["ok"], True)

# --------------------------------------------------------------------------- #
print("\n=== the launch decision ===")
today = S.now().date()
armed = {"armed_date": today.isoformat(), "armed_at": "x",
         "stopped_date": None, "run": None}

reset(state={"armed_date": None, "armed_at": None, "stopped_date": None,
             "run": None}, token_date=today.isoformat())
cfg = S.load_config(); cfg["date"] = today.isoformat(); S.save_config(cfg)
check("not armed -> no launch",
      S.should_launch(S.load_config(), at(today, 9, 30))[1], "not armed for today")

reset(state=armed, token_date="skip")
cfg = S.load_config(); cfg["date"] = today.isoformat(); S.save_config(cfg)
go, why = S.should_launch(S.load_config(), at(today, 9, 30))
check("armed but no token -> waits", go, False)
check("  reason", why.startswith("waiting for login"), True)

reset(state=armed, token_date=today.isoformat())
cfg = S.load_config(); cfg["date"] = today.isoformat(); S.save_config(cfg)
check("before 09:00 -> no launch",
      S.should_launch(S.load_config(), at(today, 8, 30))[1], "before 09:00")
go, why = S.should_launch(S.load_config(), at(today, 9, 30))
check("armed + token + after 09:00 -> LAUNCH", go, True)
check("late login at 11:00 still launches",
      S.should_launch(S.load_config(), at(today, 11, 0))[0], True)
check("past 14:45 -> gives up",
      S.should_launch(S.load_config(), at(today, 15, 0))[0], False)

stopped = {**armed, "stopped_date": today.isoformat()}
reset(state=stopped, token_date=today.isoformat())
cfg = S.load_config(); cfg["date"] = today.isoformat(); S.save_config(cfg)
check("stopped -> no launch",
      S.should_launch(S.load_config(), at(today, 9, 30))[1], "stopped for today")

ran = {**armed, "run": {"date": today.isoformat(), "pid": 999999,
                        "started_at": "x", "log": "x"}}
reset(state=ran, token_date=today.isoformat())
cfg = S.load_config(); cfg["date"] = today.isoformat(); S.save_config(cfg)
check("already launched -> no second launch",
      S.should_launch(S.load_config(), at(today, 10, 0))[1], "already launched today")

reset(state=armed, token_date=today.isoformat())
cfg = S.load_config(); cfg["date"] = "2026-01-01"; S.save_config(cfg)
check("config dated elsewhere -> no launch",
      S.should_launch(S.load_config(), at(today, 9, 30))[0], False)

# --------------------------------------------------------------------------- #
print("\n=== re-arming clears a previous stop ===")
reset(state={"armed_date": "2026-09-17", "armed_at": "x",
             "stopped_date": "2026-09-17", "run": None})
check("stopped first", S.is_armed_for(THU), False)
S.arm(S.load_config(), {"date": "2026-09-17", "side": "SELL",
                        "box_low": 1.0, "box_high": 2.0, "violent_range": 180.0})
check("re-armed", S.is_armed_for(THU), True)

print("\n=== next_trading_day skips the weekend ===")
check("Friday -> Monday",
      S.next_trading_day(date(2026, 9, 18)).isoformat(), "2026-09-21")
check("Thursday -> Friday",
      S.next_trading_day(date(2026, 9, 17)).isoformat(), "2026-09-18")

# --------------------------------------------------------------------------- #
print("\n=== a mail failure must not take the panel down ===")
# Regression: Emailer raises SystemExit when the SMTP password is missing.
# SystemExit is not an Exception, so it escaped the /stop handler and killed the
# whole Flask process - and the scheduler with it.
import os
os.environ.pop("ZERODHA_SMTP_PASSWORD", None)
reset(state={"armed_date": S.now().date().isoformat(), "armed_at": "x",
             "stopped_date": None, "run": None})
import webui
webui.S = S                       # point the app at the temp files
client = webui.app.test_client()
r = client.post("/stop")
check("stop returns 200 even with no SMTP password", r.status_code, 200)
check("the stop still took effect",
      S.load_state()["stopped_date"], S.now().date().isoformat())
body = r.get_data(as_text=True)
check("page reports the mail failure", "could not be sent" in body, True)
check("page confirms the stop worked", "Stopped" in body, True)
check("try_email swallows SystemExit",
      webui.try_email("t", "b") is not None, True)
r = client.get("/status")
check("panel still serving afterwards", r.status_code, 200)

shutil.rmtree(tmp, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all control-panel checks passed")
