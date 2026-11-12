
import os
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# =====================================================
# KONFIGURATION
# =====================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
DATA_DIR = os.getenv("DATA_DIR", ".")
TIMEZONE = ZoneInfo("Europe/Berlin")

os.makedirs(DATA_DIR, exist_ok=True)

DB_NAME = os.path.join(DATA_DIR, "habbo.sqlite3")

MAX_TALER_PER_DAY = 6
SECONDS_PER_HOUR = 3600

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN fehlt!")

# =====================================================
# DATENBANK
# =====================================================

db = sqlite3.connect(DB_NAME)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS active_shifts (
    user_id INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_work (
    user_id INTEGER NOT NULL,
    work_date TEXT NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, work_date)
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT,
    admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shift_leaders (
    user_id INTEGER PRIMARY KEY,
    leader_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_notifications (
    user_id INTEGER NOT NULL,
    promotion_level INTEGER NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (user_id, promotion_level)
);
""")

db.commit()

# =====================================================
# ZEITFUNKTIONEN
# =====================================================

def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    return datetime.fromisoformat(value)


def format_duration(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours} Std. {minutes} Min."


def is_admin(interaction):
    if not interaction.guild:
        return False

    if interaction.guild_permissions.administrator:
        return True

    if ADMIN_ROLE_ID == 0:
        return False

    return any(
        role.id == ADMIN_ROLE_ID
        for role in interaction.user.roles
    )


def get_work_seconds(user_id, date):
    row = db.execute("""
        SELECT seconds FROM daily_work
        WHERE user_id = ? AND work_date = ?
    """, (user_id, date)).fetchone()

    return row["seconds"] if row else 0


def get_daily_talers(user_id, date):
    seconds = get_work_seconds(user_id, date)
    return min(seconds // SECONDS_PER_HOUR, MAX_TALER_PER_DAY)


# =====================================================
# ARBEITSZEIT SPEICHERN
# =====================================================

def add_work_seconds(user_id, start, end):
    cursor = start

    while cursor < end:
        local_cursor = cursor.astimezone(TIMEZONE)
        work_date = local_cursor.date()

        next_midnight = datetime.combine(
            work_date + timedelta(days=1),
            time.min,
            tzinfo=TIMEZONE
        ).astimezone(timezone.utc)

        segment_end = min(end, next_midnight)

        seconds = int(
            (segment_end - cursor).total_seconds()
        )

        if seconds > 0:
            db.execute("""
                INSERT INTO daily_work
                    (user_id, work_date, seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, work_date)
                DO UPDATE SET seconds =
                    seconds + excluded.seconds
            """, (
                user_id,
                work_date.isoformat(),
                seconds
            ))

        cursor = segment_end

    db.commit()


def get_total_work_seconds(user_id):
    row = db.execute("""
        SELECT COALESCE(SUM(seconds), 0) AS total
        FROM daily_work
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    total = row["total"]

    active = db.execute("""
        SELECT start_time FROM active_shifts
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    if active:
        start = parse_datetime(active["start_time"])
        total += max(
            0,
