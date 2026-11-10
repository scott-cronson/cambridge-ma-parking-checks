"""parking-checks command line. Every check supports --dry-run, which prints alerts instead of
sending them and works on an in-memory copy of the state database, so it changes nothing."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import ET
from .config import Config, ConfigError, load_config
from .health import Outcome, run_check
from .net import PoliteSession
from .notify import Notifier, default_channels
from .state import SCHEMA, State

log = logging.getLogger("parking_checks")


def _clock(fixed: datetime | None) -> Callable[[], datetime]:
    if fixed is None:
        return lambda: datetime.now(ET)
    return lambda: fixed


def _parse_now(v: str | None) -> datetime | None:
    if not v:
        return None
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=ET)


def open_state(cfg: Config, dry_run: bool) -> State:
    if not dry_run:
        return State(cfg.db_path)
    mem = State(":memory:")
    if cfg.db_path.is_file():
        src = sqlite3.connect(f"file:{cfg.db_path.as_posix()}?mode=ro", uri=True)
        try:
            src.backup(mem.conn)
        finally:
            src.close()
        mem.conn.executescript(SCHEMA)  # in case the file predates a table
    return mem


def _http(cfg: Config, min_interval: float = 1.0) -> PoliteSession:
    return PoliteSession(cfg.http_user_agent, timeout=20, min_interval=min_interval, retries=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parking-checks", description=__doc__)
    ap.add_argument("--env", type=Path, help="path to .env (default: repo .env or $PARKING_CHECKS_ENV)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, *, dry: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        if dry:
            p.add_argument("--dry-run", action="store_true", help="print alerts; change nothing")
            p.add_argument("--now", help="pretend the time is this ISO datetime (Eastern if no zone)")
        return p

    add("tow-plate", "search the tow lookup for the plate")
    add("tow-freshness", "confirm the tow lookup is live and current")
    add("tow-sweep", "sample every make for the latency log")
    add("tow-latency", "print how quickly tows reach the lookup", dry=False)
    add("cleaning-nightly", "compare the last parked spot with the cleaning schedule")
    add("serve-intake", "run the HTTPS intake endpoint (behind Tailscale Funnel)", dry=False)
    add("notify-test", "send a test message on every channel")
    add("telegram-chat-id", "list chats that have messaged the bot", dry=False)
    add("last-location", "print the newest stored parking location", dry=False)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_config(args.env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "serve-intake":
        return _serve_intake(cfg)
    if args.cmd == "telegram-chat-id":
        return _telegram_chat_id(cfg)

    dry = getattr(args, "dry_run", False)
    clock = _clock(_parse_now(getattr(args, "now", None)))
    state = open_state(cfg, dry)
    notifier = Notifier(state, default_channels(cfg), dry_run=dry, clock=clock)
    try:
        if args.cmd == "tow-latency":
            from .tow.latency import latency_report

            print(latency_report(state))
            return 0
        if args.cmd == "last-location":
            loc = state.latest_location()
            print(loc if loc else "no location stored")
            return 0
        if args.cmd == "notify-test":
            return _notify_test(cfg, notifier, clock)
        try:
            out = _run(args.cmd, cfg, state, notifier, clock)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
        print(f"{args.cmd}: {out.status}: {out.detail}")
        return 1 if out.status == "error" else 0
    finally:
        state.close()


def _run(cmd: str, cfg: Config, state: State, notifier: Notifier, clock) -> Outcome:
    if cmd in ("tow-plate", "tow-freshness", "tow-sweep"):
        from .tow.client import TowClient

        if cmd == "tow-plate":
            cfg.require_plate()

        client = TowClient(_http(cfg))
        if cmd == "tow-plate":
            from .tow.plate_check import run_plate_check

            fn = lambda: run_plate_check(cfg, client, state, notifier, clock())  # noqa: E731
        elif cmd == "tow-freshness":
            from .tow.freshness_check import run_freshness_check

            fn = lambda: run_freshness_check(cfg, client, state, notifier, clock())  # noqa: E731
        else:
            from .tow.latency import run_sweep

            fn = lambda: run_sweep(cfg, client, state, clock())  # noqa: E731
        return run_check(cmd, state, notifier, clock, fn)

    if cmd == "cleaning-nightly":
        from .cleaning.calendar import CityCalendar
        from .cleaning.geocode import reverse_geocode
        from .cleaning.nightly_check import run_nightly_check
        from .cleaning.schedule import Schedule

        sched = Schedule.load(cfg.schedule_path, cfg.coverage_center)
        calendar = CityCalendar(_http(cfg), sched)
        geo_http = _http(cfg, min_interval=1.1)
        return run_check(
            cmd,
            state,
            notifier,
            clock,
            lambda: run_nightly_check(
                cfg,
                sched,
                state,
                notifier,
                calendar,
                clock(),
                geocoder=lambda lat, lon: reverse_geocode(geo_http, lat, lon),
            ),
        )
    raise SystemExit(f"unknown command {cmd}")


def _notify_test(cfg: Config, notifier: Notifier, clock) -> int:
    now = clock()
    chans = [c.name for c in notifier.channels]
    if not chans and not notifier.dry_run:
        print("no channels configured: set SMTP_* + ALERT_EMAILS and/or TELEGRAM_*", file=sys.stderr)
        return 2
    res = notifier.notify(
        "test",
        "Test alert",
        f"This is a test from cambridge-ma-parking-checks at {now:%Y-%m-%d %H:%M %Z}.\n"
        f"Channels: {', '.join(chans) or 'none'}. Plate: {cfg.plate}.",
        f"test:{now.isoformat()}",
        short="Parking checks test alert. If you can read this, this channel works.",
    )
    print(res)
    return 0 if res and all(v in ("sent", "dry-run") for v in res.values()) else 1


def _telegram_chat_id(cfg: Config) -> int:
    if not cfg.telegram_bot_token:
        print("set TELEGRAM_BOT_TOKEN first", file=sys.stderr)
        return 2
    import requests

    r = requests.get(f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getUpdates", timeout=20)
    if r.status_code != 200:
        print(f"Telegram API HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 1
    chats = {}
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            chats[chat["id"]] = name
    if not chats:
        print("No chats yet. Open your bot in Telegram, press Start, send any message, then rerun.")
        return 1
    for cid, name in chats.items():
        print(f"{cid}\t{name}")
    print("\nPut the id(s) in TELEGRAM_CHAT_IDS in .env (comma separated).")
    return 0


def _serve_intake(cfg: Config) -> int:
    import uvicorn

    from .cleaning.geocode import reverse_geocode
    from .cleaning.intake import create_app
    from .cleaning.schedule import Schedule

    try:
        cfg.require_intake_token()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    sched = Schedule.load(cfg.schedule_path, cfg.coverage_center)
    State(cfg.db_path).close()  # create schema before serving
    geo_http = _http(cfg, min_interval=1.1)
    app = create_app(
        cfg,
        lambda: State(cfg.db_path),
        sched,
        geocoder=lambda lat, lon: reverse_geocode(geo_http, lat, lon),
    )
    uvicorn.run(
        app,
        host=cfg.intake_host,
        port=cfg.intake_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
