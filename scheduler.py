"""Arming state, token handling and the 09:00 launch, shared by the web UI.

Kept separate from webui.py so the rules are testable without a browser.

The day is armed by submitting the form before the cutoff (22:00 by default).
Arming alone does not start anything: at launch time the scheduler also needs a
Kite access token that is valid *today*. Kite flushes every token between 07:30
and 08:30, so last night's token is always dead by morning - the login has to
happen after ~07:35, which is why the UI has a separate one-click login.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
TOKEN = HERE / ".kite_token.json"
LOGS = HERE / "logs"
PYTHON = HERE / "venv" / "bin" / "python"

DEFAULT_UI = {
    "port": 5000,
    "arm_cutoff": "22:00",        # form closes at this time
    "launch_at": "09:00",         # when strategy.py starts
    "wait_for_token_until": "14:45",  # keep waiting for a login until this
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def now() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def load_config() -> dict:
    cfg = json.loads(CONFIG.read_text())
    ui = {**DEFAULT_UI, **cfg.get("ui", {})}
    cfg["ui"] = ui
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def load_state() -> dict:
    if not STATE.exists():
        return {"armed_date": None, "armed_at": None, "stopped_date": None,
                "run": None}
    return json.loads(STATE.read_text())


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2) + "\n")


def next_trading_day(frm: Optional[date] = None) -> date:
    d = (frm or now().date()) + timedelta(days=1)
    while d.weekday() >= 5:            # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #


def token_status() -> dict:
    """Is there a token, and is it for today?

    Only a date check - verifying against Kite costs a network call on every
    status poll. The strategy itself calls profile() at startup and fails loudly
    if the token is actually dead.
    """
    if not TOKEN.exists():
        return {"ok": False, "reason": "no token file", "date": None}
    try:
        data = json.loads(TOKEN.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"unreadable: {exc}", "date": None}
    today = now().date().isoformat()
    if data.get("date") != today:
        return {"ok": False, "date": data.get("date"),
                "reason": f"token is from {data.get('date')}, not {today}"}
    return {"ok": True, "date": data.get("date"), "user": data.get("user_id"),
            "reason": "valid for today"}


# --------------------------------------------------------------------------- #
# arming
# --------------------------------------------------------------------------- #


def arm_window_open(cfg: dict, at: Optional[datetime] = None) -> tuple[bool, str]:
    at = at or now()
    cutoff = parse_hhmm(cfg["ui"]["arm_cutoff"])
    if at.time() > cutoff:
        return False, (f"the form closes at {cfg['ui']['arm_cutoff']}; "
                       f"it is {at:%H:%M}")
    return True, ""


def validate_form(cfg: dict, form: dict, at: Optional[datetime] = None) -> tuple[Optional[dict], list]:
    """Returns (cleaned, errors). Never writes anything."""
    at = at or now()
    errors = []
    clean = {}

    raw_date = (form.get("date") or "").strip()
    try:
        d = date.fromisoformat(raw_date)
        if d < at.date():
            errors.append(f"{d} is in the past")
        elif d.weekday() >= 5:
            errors.append(f"{d:%Y-%m-%d} is a {d:%A} - the market is shut")
        else:
            clean["date"] = d.isoformat()
    except ValueError:
        errors.append("date must be YYYY-MM-DD")

    side = (form.get("side") or "").strip().upper()
    if side not in ("BUY", "SELL"):
        errors.append("side must be BUY or SELL")
    else:
        clean["side"] = side

    nums = {}
    for field in ("box_low", "box_high", "violent_range"):
        raw = (form.get(field) or "").strip()
        try:
            nums[field] = float(raw)
        except ValueError:
            errors.append(f"{field} must be a number")
    if len(nums) == 3:
        if nums["box_low"] >= nums["box_high"]:
            errors.append("box_low must be below box_high")
        if nums["violent_range"] <= 0:
            errors.append("violent_range must be positive")
        clean.update(nums)

    return (clean if not errors else None), errors


def arm(cfg: dict, clean: dict) -> dict:
    """Write the config and record the arming. Caller has already validated."""
    cfg = dict(cfg)
    cfg.update({k: clean[k] for k in
                ("date", "side", "box_low", "box_high", "violent_range")})
    save_config(cfg)
    st = load_state()
    st["armed_date"] = clean["date"]
    st["armed_at"] = now().isoformat()
    if st.get("stopped_date") == clean["date"]:
        st["stopped_date"] = None       # re-arming clears a previous stop
    save_state(st)
    return st


def is_armed_for(day: date, st: Optional[dict] = None) -> bool:
    st = st or load_state()
    return (st.get("armed_date") == day.isoformat()
            and st.get("stopped_date") != day.isoformat())


# --------------------------------------------------------------------------- #
# the strategy process
# --------------------------------------------------------------------------- #


def running_pid(st: Optional[dict] = None) -> Optional[int]:
    st = st or load_state()
    run = st.get("run") or {}
    pid = run.get("pid")
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def launch(cfg: dict) -> dict:
    """Start strategy.py for today, logging to logs/<date>.log."""
    LOGS.mkdir(exist_ok=True)
    today = now().date().isoformat()
    logfile = LOGS / f"{today}.log"
    fh = open(logfile, "a")
    proc = subprocess.Popen(
        [str(PYTHON), str(HERE / "strategy.py")],
        cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    st = load_state()
    st["run"] = {"date": today, "pid": proc.pid,
                 "started_at": now().isoformat(), "log": str(logfile)}
    save_state(st)
    return st["run"]


def kill_running() -> Optional[int]:
    pid = running_pid()
    if pid is None:
        return None
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return None
    return pid


def should_launch(cfg: dict, at: Optional[datetime] = None) -> tuple[bool, str]:
    """The whole launch decision in one place, so it can be tested directly."""
    at = at or now()
    today = at.date()

    if today.weekday() >= 5:
        return False, "weekend"
    st = load_state()
    if not is_armed_for(today, st):
        if st.get("stopped_date") == today.isoformat():
            return False, "stopped for today"
        return False, "not armed for today"

    run = st.get("run") or {}
    if run.get("date") == today.isoformat():
        return False, "already launched today"
    if running_pid(st):
        return False, "a strategy process is already running"

    if at.time() < parse_hhmm(cfg["ui"]["launch_at"]):
        return False, f"before {cfg['ui']['launch_at']}"
    if at.time() > parse_hhmm(cfg["ui"]["wait_for_token_until"]):
        return False, (f"past {cfg['ui']['wait_for_token_until']} with no "
                       "valid token - giving up for today")

    tok = token_status()
    if not tok["ok"]:
        return False, f"waiting for login ({tok['reason']})"

    cfg_date = cfg.get("date")
    if cfg_date != today.isoformat():
        return False, f"config.json is dated {cfg_date}, not {today}"

    return True, "armed, token valid, launching"
