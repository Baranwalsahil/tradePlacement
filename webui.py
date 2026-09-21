#!/usr/bin/env python3
"""Local control panel for the BANKNIFTY alerter.

    ./venv/bin/python webui.py          then open http://127.0.0.1:5000

What it does:
  * evening - fill the form (date, side, box_low, box_high, violent_range) and
    submit before the cutoff. That writes config.json and ARMS the day.
  * morning - one click logs you into Kite. The callback is caught here and
    today's access token is minted. Kite flushes tokens between 07:30 and 08:30,
    so this cannot be done the night before.
  * 09:00   - the scheduler launches strategy.py, but only if the day is armed
    AND a token valid for today exists. If you have not logged in yet it keeps
    waiting and starts the moment you do (the strategy backfills from 09:15, so
    a late start still reconstructs the whole session).
  * STOP    - disarms the day, kills a running strategy, and emails a record.

Binds to 127.0.0.1 only. It can trigger a Kite login and controls whether live
alerts go out, so it must not be exposed to the network.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from flask import Flask, jsonify, redirect, render_template_string, request

import scheduler as S
from strategy import Emailer

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


class KeepAlive:
    """Stop a BaseException in any handler from killing the whole panel.

    Flask turns an Exception into a 500, but SystemExit and friends are not
    Exceptions - they escape the request and terminate the process, which would
    also stop the scheduler. This process has to survive the whole day.
    """

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        try:
            return self.wsgi(environ, start_response)
        except BaseException:  # noqa: BLE001
            note("request crashed:\n" + traceback.format_exc())
            body = b"Something failed, but the panel is still up. Check the log."
            start_response("500 Internal Server Error",
                           [("Content-Type", "text/plain"),
                            ("Content-Length", str(len(body)))])
            return [body]


app.wsgi_app = KeepAlive(app.wsgi_app)

_events: list = []          # recent activity, newest last
_lock = threading.Lock()

_login_api_key: Optional[str] = None   # api_key that built the last login URL
_spent_tokens: list = []               # request_tokens already sent to Kite


def _spend(request_token: str) -> None:
    _spent_tokens.append(request_token)
    del _spent_tokens[:-20]


def note(msg: str) -> None:
    with _lock:
        _events.append(f"{S.now():%H:%M:%S}  {msg}")
        del _events[:-40]
    print(f"{S.now():%H:%M:%S}  {msg}", flush=True)


def try_email(subject: str, body: str) -> Optional[str]:
    """Send mail, but never let a mail problem take the panel down.

    Emailer raises SystemExit when the SMTP password is missing - correct for
    strategy.py at startup, fatal here: it propagates out of the request and
    kills the Flask process, taking the scheduler with it. Catch BaseException,
    not Exception.
    """
    try:
        Emailer(S.load_config()["email"]).send(subject, body)
        return None
    except BaseException as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        note(f"email failed ({subject}): {msg}")
        return msg


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #

PAGE = """<!doctype html>
<title>BANKNIFTY alerter</title>
<style>
 :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fafafa; --card:#fff;
         --line:#e0e0e0; --muted:#666; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; }
 @media (prefers-color-scheme: dark) {
   :root { --fg:#e6e6e6; --bg:#161616; --card:#1f1f1f; --line:#333; --muted:#999;
           --ok:#3fb950; --warn:#d29922; --bad:#f85149; } }
 body { font:14px/1.5 system-ui,sans-serif; margin:0; padding:24px;
        background:var(--bg); color:var(--fg); }
 .wrap { max-width:760px; margin:0 auto; }
 h1 { font-size:20px; margin:0 0 4px; }
 .sub { color:var(--muted); margin:0 0 20px; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:18px; margin-bottom:16px; }
 .row { display:flex; gap:12px; flex-wrap:wrap; }
 .row > div { flex:1 1 150px; }
 label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
 input, select { width:100%; padding:8px 10px; font:inherit; box-sizing:border-box;
        border:1px solid var(--line); border-radius:6px;
        background:var(--bg); color:var(--fg); }
 button { font:inherit; padding:9px 18px; border-radius:6px; cursor:pointer;
          border:1px solid var(--line); background:var(--card); color:var(--fg); }
 button.primary { background:#1f6feb; border-color:#1f6feb; color:#fff; }
 button.danger  { background:var(--bad); border-color:var(--bad); color:#fff; }
 button:disabled { opacity:.45; cursor:not-allowed; }
 .pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:12px;
         font-weight:600; }
 .pill.ok{background:var(--ok);color:#fff} .pill.warn{background:var(--warn);color:#fff}
 .pill.bad{background:var(--bad);color:#fff} .pill.idle{background:var(--line);color:var(--fg)}
 table { width:100%; border-collapse:collapse; font-size:13px; }
 td { padding:4px 0; } td:first-child { color:var(--muted); width:42%; }
 .err { color:var(--bad); margin:8px 0 0; }
 .msg { color:var(--ok); margin:8px 0 0; }
 pre { background:var(--bg); border:1px solid var(--line); border-radius:6px;
       padding:10px; overflow-x:auto; font-size:12px; margin:0; max-height:220px; }
 .muted { color:var(--muted); font-size:12px; }
</style>
<div class="wrap">
<h1>BANKNIFTY alerter</h1>
<p class="sub">{{ nowstr }} IST &middot; alert-only, no orders are ever placed</p>

<div class="card">
  <table>
   <tr><td>Day armed for</td><td>
     {% if st.armed_date %}<span class="pill {{ 'ok' if armed_live else 'idle' }}">
       {{ st.armed_date }}</span>
       {% if stopped %}<span class="pill bad">STOPPED</span>{% endif %}
     {% else %}<span class="pill idle">not armed</span>{% endif %}</td></tr>
   <tr><td>Kite token</td><td>
     {% if tok.ok %}<span class="pill ok">valid today</span> {{ tok.user or '' }}
     {% else %}<span class="pill warn">{{ tok.reason }}</span>{% endif %}</td></tr>
   <tr><td>Strategy process</td><td>
     {% if pid %}<span class="pill ok">running</span> pid {{ pid }}
     {% else %}<span class="pill idle">not running</span>{% endif %}</td></tr>
   <tr><td>Scheduler</td><td>{{ launch_reason }}</td></tr>
   <tr><td>Form window</td><td>
     {% if window_open %}open until {{ cfg.ui.arm_cutoff }}
     {% else %}<span class="pill warn">closed</span> {{ window_why }}{% endif %}</td></tr>
  </table>
</div>

<div class="card">
  <form method="post" action="/arm">
    <div class="row">
      <div><label>Date</label><input name="date" value="{{ form.date }}"
           placeholder="YYYY-MM-DD"></div>
      <div><label>Side</label><select name="side">
        <option {{ 'selected' if form.side=='BUY' else '' }}>BUY</option>
        <option {{ 'selected' if form.side=='SELL' else '' }}>SELL</option>
      </select></div>
    </div>
    <div class="row" style="margin-top:12px">
      <div><label>Box low</label><input name="box_low" value="{{ form.box_low }}"></div>
      <div><label>Box high</label><input name="box_high" value="{{ form.box_high }}"></div>
      <div><label>Violent range</label>
           <input name="violent_range" value="{{ form.violent_range }}"></div>
    </div>
    <p style="margin:16px 0 0">
      <button class="primary" type="submit" {{ '' if window_open else 'disabled' }}>
        Submit &amp; arm</button>
    </p>
    {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}
    {% if message %}<p class="msg">{{ message }}</p>{% endif %}
  </form>
  <p class="muted" style="margin-top:14px">
    No-trade zone will be
    {{ '%.2f'|format(form.box_low|float - cfg.breakout_buffer) }} –
    {{ '%.2f'|format(form.box_high|float + cfg.breakout_buffer) }}
    (buffer {{ cfg.breakout_buffer }} both edges).
    Any candle touching it ends the day.
  </p>
</div>

<div class="card">
  <div class="row">
    <div><form method="get" action="/login">
      <button class="primary" type="submit" {{ 'disabled' if tok.ok else '' }}>
        {{ 'Logged in' if tok.ok else 'Log in to Kite' }}</button></form></div>
    <div><form method="post" action="/stop"
          onsubmit="return confirm('Disarm {{ st.armed_date or 'today' }}, kill any running strategy, and email a record?')">
      <button class="danger" type="submit"
        {{ '' if (armed_live or pid) else 'disabled' }}>Stop</button></form></div>
  </div>
  <p class="muted" style="margin-top:12px">
    Kite clears every access token between 07:30 and 08:30, so last night's login
    is always dead by morning. Log in after 07:35 on the day you want to trade.
  </p>
  <details style="margin-top:10px"><summary class="muted">Paste a request_token
    manually</summary>
    <form method="post" action="/manual-token" style="margin-top:10px">
      <input name="request_token" placeholder="request_token, or the whole redirect URL">
      <p style="margin:10px 0 0"><button type="submit">Exchange</button></p>
    </form>
    <p class="muted">Use this if the Kite app's Redirect URL is not pointed at
      <code>{{ callback_url }}</code>.</p>
  </details>
</div>

<div class="card">
  <table>
    <tr><td>Instrument</td><td>{{ cfg.index_symbol }}</td></tr>
    <tr><td>Candle</td><td>{{ cfg.candle_minutes }} min</td></tr>
    <tr><td>Buffer</td><td>{{ cfg.breakout_buffer }} pts, both edges</td></tr>
    <tr><td>No new entry after</td><td>{{ cfg.no_new_entry_after }}</td></tr>
    <tr><td>Square-off alert</td><td>{{ cfg.squareoff_alert_at }}</td></tr>
    <tr><td>Launch at</td><td>{{ cfg.ui.launch_at }}, waits for a token until
        {{ cfg.ui.wait_for_token_until }}</td></tr>
    <tr><td>Alerts to</td><td>{{ cfg.email.to|join(', ') }}</td></tr>
  </table>
  <p class="muted" style="margin-top:10px">These live in config.json.</p>
</div>

<div class="card">
  <pre>{{ events }}</pre>
</div>
</div>
{% if autoreload %}<script>setTimeout(() => location.reload(), 20000);</script>{% endif %}
"""


def render(message: str = "", errors: Optional[list] = None,
           form: Optional[dict] = None):
    cfg = S.load_config()
    st = S.load_state()
    today = S.now().date()
    win_ok, win_why = S.arm_window_open(cfg)
    _, reason = S.should_launch(cfg)
    defaults = {
        "date": cfg.get("date") or S.next_trading_day().isoformat(),
        "side": cfg.get("side", "BUY"),
        "box_low": cfg.get("box_low", ""),
        "box_high": cfg.get("box_high", ""),
        "violent_range": cfg.get("violent_range", 180),
    }
    with _lock:
        events = "\n".join(_events[-18:]) or "(nothing yet)"
    return render_template_string(
        PAGE,
        cfg=cfg, st=st, tok=S.token_status(), pid=S.running_pid(),
        armed_live=S.is_armed_for(today, st) or (
            st.get("armed_date") and st["armed_date"] >= today.isoformat()
            and st.get("stopped_date") != st.get("armed_date")),
        stopped=st.get("stopped_date") == st.get("armed_date")
        and st.get("armed_date") is not None,
        window_open=win_ok, window_why=win_why,
        launch_reason=reason, nowstr=f"{S.now():%a %d %b %Y, %H:%M}",
        form={**defaults, **(form or {})},
        errors=errors or [], message=message,
        callback_url=callback_url(), events=events,
        # Reloading a page whose request was a POST resubmits that POST. The
        # timed reload below re-armed the day every 20 seconds because of it,
        # so only a GET gets the script.
        autoreload=request.method == "GET",
    )


def home(message: str = "", errors: Optional[list] = None):
    """Post/Redirect/Get: land on a plain GET of / carrying the outcome.

    Keeps the 20s auto-reload harmless - it re-runs a GET, never the action.
    """
    params = [("msg", message)] if message else []
    params += [("err", e) for e in errors or []]
    return redirect("/?" + urlencode(params) if params else "/")


def callback_url() -> str:
    port = S.load_config()["ui"]["port"]
    return f"http://127.0.0.1:{port}/callback"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return render(message=request.args.get("msg", ""),
                  errors=request.args.getlist("err"))


@app.post("/arm")
def arm():
    cfg = S.load_config()
    ok, why = S.arm_window_open(cfg)
    if not ok:
        return render(errors=[f"Cannot arm: {why}."], form=request.form.to_dict())
    clean, errors = S.validate_form(cfg, request.form.to_dict())
    if errors:
        return render(errors=errors, form=request.form.to_dict())
    S.arm(cfg, clean)
    note(f"armed {clean['date']} {clean['side']} "
         f"box {clean['box_low']}-{clean['box_high']} "
         f"violent>{clean['violent_range']}")
    return home(f"Armed for {clean['date']}. "
                f"Log in to Kite after 07:35 that morning.")


@app.post("/stop")
def stop():
    st = S.load_state()
    target = st.get("armed_date") or S.now().date().isoformat()
    killed = S.kill_running()
    st = S.load_state()
    st["stopped_date"] = target
    S.save_state(st)
    note(f"STOPPED {target}" + (f", killed pid {killed}" if killed else ""))
    mail_err = try_email(
        "STOPPED - trading disarmed",
        f"{S.now():%Y-%m-%d %H:%M} IST\n\n"
        f"{target} has been disarmed from the control panel; strategy.py "
        f"will not launch.\n"
        + (f"A running strategy (pid {killed}) was terminated. If a position "
           "was open, nothing is watching it now - manage it yourself.\n"
           if killed else "No strategy process was running.\n"))
    msg = f"Stopped {target}." + (f" Killed pid {killed}." if killed else "")
    if mail_err:
        return home(msg, [f"The stop took effect, but the confirmation "
                          f"email could not be sent - {mail_err}"])
    return home(msg + " Confirmation emailed.")


@app.get("/login")
def login():
    from kiteconnect import KiteConnect
    global _login_api_key
    api_key = (os.environ.get("KITE_API_KEY") or "").strip()
    if not api_key:
        return render(errors=["KITE_API_KEY is not set in this shell."])
    _login_api_key = api_key
    note(f"opening Kite login with api_key ...{api_key[-4:]}")
    return redirect(KiteConnect(api_key=api_key).login_url())


@app.get("/callback")
def callback():
    rt = request.args.get("request_token")
    if not rt:
        return home(errors=[f"Kite redirected here without a request_token "
                            f"({dict(request.args)})."])
    ok, msg = exchange(rt)
    return home(msg) if ok else home(errors=[msg])


@app.post("/manual-token")
def manual_token():
    raw = (request.form.get("request_token") or "").strip()
    if "request_token=" in raw:
        q = urlparse(raw).query or raw.split("?", 1)[-1]
        raw = (parse_qs(q).get("request_token") or [raw])[0]
    if not raw:
        return render(errors=["Nothing pasted."])
    ok, msg = exchange(raw)
    return home(msg) if ok else home(errors=[msg])


def exchange(request_token: str) -> tuple[bool, str]:
    """request_token -> access_token, cached for strategy.py.

    Kite answers a bad checksum with the same "Token is invalid or has expired"
    it uses for a stale token, so a wrong or whitespace-padded api_secret looks
    exactly like a slow login. Strip the env values and say which causes are
    still on the table rather than blaming the token.
    """
    import json
    import stat

    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import TokenException

    api_key = (os.environ.get("KITE_API_KEY") or "").strip()
    api_secret = (os.environ.get("KITE_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        return False, "KITE_API_KEY / KITE_API_SECRET are not set in this shell."
    if request_token in _spent_tokens:
        return False, ("That request_token has already been exchanged once. "
                       "They are single-use - click Log in to Kite for a fresh "
                       "one instead of reloading this page.")

    kite = KiteConnect(api_key=api_key)
    try:
        sess = kite.generate_session(request_token, api_secret=api_secret)
    except TokenException as exc:
        _spend(request_token)   # Kite saw it, so it is burnt either way
        causes = ["it was already used - one exchange per login",
                  "it is more than a few minutes old",
                  f"KITE_API_SECRET in this process does not match the secret "
                  f"of app ...{api_key[-4:]} in the Kite developer console"]
        if _login_api_key and _login_api_key != api_key:
            causes.append(f"the login URL was built with api_key "
                          f"...{_login_api_key[-4:]} but this process exchanges "
                          f"with ...{api_key[-4:]}")
        note(f"token exchange failed (api_key ...{api_key[-4:]}): {exc}")
        return False, (f"Token exchange failed: {exc}. One of these: "
                       + "; ".join(causes) + ".")
    except Exception as exc:  # noqa: BLE001 - network, DNS, Kite outage
        note(f"token exchange error (api_key ...{api_key[-4:]}): {exc}")
        return False, (f"Could not reach Kite to exchange the token: {exc}. "
                       "The token may still be good - retry within a minute or "
                       "two, otherwise log in again.")
    _spend(request_token)
    S.TOKEN.write_text(json.dumps({
        "access_token": sess["access_token"], "api_key": api_key,
        "date": S.now().date().isoformat(), "user_id": sess.get("user_id"),
    }, indent=2))
    S.TOKEN.chmod(stat.S_IRUSR | stat.S_IWUSR)
    note(f"token minted for {sess.get('user_id')}")
    return True, f"Logged in as {sess.get('user_id')}. Token valid for today."


@app.get("/status")
def status():
    cfg = S.load_config()
    go, reason = S.should_launch(cfg)
    return jsonify({"now": S.now().isoformat(), "state": S.load_state(),
                    "token": S.token_status(), "pid": S.running_pid(),
                    "would_launch": go, "reason": reason})


@app.get("/healthz")
def healthz():
    """Cheap liveness probe. No Kite calls, no disk writes, no side effects.

    Safe to hit every few minutes from an uptime pinger to stop a free-tier
    host from spinning the instance down.
    """
    return jsonify({"ok": True, "now": S.now().isoformat(),
                    "pid_running": S.running_pid() is not None}), 200


# --------------------------------------------------------------------------- #
# scheduler thread
# --------------------------------------------------------------------------- #


def scheduler_loop(interval: int = 20) -> None:
    last_reason = None
    while True:
        try:
            cfg = S.load_config()
            go, reason = S.should_launch(cfg)
            if go:
                run = S.launch(cfg)
                note(f"launched strategy.py pid {run['pid']} -> {run['log']}")
            elif reason != last_reason and reason.startswith("waiting for login"):
                note(reason)
            last_reason = reason
        except Exception:  # noqa: BLE001
            note("scheduler error:\n" + traceback.format_exc())
        time.sleep(interval)


def self_ping_loop(url: str, interval: int = 600) -> None:
    """Hit our own public /healthz so the host sees inbound traffic.

    Only useful on hosts that sleep an idle instance. It cannot wake a process
    that is already asleep - pair it with an external pinger for that.
    """
    import urllib.error
    import urllib.request

    last_err = None
    while True:
        time.sleep(interval)
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                r.read(64)
            last_err = None
        except Exception as exc:  # noqa: BLE001
            msg = f"self-ping failed: {exc}"
            if msg != last_err:
                note(msg)
                last_err = msg


def main() -> int:
    cfg = S.load_config()
    port = int(cfg["ui"]["port"])
    for var in ("KITE_API_KEY", "KITE_API_SECRET"):
        if not os.environ.get(var):
            print(f"warning: {var} is not set - login will fail", file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # PORT/HOST are set by PaaS hosts (Render, Fly, Heroku). Locally we keep
    # binding to loopback only - this panel can mint Kite tokens and arm live
    # alerts, so it must not be reachable from the network by accident.
    port = int(os.environ.get("PORT") or port)
    host = os.environ.get("HOST", "127.0.0.1")

    keepalive = os.environ.get("KEEPALIVE_URL")
    if keepalive:
        every = int(os.environ.get("KEEPALIVE_INTERVAL", "600"))
        threading.Thread(target=self_ping_loop, args=(keepalive, every),
                         daemon=True).start()
        note(f"self-ping every {every}s -> {keepalive}")

    note(f"control panel on http://{host}:{port}")
    note(f"Kite Redirect URL must be exactly {callback_url()}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
