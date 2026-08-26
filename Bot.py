import asyncio
import calendar
from contextlib import contextmanager
from datetime import datetime, time, timedelta
import functools
import logging
import os
import signal
from typing import Any, Callable, Generator, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from aiohttp import web
import psycopg2
from psycopg2 import pool
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.helpers import escape_markdown

# =========================================================
# CONFIGURATION & LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MeditationBot")

TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "-4721378655"))
DATABASE_URL = os.getenv("DATABASE_URL")
TIMEZONE = ZoneInfo("Asia/Kolkata")
SESSION_MINUTES = 20

# Global Connection Pool reference
DB_POOL: Optional[pool.ThreadedConnectionPool] = None


# =========================================================
# UI DESIGN SYSTEM & FORMATTING HELPERS
# =========================================================

def esc(text: Any) -> str:
    """Escapes strings safely for Telegram MarkdownV2."""
    if text is None:
        return ""
    return escape_markdown(str(text), version=2)


def get_current_date() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def render_progress_bar(percentage: int, total_blocks: int = 10) -> str:
    filled = int(round((percentage / 100) * total_blocks))
    filled = max(0, min(total_blocks, filled))
    return "▓" * filled + "░" * (total_blocks - filled)


def generate_color_grid(
    year: int,
    month: int,
    attended_days: Set[int],
    current_year: int,
    current_month: int,
    current_day: int,
) -> str:
    cal = calendar.monthcalendar(year, month)
    lines = [
        " Mo  Tu  We  Th  Fr  Sa  Su ",
        "─────────────────────────────────",
    ]

    is_current_month = year == current_year and month == current_month
    is_past_month = (year < current_year) or (year == current_year and month < current_month)

    for week in cal:
        row = " "
        for day in week:
            if day == 0:
                cell = "     "
            elif day in attended_days:
                cell = f"{day:02d}🟩"
            elif is_current_month and day == current_day:
                cell = f"{day:02d}🟨"
            elif is_past_month or (is_current_month and day < current_day):
                cell = f"{day:02d}🟥"
            else:
                cell = f"{day:02d}⬜"
            row += cell + " "
        lines.append(row.rstrip())
    return "\n".join(lines)


# =========================================================
# RESILIENT DATABASE LAYER (CONNECTION POOL + RETRIES)
# =========================================================

def init_db_pool() -> None:
    """Initializes a thread-safe connection pool for Postgres/Neon."""
    global DB_POOL
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing! Please configure it in your dashboard.")
    
    DB_POOL = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=DATABASE_URL,
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    logger.info("PostgreSQL connection pool initialized successfully.")


def close_db_pool() -> None:
    """Closes all pooled database connections cleanly."""
    global DB_POOL
    if DB_POOL and not DB_POOL.closed:
        DB_POOL.closeall()
        logger.info("PostgreSQL connection pool closed.")


@contextmanager
def get_db_cursor() -> Generator[Any, None, None]:
    """Safe context manager to acquire and return a pooled connection & cursor."""
    if DB_POOL is None:
        raise RuntimeError("Database pool has not been initialized.")
    
    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        DB_POOL.putconn(conn)


def db_retry(max_retries: int = 3, backoff_factor: float = 0.5) -> Callable:
    """Decorator to retry transient database connection failures (e.g., Neon cold-start)."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (psycopg2.OperationalError, psycopg2.DatabaseError) as err:
                    last_err = err
                    sleep_time = backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        f"Database operational error on attempt {attempt}/{max_retries} "
                        f"in {func.__name__}: {err}. Retrying in {sleep_time:.2f}s..."
                    )
                    import time as t
                    t.sleep(sleep_time)
            logger.error(f"Function {func.__name__} failed after {max_retries} attempts.")
            raise last_err
        return wrapper
    return decorator


@db_retry(max_retries=3)
def _setup_database_sync() -> None:
    with get_db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                session TEXT NOT NULL,
                duration_minutes INTEGER DEFAULT 20,
                status TEXT DEFAULT 'present',
                CONSTRAINT unique_user_date_session UNIQUE(user_id, attendance_date, session)
            );
            CREATE INDEX IF NOT EXISTS idx_user_date ON attendance(user_id, attendance_date);
            CREATE INDEX IF NOT EXISTS idx_date_user ON attendance(attendance_date, user_id);
        """)


