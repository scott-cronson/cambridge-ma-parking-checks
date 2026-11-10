"""SQLite state: parked locations, tow records, sent alerts, and check runs.

All timestamps are stored as ISO 8601 strings with a UTC offset.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    captured_at TEXT,
    device      TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    street_raw  TEXT,
    city        TEXT,
    source      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tow_records (
    id            INTEGER PRIMARY KEY,
    plate         TEXT NOT NULL,
    make          TEXT,
    model         TEXT,
    tow_datetime  TEXT NOT NULL,
    towed_from    TEXT,
    tow_company   TEXT,
    reason        TEXT,
    tracking_no   TEXT,
    first_seen_at TEXT NOT NULL,
    first_seen_by TEXT,
    UNIQUE (plate, tow_datetime)
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    dedupe_key TEXT NOT NULL,
    channel    TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (dedupe_key, channel)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    check_name  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS runs_check ON runs (check_name, id);
"""


@dataclass
class Location:
    id: int
    received_at: datetime
    captured_at: datetime | None
    device: str
    lat: float | None
    lon: float | None
    street_raw: str | None
    city: str | None
    source: str

    @property
    def logged_at(self) -> datetime:
        return self.captured_at or self.received_at


@dataclass
class Run:
    id: int
    check_name: str
    started_at: datetime
    outcome: str | None
    detail: str | None


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- locations -------------------------------------------------------------------------
    def save_location(
        self,
        *,
        received_at: datetime,
        device: str,
        lat: float | None,
        lon: float | None,
        street_raw: str | None,
        city: str | None,
        source: str,
        captured_at: datetime | None = None,
    ) -> Location:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO locations (received_at, captured_at, device, lat, lon, street_raw,"
                " city, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    received_at.isoformat(),
                    captured_at.isoformat() if captured_at else None,
                    device,
                    lat,
                    lon,
                    street_raw,
                    city,
                    source,
                ),
            )
        loc = self.get_location(cur.lastrowid)
        assert loc is not None
        return loc

    def update_location_street(self, loc_id: int, street_raw: str, source: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE locations SET street_raw = ?, source = ? WHERE id = ?",
                (street_raw, source, loc_id),
            )

    def _loc(self, row: sqlite3.Row | None) -> Location | None:
        if row is None:
            return None
        return Location(
            id=row["id"],
            received_at=_dt(row["received_at"]),  # type: ignore[arg-type]
            captured_at=_dt(row["captured_at"]),
            device=row["device"],
            lat=row["lat"],
            lon=row["lon"],
            street_raw=row["street_raw"],
            city=row["city"],
            source=row["source"],
        )

    def get_location(self, loc_id: int) -> Location | None:
        return self._loc(self.conn.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)).fetchone())

    def latest_location(self) -> Location | None:
        row = self.conn.execute("SELECT * FROM locations ORDER BY id DESC LIMIT 1").fetchone()
        return self._loc(row)

    # --- tow records -----------------------------------------------------------------------
    def record_tow(
        self,
        *,
        plate: str,
        tow_datetime: datetime,
        first_seen_at: datetime,
        first_seen_by: str,
        make: str | None = None,
        model: str | None = None,
        towed_from: str | None = None,
        tow_company: str | None = None,
        reason: str | None = None,
        tracking_no: str | None = None,
    ) -> bool:
        """Insert a tow record. Returns True when it was not seen before.

        Later sightings fill in detail fields that an earlier summary row lacked.
        """
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO tow_records (plate, make, model, tow_datetime, towed_from,"
                " tow_company, reason, tracking_no, first_seen_at, first_seen_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plate,
                    make,
                    model,
                    tow_datetime.isoformat(),
                    towed_from,
                    tow_company,
                    reason,
                    tracking_no,
                    first_seen_at.isoformat(),
                    first_seen_by,
                ),
            )
            if cur.rowcount == 0:
                self.conn.execute(
                    "UPDATE tow_records SET make = COALESCE(make, ?), model = COALESCE(model, ?),"
                    " towed_from = COALESCE(towed_from, ?), tow_company = COALESCE(tow_company, ?),"
                    " reason = COALESCE(reason, ?), tracking_no = COALESCE(tracking_no, ?)"
                    " WHERE plate = ? AND tow_datetime = ?",
                    (
                        make,
                        model,
                        towed_from,
                        tow_company,
                        reason,
                        tracking_no,
                        plate,
                        tow_datetime.isoformat(),
                    ),
                )
        return cur.rowcount == 1

    def tow_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tow_records ORDER BY id").fetchall()

    # --- alerts ----------------------------------------------------------------------------
    def alert_sent(self, dedupe_key: str, channel: str | None = None) -> bool:
        if channel is None:
            q, args = "SELECT 1 FROM alerts_sent WHERE dedupe_key = ?", (dedupe_key,)
        else:
            q = "SELECT 1 FROM alerts_sent WHERE dedupe_key = ? AND channel = ?"
            args = (dedupe_key, channel)
        return self.conn.execute(q, args).fetchone() is not None

    def mark_sent(self, dedupe_key: str, channel: str, sent_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO alerts_sent (dedupe_key, channel, sent_at) VALUES (?, ?, ?)",
                (dedupe_key, channel, sent_at.isoformat()),
            )

    def sent_with_prefix(self, prefix: str) -> list[tuple[str, datetime]]:
        """Distinct dedupe keys starting with prefix, with the earliest send time of each."""
        rows = self.conn.execute(
            "SELECT dedupe_key, MIN(sent_at) AS t FROM alerts_sent WHERE dedupe_key LIKE ? ESCAPE '\\'"
            " GROUP BY dedupe_key ORDER BY t",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
        return [(r["dedupe_key"], datetime.fromisoformat(r["t"])) for r in rows]

    # --- runs ------------------------------------------------------------------------------
    def start_run(self, check_name: str, started_at: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (check_name, started_at) VALUES (?, ?)",
                (check_name, started_at.isoformat()),
            )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def finish_run(self, run_id: int, finished_at: datetime, outcome: str, detail: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, detail = ? WHERE id = ?",
                (finished_at.isoformat(), outcome, detail[:4000], run_id),
            )

    def recent_runs(self, check_name: str, limit: int = 20) -> list[Run]:
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE check_name = ? AND outcome IS NOT NULL ORDER BY id DESC LIMIT ?",
            (check_name, limit),
        ).fetchall()
        return [
            Run(r["id"], r["check_name"], datetime.fromisoformat(r["started_at"]), r["outcome"], r["detail"])
            for r in rows
        ]

    def first_run(self, check_name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE check_name = ? AND outcome IS NOT NULL ORDER BY id LIMIT 1",
            (check_name,),
        ).fetchone()
