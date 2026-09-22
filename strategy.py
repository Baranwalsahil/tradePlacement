#!/usr/bin/env python3
"""BANKNIFTY box-breakout alerter.

ALERT ONLY. This script never places, modifies or cancels an order. It reads
5-minute candles from Kite's historical endpoint, runs the setup over them, and
emails you when it fires. You place every trade yourself.

Decision flow (see README.md for the full table):

The box is a NO-TRADE ZONE. Widened by the buffer on both edges, a candle counts
as inside if any part of its range overlaps it - a wick is enough. Nothing
happens while price is in there.

Direction is NOT part of the box test. The box only asks "are we outside it?".
BUY vs SELL comes from the manual flag, and the entry conditions are
VWAP-relative - so price trading BELOW the box is a perfectly good BUY day, as
long as a candle closes above VWAP.

    09:15-09:20 candle range > 180 pts   -> too violent, stand down for the day
    ANY candle touching the zone         -> day is dead, unconditionally. That
                                            includes a session that simply opens
                                            inside the zone. Every candle from
                                            09:15 on must be entirely clear.
    C1 = first clear candle closing on the right side of VWAP
                                            BUY: close > VWAP, SELL: close < VWAP
        wrong side of VWAP               -> skip it, keep hunting
    C2 = first later clear candle closing beyond C1's extreme
                                            BUY: close > C1.high, SELL: < C1.low
        any candle closing back
        through VWAP                     -> C1 invalidated, hunt a fresh C1
        any candle touching the zone     -> day is dead
    C2 found                             -> ENTER alert, entry = C2 close
    target = entry +/- (High(C2) - Low(C1))     [BUY: +, SELL: -]
    stop   = L1 closes past VWAP, then a later candle closes past L1's extreme
    also   = any candle touching the zone closes the position immediately

Data note: the BANKNIFTY index carries no volume, so OHLC comes from the index
while the volume weighting VWAP is taken from the nearest-expiry BANKNIFTY
future. Both come from the historical endpoint, polled ~30s after each candle
closes - the websocket sends throttled snapshots (~1/sec) that miss the opening
print and intra-second extremes, which put the open out by 32 pts and a high by
48 pts on measured days. Ticks are used only for the live LTP target check.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import queue
import smtplib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from enum import Enum
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect, KiteTicker

IST = ZoneInfo("Asia/Kolkata")
SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 30)

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".kite_token.json"

# Feed watchdog. A dead websocket is silent - the socket can sit in CLOSE-WAIT
# with no on_close callback, and the strategy then makes no decisions at all
# while looking perfectly healthy. These are seconds without a single tick.
STALE_WARN = 90
STALE_FORCE_RECONNECT = 150
STALE_FATAL = 420
HEARTBEAT_EVERY = 300

# Candles are polled from the historical endpoint. Measured on 2026-09-03, Kite
# published a just-closed candle within 2s on both the index and the futures
# leg, so the wait is short. A miss is harmless - the poll simply retries - so
# there is no reason to pad this. Every second here is a second of entry drift.
POLL_DELAY = 8
POLL_RETRY = 5

# Kite's own HTTP default is 7s, which is tight for a historical request and
# produced a read timeout that killed a live run on 2026-09-04.
HTTP_TIMEOUT = 20

# A transient fetch failure must not end the session. Retries back off from
# POLL_RETRY up to FETCH_BACKOFF_MAX; a warning email goes out once the failures
# have persisted for FETCH_WARN_AFTER, and the run only gives up - loudly - at
# FETCH_FATAL_AFTER.
FETCH_BACKOFF_MAX = 60
FETCH_WARN_AFTER = 60
FETCH_FATAL_AFTER = 300

log = logging.getLogger("bnf")


# --------------------------------------------------------------------------- #
# candles
# --------------------------------------------------------------------------- #


@dataclass
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def range(self) -> float:
        return self.high - self.low

    def __str__(self) -> str:
        return (
            f"{self.start:%H:%M} O={self.open:.2f} H={self.high:.2f} "
            f"L={self.low:.2f} C={self.close:.2f} V={self.volume} "
            f"range={self.range:.1f}"
        )


class SessionVWAP:
    """cum_pv += ((H+L+C)/3) * volume ; cum_vol += volume ; vwap = cum_pv/cum_vol"""

    def __init__(self) -> None:
        self.cum_pv = 0.0
        self.cum_vol = 0

    def update(self, bar: Bar) -> None:
        if bar.volume <= 0:
            return
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.cum_pv += typical * bar.volume
        self.cum_vol += bar.volume

    @property
    def value(self) -> Optional[float]:
        return self.cum_pv / self.cum_vol if self.cum_vol else None


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #


SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


class Emailer:
    """Alert mail over SMTP, or over the SendGrid HTTPS API.

    Render blocks outbound port 25 on every instance type and also 465/587 on
    the free ones, so SMTP cannot leave the container there at all. Setting
    "transport": "http" in the email config sends over port 443 instead, which
    no PaaS blocks.
    """

    def __init__(self, cfg: dict, enabled: bool = True) -> None:
        self.transport = cfg.get("transport", "smtp")
        self.to = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]
        self.enabled = enabled

        if self.transport == "http":
            self.sender = cfg["from"]
            self.api_url = cfg.get("api_url", SENDGRID_URL)
            env_name = cfg.get("api_key_env", "ZERODHA_SENDGRID_KEY")
            self.api_key = os.environ.get(env_name)
            if enabled and not self.api_key:
                raise SystemExit(
                    f"SendGrid API key not found. Set {env_name} in the "
                    "environment (the key is shown once, at creation time)."
                )
            return

        self.host = cfg["smtp_host"]
        self.port = int(cfg["smtp_port"])
        self.user = cfg["user"]
        self.sender = cfg.get("from", cfg["user"])
        env_name = cfg.get("password_env", "ZERODHA_SMTP_PASSWORD")
        self.password = os.environ.get(env_name)
        if enabled and not self.password:
            raise SystemExit(
                f"SMTP password not found. Set {env_name} in the environment "
                "(a Google app password, not your account password)."
            )

    def send(self, subject: str, body: str) -> None:
        line = f"[BNF] {subject}"
        log.info("ALERT %s :: %s", subject, body.replace("\n", " | "))
        if not self.enabled:
            return
        if self.transport == "http":
            self._send_http(line, body)
            return
        self._send_smtp(line, body)

    def _send_http(self, line: str, body: str) -> None:
        """POST the mail to SendGrid. A dead mail path must not kill the run."""
        payload = json.dumps({
            "personalizations": [{"to": [{"email": addr} for addr in self.to]}],
            "from": {"email": self.sender},
            "subject": line,
            "content": [{"type": "text/plain", "value": body}],
        }).encode()
        req = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
                # SendGrid answers 202 and nothing else, so without this line a
                # send that never reaches an inbox is indistinguishable from one
                # that was never attempted.
                log.info("mail accepted: HTTP %s id=%s", resp.status,
                         resp.headers.get("X-Message-Id", "?"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            log.error("email failed (%s): HTTP %s %s", line, exc.code, detail)
        except Exception as exc:  # noqa: BLE001 - see _send_smtp
            log.error("email failed (%s): %s", line, exc)

    def _send_smtp(self, line: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = line
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg.set_content(body)
        try:
            with self._connect() as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except OSError as exc:
            hint = ""
            if getattr(exc, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH,
                                               errno.ECONNREFUSED, errno.ETIMEDOUT):
                hint = (f" - the TCP connection to {self.host}:{self.port} never "
                        "opened. Gmail and the password are not involved yet; "
                        "the host is refusing or has no route for outbound SMTP. "
                        "Many PaaS providers block ports 25/465/587, in which "
                        "case mail has to go out over an HTTPS email API instead.")
            log.error("email failed (%s): %s%s", line, exc, hint)
        except Exception as exc:  # noqa: BLE001 - a dead mail server must not kill the run
            log.error("email failed (%s): %s", line, exc)

    def _connect(self) -> smtplib.SMTP:
        """Open the SMTP connection, preferring IPv4.

        A container can be handed an AAAA record for the mail host while having
        no IPv6 route at all, and connect() then fails with ENETUNREACH before
        anything reaches Gmail. Resolving the A records ourselves keeps it on
        IPv4; _host is restored afterwards so STARTTLS still validates the
        certificate against the real hostname rather than the literal IP.
        """
        try:
            addrs = socket.getaddrinfo(self.host, self.port, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except OSError:
            addrs = []
        last: Optional[OSError] = None
        for _, _, _, _, sockaddr in addrs:
            try:
                smtp = smtplib.SMTP(sockaddr[0], self.port, timeout=30)
            except OSError as exc:
                last = exc
                continue
            smtp._host = self.host
            return smtp
        if last is not None:
            raise last
        return smtplib.SMTP(self.host, self.port, timeout=30)


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class State(str, Enum):
    FIRST_BAR = "FIRST_BAR"
    HUNT_C1 = "HUNT_C1"
    HUNT_C2 = "HUNT_C2"
    IN_TRADE = "IN_TRADE"
    DONE = "DONE"


@dataclass
class Strategy:
    cfg: dict
    notify: Emailer

    state: State = State.FIRST_BAR
    c1: Optional[Bar] = None
    c2: Optional[Bar] = None
    l1: Optional[Bar] = None
    entry: Optional[float] = None
    target: Optional[float] = None
    bars: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.side = Side(self.cfg["side"].upper())
        self.box_low = float(self.cfg["box_low"])
        self.box_high = float(self.cfg["box_high"])
        self.buffer = float(self.cfg["breakout_buffer"])
        self.violent = float(self.cfg["violent_range"])
        self.first_bar_eligible = bool(self.cfg.get("first_bar_eligible_as_c1", True))
        self.cutoff = _parse_time(self.cfg["no_new_entry_after"])
        self.squareoff = _parse_time(self.cfg["squareoff_alert_at"])
        # The no-trade zone is the box widened by the buffer on BOTH edges. A
        # candle counts as inside if any part of its range overlaps the zone -
        # a wick is enough.
        self.zone_low = self.box_low - self.buffer
        self.zone_high = self.box_high + self.buffer

    # -- zone geometry ---------------------------------------------------- #

    def clear_above(self, bar: Bar) -> bool:
        return bar.low > self.zone_high

    def clear_below(self, bar: Bar) -> bool:
        return bar.high < self.zone_low

    def touches_zone(self, bar: Bar) -> bool:
        return not (self.clear_above(bar) or self.clear_below(bar))

    # NOTE: there is deliberately no "clear on the flagged side" test. The box
    # asks only whether price is outside it at all - direction is not part of
    # it. BUY vs SELL is decided by the flag, and the entry conditions are
    # VWAP-relative. Price trading below the box is a perfectly good BUY day.

    # -- bar-driven ------------------------------------------------------- #

    def on_bar(self, bar: Bar, vwap: Optional[float]) -> None:
        self.bars.append(bar)
        log.info("bar %s vwap=%s state=%s", bar, _fmt(vwap), self.state.value)

        if self.state is State.DONE:
            return
        if self.state is State.FIRST_BAR:
            self._first_bar(bar, vwap)
        elif self.state is State.HUNT_C1:
            self._hunt_c1(bar, vwap)
        elif self.state is State.HUNT_C2:
            self._hunt_c2(bar, vwap)
        elif self.state is State.IN_TRADE:
            self._manage(bar, vwap)

    def _first_bar(self, bar: Bar, vwap: Optional[float]) -> None:
        if bar.range > self.violent:
            self._finish(
                "NO TRADE TODAY - too violent",
                f"Opening candle {bar.start:%H:%M}-{bar.end:%H:%M} range "
                f"{bar.range:.1f} pts > {self.violent:.0f}.\n"
                f"{bar}\n\nStanding down for the day.",
            )
            return
        log.info("opening candle range %.1f <= %.0f, day is live", bar.range, self.violent)
        self.state = State.HUNT_C1
        if self.first_bar_eligible:
            self._hunt_c1(bar, vwap)
        elif self.touches_zone(bar):
            # The zone test applies to the opening candle whether or not it is
            # allowed to be C1 itself.
            self._back_in_the_box(bar)

    def _back_in_the_box(self, bar: Bar) -> None:
        where = (
            "opened inside" if bar.start.time() == SESSION_START else "touched"
        )
        self._finish(
            "NO TRADE TODAY - price in the box",
            "\n".join(
                [
                    f"{bar.start:%H:%M} {where} the no-trade zone "
                    f"{self.zone_low:.2f} - {self.zone_high:.2f}.",
                    "",
                    f"{bar}",
                    "",
                    "Any contact with the zone ends the day. Standing down.",
                ]
            ),
        )

    def _hunt_c1(self, bar: Bar, vwap: Optional[float]) -> None:
        # Any contact with the zone ends the day, unconditionally - including a
        # session that simply opens inside it. There is no "wait for price to
        # leave the box" state.
        if self.touches_zone(bar):
            self._back_in_the_box(bar)
            return

        if vwap is None:
            log.warning("breakout at %s but VWAP has no volume yet - skipping", bar.start)
            return

        vwap_ok = bar.close > vwap if self.side is Side.BUY else bar.close < vwap
        if not vwap_ok:
            log.info(
                "%s cleared the zone (close %.2f) but is on the wrong side of "
                "VWAP %.2f - skipping",
                f"{bar.start:%H:%M}",
                bar.close,
                vwap,
            )
            return

        self.c1 = bar
        self.state = State.HUNT_C2
        log.info("C1 set at %s: %s (vwap %.2f)", f"{bar.start:%H:%M}", bar, vwap)

    def _hunt_c2(self, bar: Bar, vwap: Optional[float]) -> None:
        if self.touches_zone(bar):
            self._back_in_the_box(bar)
            return

        if vwap is None:
            return

        # A close back through VWAP kills this C1; the day carries on.
        killed = bar.close < vwap if self.side is Side.BUY else bar.close > vwap
        if killed:
            log.info(
                "C1 (%s) invalidated - %s closed %.2f through VWAP %.2f; hunting a fresh C1",
                f"{self.c1.start:%H:%M}",
                f"{bar.start:%H:%M}",
                bar.close,
                vwap,
            )
            self.c1 = None
            self.state = State.HUNT_C1
            self._hunt_c1(bar, vwap)
            return

        confirmed = (
            bar.close > self.c1.high
            if self.side is Side.BUY
            else bar.close < self.c1.low
        )
        if confirmed:
            self._enter(bar, vwap)

    def _enter(self, bar: Bar, vwap: float) -> None:
        self.c2 = bar
        self.entry = bar.close
        if self.side is Side.BUY:
            distance = bar.high - self.c1.low
            self.target = self.entry + distance
        else:
            distance = self.c1.high - bar.low
            self.target = self.entry - distance

        self.l1 = None
        self.state = State.IN_TRADE
        self.notify.send(
            f"ENTER {self.side.value} @ {self.entry:.2f}",
            "\n".join(
                [
                    f"ENTER {self.side.value}  BANKNIFTY",
                    "",
                    f"Entry (C2 close) : {self.entry:.2f}",
                    f"Target           : {self.target:.2f}   (+{distance:.1f} pts)",
                    f"VWAP now         : {vwap:.2f}",
                    "",
                    f"Box              : {self.box_low:.0f} - {self.box_high:.0f}",
                    f"No-trade zone    : {self.zone_low:.0f} - {self.zone_high:.0f} "
                    f"(buffer {self.buffer:.0f} both edges)",
                    f"C1  {self.c1}",
                    f"C2  {bar}",
                    "",
                    "Stop rule: a candle closing back through VWAP arms L1; a later",
                    "candle closing past L1's extreme is the exit. You will get an",
                    "email at each step. Square-off reminder at "
                    f"{self.squareoff:%H:%M}.",
                ]
            ),
        )

    def _manage(self, bar: Bar, vwap: Optional[float]) -> None:
        # Price coming back to the box closes the trade, whatever the target and
        # the L1/L2 stop are doing.
        if self.touches_zone(bar):
            pnl = (
                bar.close - self.entry
                if self.side is Side.BUY
                else self.entry - bar.close
            )
            self._finish(
                f"CLOSE {self.side.value} - price back in the box",
                "\n".join(
                    [
                        f"{bar.start:%H:%M} touched the no-trade zone "
                        f"{self.zone_low:.2f} - {self.zone_high:.2f}.",
                        "",
                        f"{bar}",
                        "",
                        f"Entry {self.entry:.2f} -> {bar.close:.2f} = {pnl:+.1f} pts.",
                        f"Target {self.target:.2f} was not reached. Close the "
                        "position now.",
                    ]
                ),
            )
            return

        if vwap is None:
            return

        past_vwap = bar.close < vwap if self.side is Side.BUY else bar.close > vwap

        if self.l1 is None:
            if past_vwap:
                self.l1 = bar
                edge = bar.low if self.side is Side.BUY else bar.high
                self.notify.send(
                    "STOP WARNING - L1 armed",
                    "\n".join(
                        [
                            f"{bar.start:%H:%M} closed {bar.close:.2f}, through VWAP {vwap:.2f}.",
                            f"L1 armed. Exit if a later candle closes past {edge:.2f}.",
                            "",
                            f"{bar}",
                            f"Entry {self.entry:.2f}  Target {self.target:.2f}",
                        ]
                    ),
                )
            return

        # L1 is armed.
        if not past_vwap:
            self.l1 = None
            self.notify.send(
                "STOP WARNING CLEARED",
                f"{bar.start:%H:%M} closed {bar.close:.2f}, back on the right side of "
                f"VWAP {vwap:.2f}. L1 disarmed, position stands.\n\n{bar}",
            )
            return

        broke_l1 = (
            bar.close < self.l1.low
            if self.side is Side.BUY
            else bar.close > self.l1.high
        )
        if broke_l1:
            edge = self.l1.low if self.side is Side.BUY else self.l1.high
            pnl = (
                bar.close - self.entry
                if self.side is Side.BUY
                else self.entry - bar.close
            )
            self._finish(
                f"STOP - exit {self.side.value} now",
                "\n".join(
                    [
                        f"{bar.start:%H:%M} closed {bar.close:.2f}, past L1's "
                        f"{'low' if self.side is Side.BUY else 'high'} {edge:.2f}.",
                        "",
                        f"L1  {self.l1}",
                        f"L2  {bar}",
                        "",
                        f"Entry {self.entry:.2f} -> {bar.close:.2f} = {pnl:+.1f} pts.",
                    ]
                ),
            )

    # -- tick-driven ------------------------------------------------------ #

    def on_price(self, ts: datetime, ltp: float) -> None:
        if self.state is not State.IN_TRADE or self.target is None:
            return
        hit = ltp >= self.target if self.side is Side.BUY else ltp <= self.target
        if hit:
            pnl = (
                ltp - self.entry if self.side is Side.BUY else self.entry - ltp
            )
            self._finish(
                "TARGET HIT - book it",
                "\n".join(
                    [
                        f"{ts:%H:%M:%S} LTP {ltp:.2f} reached target {self.target:.2f}.",
                        "",
                        f"Entry {self.entry:.2f} -> {ltp:.2f} = {pnl:+.1f} pts.",
                    ]
                ),
            )

    # -- clock-driven ----------------------------------------------------- #

    def on_clock(self, now: datetime) -> None:
        if self.state is State.DONE:
            return
        t = now.time()

        if self.state is State.IN_TRADE and t >= self.squareoff:
            self._finish(
                "SQUARE OFF - session ending",
                f"{t:%H:%M} and the trade is still open. Neither target "
                f"({self.target:.2f}) nor the L1/L2 stop fired.\n\n"
                f"Entry {self.entry:.2f}. Close it out.",
            )
            return

        if self.state in (State.FIRST_BAR, State.HUNT_C1, State.HUNT_C2) and t >= self.cutoff:
            self._finish(
                "NO TRADE TODAY - cutoff reached",
                f"No entry by {self.cutoff:%H:%M}. Last state was {self.state.value}"
                + (f", C1 was {self.c1.start:%H:%M}." if self.c1 else "."),
            )

    def _finish(self, subject: str, body: str) -> None:
        self.state = State.DONE
        self.notify.send(subject, body)


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


def _fmt(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "n/a"


def _parse_time(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def load_config(path: Path, ignore_date: bool) -> dict:
    cfg = json.loads(path.read_text())
    today = datetime.now(IST).date()
    cfg_date = date.fromisoformat(cfg["date"])
    if cfg_date != today and not ignore_date:
        raise SystemExit(
            f"{path.name} is dated {cfg_date} but today is {today}. Update the box "
            "for today, or pass --ignore-date if you really mean to reuse it."
        )
    if cfg["side"].upper() not in ("BUY", "SELL"):
        raise SystemExit("side must be BUY or SELL")
    if float(cfg["box_low"]) >= float(cfg["box_high"]):
        raise SystemExit("box_low must be below box_high")
    return cfg


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"No token at {TOKEN_FILE}. Run `python login.py` first.")
    data = json.loads(TOKEN_FILE.read_text())
    today = datetime.now(IST).date().isoformat()
    if data.get("date") != today:
        raise SystemExit(
            f"Cached token is from {data.get('date')}, not {today}. "
            "Kite tokens expire daily - run `python login.py` again."
        )
    return data


def resolve_instruments(kite: KiteConnect, cfg: dict) -> tuple[int, dict]:
    index_symbol = cfg["index_symbol"]
    quote = kite.ltp([index_symbol])
    if index_symbol not in quote:
        raise SystemExit(f"Kite did not recognise {index_symbol}")
    index_token = quote[index_symbol]["instrument_token"]

    today = datetime.now(IST).date()
    best = None
    for inst in kite.instruments("NFO"):
        if inst.get("name") != cfg["fut_name"] or inst.get("instrument_type") != "FUT":
            continue
        expiry = inst.get("expiry")
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        if not expiry or expiry < today:
            continue
        if best is None or expiry < best[0]:
            best = (expiry, inst)

    if best is None:
        raise SystemExit(f"No live {cfg['fut_name']} future found in the NFO dump")

    expiry, fut = best
    log.info(
        "index %s token=%s | volume from %s (expiry %s, token %s)",
        index_symbol,
        index_token,
        fut["tradingsymbol"],
        expiry,
        fut["instrument_token"],
    )
    return index_token, fut


class FetchError(Exception):
    """A historical-data request failed.

    Deliberately NOT SystemExit: during the session a dropped packet must be
    retried, not treated as the end of the trading day. Only the caller knows
    whether a failure is fatal.
    """


def _interval(minutes: int) -> str:
    return "minute" if minutes == 1 else f"{minutes}minute"


def fetch_candles(
    kite: KiteConnect,
    index_token: int,
    fut_token: int,
    frm: datetime,
    to: datetime,
    minutes: int,
) -> list:
    """Historical bars: OHLC from the index, volume from the future.

    Mirrors the live path exactly - the index carries no volume of its own, so
    the future supplies the weight for VWAP. Only candles that have fully closed
    by ``to`` are returned; a half-formed candle would poison both VWAP and the
    180-pt filter.
    """
    interval = _interval(minutes)
    try:
        idx = kite.historical_data(index_token, frm, to, interval)
        fut = kite.historical_data(fut_token, frm, to, interval)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(str(exc)) from exc

    volume_at = {}
    for c in fut:
        ts = c["date"]
        volume_at[ts.replace(tzinfo=IST) if ts.tzinfo is None else ts] = int(
            c.get("volume") or 0
        )

    bars = []
    for c in idx:
        ts = c["date"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        if ts.time() < SESSION_START:
            continue
        end = ts + timedelta(minutes=minutes)
        if end > to:
            continue  # still forming
        bars.append(
            Bar(
                start=ts,
                end=end,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=volume_at.get(ts, 0),
            )
        )
    return bars


def tick_time(tick: dict, fallback: datetime) -> datetime:
    ts = tick.get("exchange_timestamp") or tick.get("last_trade_time")
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=IST)
    return fallback


def connect(cfg: dict) -> tuple:
    token = load_token()
    api_key = os.environ.get("KITE_API_KEY") or token["api_key"]
    kite = KiteConnect(api_key=api_key, timeout=HTTP_TIMEOUT)
    kite.set_access_token(token["access_token"])
    kite.profile()  # fail fast if the token is dead
    index_token, fut = resolve_instruments(kite, cfg)
    return kite, api_key, token, index_token, fut


def replay(cfg: dict, notify: Emailer, day: date) -> None:
    """Run a past session end to end from historical candles. No websocket.

    Intra-candle price is approximated by the candle's own extremes, so a target
    is detected in the candle that actually reached it - but at the candle's
    high/low, not at the tick that first crossed. Entries, stops and box touches
    are all close-based, so those are exact.
    """
    kite, _api_key, _token, index_token, fut = connect(cfg)
    fut_token = fut["instrument_token"]
    minutes = int(cfg["candle_minutes"])

    frm = datetime.combine(day, SESSION_START, tzinfo=IST)
    to = datetime.combine(day, SESSION_END, tzinfo=IST)
    try:
        bars = fetch_candles(kite, index_token, fut_token, frm, to, minutes)
    except FetchError as exc:
        raise SystemExit(f"Historical data request failed: {exc}") from exc
    if not bars:
        raise SystemExit(f"No candles returned for {day} - was it a trading day?")

    vwap = SessionVWAP()
    strat = Strategy(cfg=cfg, notify=notify)
    log.info(
        "replaying %s: %d candles, %s box %.0f-%.0f, zone %.0f-%.0f",
        day, len(bars), strat.side.value, strat.box_low, strat.box_high,
        strat.zone_low, strat.zone_high,
    )
    print(f"\n{'bar':<7}{'open':>10}{'high':>10}{'low':>10}{'close':>10}"
          f"{'volume':>10}{'vwap':>11}   state")
    print("-" * 88)

    for b in bars:
        if strat.state is State.DONE:
            break
        # Ticks reach the strategy before the candle closes.
        if strat.state is State.IN_TRADE:
            strat.on_price(b.end, b.high)
            if strat.state is not State.DONE:
                strat.on_price(b.end, b.low)
        if strat.state is State.DONE:
            print(f"{b.start:%H:%M}  {b.open:>10.2f}{b.high:>10.2f}{b.low:>10.2f}"
                  f"{b.close:>10.2f}{b.volume:>10}{'':>11}   DONE (intra-candle)")
            break
        vwap.update(b)
        strat.on_bar(b, vwap.value)
        strat.on_clock(b.end)
        print(f"{b.start:%H:%M}  {b.open:>10.2f}{b.high:>10.2f}{b.low:>10.2f}"
              f"{b.close:>10.2f}{b.volume:>10}"
              f"{(f'{vwap.value:.2f}' if vwap.value else 'n/a'):>11}   "
              f"{strat.state.value}")

    print(f"\nfinal state: {strat.state.value}")
    if strat.entry is not None:
        print(f"entry {strat.entry:.2f}  target {strat.target:.2f}")


def run(cfg: dict, notify: Emailer, skip_backfill: bool = False) -> None:
    kite, api_key, token, index_token, fut = connect(cfg)
    fut_token = fut["instrument_token"]

    vwap = SessionVWAP()
    strat = Strategy(cfg=cfg, notify=notify)

    log.info(
        "armed: %s box %.0f-%.0f no-trade zone %.0f-%.0f violent>%.0f "
        "cutoff %s squareoff %s",
        strat.side.value,
        strat.box_low,
        strat.box_high,
        strat.zone_low,
        strat.zone_high,
        strat.violent,
        cfg["no_new_entry_after"],
        cfg["squareoff_alert_at"],
    )

    ticks_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    ticker = KiteTicker(api_key, token["access_token"])

    def on_ticks(ws, ticks):  # noqa: ANN001 - kiteconnect callback signature
        ticks_q.put(ticks)

    def on_connect(ws, response):  # noqa: ANN001
        ws.subscribe([index_token, fut_token])
        ws.set_mode(ws.MODE_FULL, [index_token, fut_token])
        log.info("websocket connected, subscribed to 2 instruments")

    def on_error(ws, code, reason):  # noqa: ANN001
        log.error("websocket error %s: %s", code, reason)

    def on_close(ws, code, reason):  # noqa: ANN001
        log.warning("websocket closed %s: %s", code, reason)

    def on_reconnect(ws, attempt):  # noqa: ANN001
        log.warning("websocket reconnecting, attempt %s", attempt)

    def on_noreconnect(ws):  # noqa: ANN001
        log.error("websocket gave up reconnecting")

    ticker.on_ticks = on_ticks
    ticker.on_connect = on_connect
    ticker.on_error = on_error
    ticker.on_close = on_close
    ticker.on_reconnect = on_reconnect
    ticker.on_noreconnect = on_noreconnect
    ticker.connect(threaded=True)

    def handle_bar(bar: Bar) -> None:
        vwap.update(bar)
        strat.on_bar(bar, vwap.value)

    # ---- candles come from history, never from ticks --------------------- #
    # Kite's websocket sends throttled snapshots (~1/sec), not every trade, so
    # tick-built candles get the open and the extremes wrong - measured errors
    # of 32 pts on an open and 48 pts on a high. The 180-pt filter kills whole
    # days off that number, so candles are polled from the historical endpoint
    # instead: exact OHLC, exact volume, and identical data to --replay.
    # Ticks are kept only for the live LTP target check.
    minutes = int(cfg["candle_minutes"])
    launch = datetime.now(IST)
    session_open = datetime.combine(launch.date(), SESSION_START, tzinfo=IST)

    # cursor = end of the last candle consumed, i.e. start of the next one due.
    cursor = session_open
    next_poll: Optional[datetime] = None

    def consume(bars: list) -> None:
        nonlocal cursor
        for b in bars:
            if b.start < cursor:
                continue  # history handed back something already consumed
            handle_bar(b)
            cursor = b.end
            if strat.state is State.DONE:
                return

    if launch > session_open and not skip_backfill:
        # Failing at startup is fine to surface immediately - you are watching.
        try:
            done = fetch_candles(kite, index_token, fut_token, cursor, launch, minutes)
        except FetchError as exc:
            raise SystemExit(
                f"Backfill failed: {exc}\nThe Connect plan includes historical "
                "candles - if this is a permissions error, check the app's "
                "entitlements in the developer console."
            ) from exc
        if done:
            log.info(
                "backfilled %d candles, %s to %s",
                len(done), f"{done[0].start:%H:%M}", f"{done[-1].end:%H:%M}",
            )
            consume(done)
        else:
            log.info("no completed candles to backfill yet")
    elif skip_backfill:
        log.info("backfill disabled (--no-backfill) - no candles will be fetched")

    feed = {"last_tick": time.monotonic(), "heartbeat": time.monotonic(),
            "warned": False, "forced": 0.0}
    fetch_fail = {"n": 0, "since": None, "warned": False}

    def fatal_exit(subject: str, detail: str) -> None:
        """Never die quietly during market hours."""
        in_trade = strat.state is State.IN_TRADE
        log.error("%s :: %s", subject, detail.replace("\n", " | "))
        notify.send(
            subject,
            "\n".join([
                detail,
                "",
                f"Strategy state: {strat.state.value}",
                (
                    f"YOU ARE IN A {strat.side.value} FROM {strat.entry:.2f} "
                    f"WITH TARGET {strat.target:.2f}. Manage it manually - no "
                    "further alerts are coming."
                    if in_trade else "No position is open, nothing to manage."
                ),
            ]),
        )

    def watchdog(now: datetime) -> bool:
        """Detect a dead tick feed. Returns True if the run cannot continue.

        A websocket can die without firing on_close - the socket sits in
        CLOSE-WAIT and the reader thread never notices. The strategy then makes
        no decisions while appearing healthy, which is the worst failure mode
        available: no stop alert on an open position.
        """
        if not (SESSION_START <= now.time() <= SESSION_END):
            return False

        mono = time.monotonic()
        age = mono - feed["last_tick"]
        connected = ticker.is_connected()

        if mono - feed["heartbeat"] >= HEARTBEAT_EVERY:
            feed["heartbeat"] = mono
            log.info(
                "heartbeat: state=%s last tick %.0fs ago, ws_connected=%s, vwap=%s",
                strat.state.value, age, connected, _fmt(vwap.value),
            )

        if age < STALE_WARN:
            if feed["warned"]:
                log.info("tick feed recovered after %.0fs", age)
                feed["warned"] = False
            return False

        if not feed["warned"]:
            feed["warned"] = True
            log.error("no ticks for %.0fs (ws_connected=%s)", age, connected)
            in_trade = strat.state is State.IN_TRADE
            notify.send(
                "FEED STALLED" + (" - POSITION OPEN" if in_trade else ""),
                "\n".join(
                    [
                        f"No ticks for {age:.0f}s. Websocket reports "
                        f"connected={connected}.",
                        "",
                        f"Strategy state: {strat.state.value}",
                        (
                            f"You are in a {strat.side.value} from "
                            f"{strat.entry:.2f}. The stop is NOT being monitored "
                            "while the feed is down - watch it yourself."
                            if in_trade
                            else "No position is open."
                        ),
                        "",
                        "Candles are unaffected - they come from the historical "
                        "endpoint. What is dead is the live target check.",
                    ]
                ),
            )

        if age > STALE_FORCE_RECONNECT and mono - feed["forced"] > 60:
            feed["forced"] = mono
            log.error("forcing websocket reconnect after %.0fs of silence", age)
            try:
                ticker.close()          # triggers kiteconnect's reconnect loop
            except Exception as exc:    # noqa: BLE001
                log.error("close() failed: %s", exc)
                try:
                    ticker.connect(threaded=True)
                except Exception as exc2:  # noqa: BLE001
                    log.error("reconnect failed: %s", exc2)

        if age > STALE_FATAL:
            in_trade = strat.state is State.IN_TRADE
            log.error("feed dead for %.0fs - giving up", age)
            notify.send(
                "FEED DEAD - script stopping"
                + (" - POSITION OPEN" if in_trade else ""),
                "\n".join(
                    [
                        f"No ticks for {age:.0f}s despite reconnect attempts. "
                        "Shutting down rather than pretending to watch.",
                        "",
                        f"Strategy state: {strat.state.value}",
                        (
                            f"YOU ARE IN A {strat.side.value} FROM "
                            f"{strat.entry:.2f} WITH TARGET {strat.target:.2f}. "
                            "Manage it manually - no further alerts are coming."
                            if in_trade
                            else "No position is open, nothing to manage."
                        ),
                    ]
                ),
            )
            return True

        return False

    try:
        while not stop.is_set():
            now = datetime.now(IST)

            try:
                batch = ticks_q.get(timeout=1.0)
            except queue.Empty:
                batch = []

            if batch:
                feed["last_tick"] = time.monotonic()

            # Ticks are only for the live LTP target check now.
            for tick in batch:
                if tick.get("instrument_token") != index_token:
                    continue
                ts = tick_time(tick, now)
                if ts.time() < SESSION_START:
                    continue  # ignore the pre-open auction
                ltp = tick.get("last_price")
                if ltp:
                    strat.on_price(ts, float(ltp))

            # Ask history for the next candle once it has closed and settled.
            due = cursor + timedelta(minutes=minutes)
            if now >= due + timedelta(seconds=POLL_DELAY) and (
                next_poll is None or now >= next_poll
            ):
                try:
                    got = fetch_candles(
                        kite, index_token, fut_token, cursor, now, minutes
                    )
                except FetchError as exc:
                    # A dropped request is not the end of the day. Back off,
                    # keep trying, and escalate only if it persists.
                    if fetch_fail["since"] is None:
                        fetch_fail["since"] = now
                    fetch_fail["n"] += 1
                    down = (now - fetch_fail["since"]).total_seconds()
                    backoff = min(POLL_RETRY * 2 ** (fetch_fail["n"] - 1),
                                  FETCH_BACKOFF_MAX)
                    log.error(
                        "historical fetch failed (%d in a row, %.0fs): %s "
                        "- retrying in %ds",
                        fetch_fail["n"], down, exc, backoff,
                    )
                    if down >= FETCH_FATAL_AFTER:
                        fatal_exit(
                            "DATA FEED DEAD - script stopping",
                            f"Historical data has been unreachable for "
                            f"{down:.0f}s over {fetch_fail['n']} attempts.\n\n"
                            f"Last error: {exc}",
                        )
                        break
                    if down >= FETCH_WARN_AFTER and not fetch_fail["warned"]:
                        fetch_fail["warned"] = True
                        in_trade = strat.state is State.IN_TRADE
                        notify.send(
                            "DATA FEED FAILING"
                            + (" - POSITION OPEN" if in_trade else ""),
                            "\n".join([
                                f"Historical candle fetches have been failing "
                                f"for {down:.0f}s ({fetch_fail['n']} attempts).",
                                f"Last error: {exc}",
                                "",
                                f"Strategy state: {strat.state.value}",
                                (
                                    f"You are in a {strat.side.value} from "
                                    f"{strat.entry:.2f}. No candle-based exit "
                                    "can fire while this lasts - watch it "
                                    "yourself."
                                    if in_trade else "No position is open."
                                ),
                                "",
                                "Still retrying. You get one more mail if it "
                                "does not recover.",
                            ]),
                        )
                    next_poll = now + timedelta(seconds=backoff)
                else:
                    if fetch_fail["n"]:
                        log.info(
                            "historical fetch recovered after %d failures",
                            fetch_fail["n"],
                        )
                        fetch_fail.update(n=0, since=None, warned=False)
                    if got:
                        next_poll = None
                        consume(got)
                    else:
                        log.warning(
                            "history has not published the %s candle yet, "
                            "retrying in %ds", f"{due:%H:%M}", POLL_RETRY,
                        )
                        next_poll = now + timedelta(seconds=POLL_RETRY)

            strat.on_clock(now)

            if watchdog(now):
                break

            if strat.state is State.DONE:
                log.info("day finished - shutting down")
                break
            if now.time() >= SESSION_END and now >= datetime.combine(
                now.date(), SESSION_END, tzinfo=IST
            ) + timedelta(seconds=POLL_DELAY + 15):
                final = fetch_candles(
                    kite, index_token, fut_token, cursor, now, minutes
                )
                consume(final)
                strat.on_clock(now)
                log.info("session over - shutting down")
                break
    except KeyboardInterrupt:
        log.info("interrupted")
        if strat.state is State.IN_TRADE:
            notify.send(
                "STOPPED BY HAND - POSITION OPEN",
                f"You stopped the script while in a {strat.side.value} from "
                f"{strat.entry:.2f} (target {strat.target:.2f}). Nothing is "
                "watching it now.",
            )
    except BaseException as exc:  # noqa: BLE001
        # Anything unexpected - a bug, an API change, a dropped connection the
        # retry logic did not cover. On 2026-09-04 an uncaught read timeout
        # ended a live session with no notification at all. Never again.
        log.exception("unhandled error, shutting down")
        fatal_exit(
            "CRASHED - script stopping",
            f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        try:
            ticker.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--ignore-date", action="store_true",
                        help="run even if the config date is not today")
    parser.add_argument("--no-email", action="store_true",
                        help="log alerts to the console only")
    parser.add_argument("--test-email", action="store_true",
                        help="send one test email and exit")
    parser.add_argument("--no-backfill", action="store_true",
                        help="do not reconstruct the session from history at startup")
    parser.add_argument("--replay", metavar="YYYY-MM-DD",
                        help="replay a past session from historical candles and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # A replay is about a past day by definition, so the date guard is moot.
    cfg = load_config(Path(args.config), args.ignore_date or bool(args.replay))
    notify = Emailer(cfg["email"], enabled=not args.no_email and not args.replay)

    if args.test_email:
        notify.send("test", "Mail is working. No strategy was started.")
        return 0

    if args.replay:
        replay(cfg, notify, date.fromisoformat(args.replay))
        return 0

    run(cfg, notify, skip_backfill=args.no_backfill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