@db_retry(max_retries=3)
def _record_attendance_sync(user_id: int, name: str, date_str: str, session: str) -> bool:
    with get_db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO attendance (user_id, name, attendance_date, session, duration_minutes, status)
            VALUES (%s, %s, %s, %s, %s, 'present')
            ON CONFLICT (user_id, attendance_date, session) DO NOTHING
            RETURNING id;
            """,
            (user_id, name, date_str, session, SESSION_MINUTES),
        )
        inserted = cur.fetchone()
        if inserted:
            cur.execute("UPDATE attendance SET name = %s WHERE user_id = %s", (name, user_id))
            return True
        return False


@db_retry(max_retries=3)
def _calculate_streak_sync(user_id: int) -> int:
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT attendance_date FROM attendance WHERE user_id = %s AND status = 'present' ORDER BY attendance_date DESC",
            (user_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    dates: Set = {datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows}
    today = datetime.now(TIMEZONE).date()
    yesterday = today - timedelta(days=1)

    current_check = today if today in dates else yesterday
    if current_check not in dates:
        return 0

    streak = 0
    while current_check in dates:
        streak += 1
        current_check -= timedelta(days=1)
    return streak


@db_retry(max_retries=3)
def _get_all_members_sync() -> List[Tuple[int, str]]:
    with get_db_cursor() as cur:
        cur.execute("SELECT DISTINCT user_id, name FROM attendance ORDER BY name ASC")
        return cur.fetchall()


async def setup_database() -> None:
    await asyncio.to_thread(_setup_database_sync)


async def record_attendance(user_id: int, name: str, date_str: str, session: str = "daily") -> bool:
    return await asyncio.to_thread(_record_attendance_sync, user_id, name, date_str, session)


async def calculate_streak(user_id: int) -> int:
    return await asyncio.to_thread(_calculate_streak_sync, user_id)


async def get_all_members() -> List[Tuple[int, str]]:
    return await asyncio.to_thread(_get_all_members_sync)


# =========================================================
# KEYBOARDS & UI DISPATCH
# =========================================================

def build_main_keyboard(date_str: Optional[str] = None) -> InlineKeyboardMarkup:
    today = date_str or get_current_date()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Instant Check-In", callback_data=f"attend:{today}:daily"),
        ],
        [
            InlineKeyboardButton("👤 Member Stats", callback_data="picker:stats"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="view:leaderboard"),
        ],
        [
            InlineKeyboardButton("📅 Calendars", callback_data="picker:grid"),
            InlineKeyboardButton("📉 Analytics", callback_data="picker:missed"),
        ],
        [
            InlineKeyboardButton("📊 Group Overview", callback_data="view:report"),
        ],
    ])


async def build_member_picker_keyboard(target_action: str, current_user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    members = await get_all_members()
    keyboard: List[List[InlineKeyboardButton]] = []

    if target_action == "stats" and current_user_id:
        keyboard.append([InlineKeyboardButton("✨ View My Own Stats", callback_data=f"user:stats:{current_user_id}")])

    row: List[InlineKeyboardButton] = []
    for user_id, name in members:
        display_name = (name[:11] + "…") if len(name) > 12 else name
        row.append(InlineKeyboardButton(f"👤 {display_name}", callback_data=f"user:{target_action}:{user_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("← Back to Menu", callback_data="view:menu")])
    return InlineKeyboardMarkup(keyboard)


async def reply(update_or_query: Any, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup
            )
        else:
            if hasattr(update_or_query, "edit_message_text"):
                try:
                    await update_or_query.edit_message_text(
                        text=text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup
                    )
                    return
                except Exception:
                    pass
            chat_id = update_or_query.message.chat_id if update_or_query.message else update_or_query.from_user.id
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Error rendering message: {e}", exc_info=True)


# =========================================================
# HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    formatted_date = datetime.now(TIMEZONE).strftime("%A, %b %d")

    @db_retry(max_retries=3)
    def _query_today_roster():
        with get_db_cursor() as cur:
            # Query all registered members
            cur.execute("SELECT DISTINCT user_id, name FROM attendance ORDER BY name ASC")
            all_known = cur.fetchall()

            # Query today's present records
            cur.execute(
                "SELECT user_id, name FROM attendance WHERE attendance_date = %s AND status = 'present'",
                (today,),
            )
            today_records = cur.fetchall()

            # Pre-fetch attendance dates for instant streak calculation
            cur.execute(
                "SELECT user_id, attendance_date FROM attendance WHERE status = 'present' ORDER BY attendance_date DESC"
            )
            all_attendance = cur.fetchall()

        user_dates = {}
        for uid, a_date in all_attendance:
            if uid not in user_dates:
                user_dates[uid] = set()
            user_dates[uid].add(datetime.strptime(a_date, "%Y-%m-%d").date())

        today_dt = datetime.now(TIMEZONE).date()
        yesterday_dt = today_dt - timedelta(days=1)
        completed_uids = {uid for uid, _ in today_records}

        roster_lines = []
        for uid, name in all_known:
            dates = user_dates.get(uid, set())
            current_check = today_dt if today_dt in dates else yesterday_dt
            streak = 0
            if current_check in dates:
                while current_check in dates:
                    streak += 1
                    current_check -= timedelta(days=1)

            streak_tag = f" \\(🔥 `{streak}d`\\)" if streak > 0 else ""

            if uid in completed_uids:
                roster_lines.append(f"🟩 `{esc(name)}`{streak_tag}")
            else:
                roster_lines.append(f"🟥 `{esc(name)}`{streak_tag}")

        completed_count = len(completed_uids)
        total_members = len(all_known)
        remaining_count = max(0, total_members - completed_count)
        rate = int((completed_count / total_members) * 100) if total_members > 0 else 0

        return roster_lines, completed_count, remaining_count, rate

    try:
        roster_lines, completed_count, remaining_count, rate = await asyncio.to_thread(_query_today_roster)
    except Exception as e:
        logger.error(f"Failed to load roster in start handler: {e}", exc_info=True)
        roster_lines = ["_No members registered yet_"]
        completed_count, remaining_count, rate = 0, 0, 0

    roster_text = "\n".join(roster_lines) if roster_lines else "_No members registered yet_"
    progress_bar = render_progress_bar(rate, total_blocks=8)

    msg = (
        f"🧘 *TM SESSIONS DASHBOARD*\n"
        f"*{esc(formatted_date)}* • `{SESSION_MINUTES}m Daily`\n"
        f"`{progress_bar}` `{rate}% Done \\({completed_count}/{completed_count + remaining_count}\\)`\n"
        f"────────────────────────────\n\n"
        f"{roster_text}\n\n"
        f"────────────────────────────\n"
        f"*Summary:* `{completed_count} Checked In` • `{remaining_count} Remaining`\n\n"
        f"👇 *Select an option below:*"
    )

    await reply(update, context, msg, reply_markup=build_main_keyboard(today))


async def show_visual_grid(update_or_query: Any, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, year: Optional[int] = None, month: Optional[int] = None):
    today = datetime.now(TIMEZONE).date()
    year = year or today.year
    month = month or today.month
    month_prefix = f"{year:04d}-{month:02d}"
    month_name = datetime(year, month, 1).strftime("%B %Y")
    _, num_days_in_month = calendar.monthrange(year, month)

    @db_retry(max_retries=3)
    def _query():
        with get_db_cursor() as cur:
            cur.execute("SELECT name FROM attendance WHERE user_id = %s LIMIT 1", (target_user_id,))
            user_row = cur.fetchone()
            name = user_row[0] if user_row else "Member"

            cur.execute(
                "SELECT DISTINCT attendance_date FROM attendance WHERE user_id = %s AND attendance_date LIKE %s AND status = 'present'",
                (target_user_id, f"{month_prefix}%"),
            )
            rows = cur.fetchall()
        return name, {datetime.strptime(r[0], "%Y-%m-%d").date().day for r in rows}

    name, attended_days = await asyncio.to_thread(_query)
    is_current_month = year == today.year and month == today.month
    is_past_month = (year < today.year) or (year == today.year and month < today.month)
    grid_ascii = generate_color_grid(year, month, attended_days, today.year, today.month, today.day)

    completed_count = len(attended_days)
    if is_current_month:
        missed_count = max(0, (today.day - 1) - len([d for d in attended_days if d < today.day]))
    elif is_past_month:
        missed_count = max(0, num_days_in_month - completed_count)
    else:
        missed_count = 0

    completion_rate = int((completed_count / num_days_in_month) * 100)
    bar = render_progress_bar(completion_rate)

    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    nav_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀ Prev", callback_data=f"navgrid:{target_user_id}:{prev_year}:{prev_month}"),
            InlineKeyboardButton("Next ▶", callback_data=f"navgrid:{target_user_id}:{next_year}:{next_month}"),
        ],
        [
            InlineKeyboardButton("📈 Analytics", callback_data=f"user:missed:{target_user_id}"),
            InlineKeyboardButton("👥 Switch Member", callback_data="picker:grid"),
        ],
        [InlineKeyboardButton("← Main Menu", callback_data="view:menu")],
    ])

    msg = (
        f"📅 *CALENDAR MATRIX*\n"
        f"👤 *{esc(name)}* • `{esc(month_name)}`\n"
        f"─────────────────────────────────\n"
        f"`{bar}` `{completed_count}/{num_days_in_month} Days`\n"
        f"• *Completed:* `{completed_count}` \\| *Missed:* `{missed_count}`\n\n"
        f"```text\n{grid_ascii}\n
