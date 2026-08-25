import asyncio
import calendar
from datetime import datetime, time, timedelta
import logging
import os
import signal
from typing import List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from aiohttp import web
import psycopg2
from psycopg2.extras import RealDictCursor
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
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GROUP_ID = int(os.getenv("GROUP_ID", "-4721378655"))
DATABASE_URL = os.getenv("DATABASE_URL")
TIMEZONE = ZoneInfo("Asia/Kolkata")
SESSION_MINUTES = 20


# =========================================================
# UI DESIGN SYSTEM & FORMATTING HELPERS
# =========================================================

def esc(text: any) -> str:
    if text is None:
        return ""
    return escape_markdown(str(text), version=2)


def get_current_date() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def render_progress_bar(percentage: int, total_blocks: int = 10) -> str:
    filled = int(round((percentage / 100) * total_blocks))
    filled = max(0, min(total_blocks, filled))
    return "▓" * filled + "░" * (total_blocks - filled)


def generate_color_grid(year: int, month: int, attended_days: Set[int], current_year: int, current_month: int, current_day: int) -> str:
    cal = calendar.monthcalendar(year, month)
    lines = [
        " Mo  Tu  We  Th  Fr  Sa  Su ",
        "─────────────────────────────────"
    ]

    is_current_month = (year == current_year and month == current_month)
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
# RESILIENT DATABASE LAYER (NEON POSTGRESQL)
# =========================================================

def _get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing!")
    
    # Retry up to 3 times to allow Neon compute wake-up
    last_err = None
    for _ in range(3):
        try:
            conn = psycopg2.connect(
                DATABASE_URL, 
                connect_timeout=15, 
                keepalives=1, 
                keepalives_idle=30, 
                keepalives_interval=10, 
                keepalives_count=5
            )
            return conn
        except Exception as e:
            last_err = e
            import time as t
            t.sleep(1)
    raise last_err


def _setup_database_sync() -> None:
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
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
        conn.commit()
    finally:
        conn.close()


def _record_attendance_sync(user_id: int, name: str, date_str: str, session: str) -> bool:
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
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
                conn.commit()
                return True
            return False
    finally:
        conn.close()


def _calculate_streak_sync(user_id: int) -> int:
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT attendance_date FROM attendance WHERE user_id = %s AND status = 'present' ORDER BY attendance_date DESC",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

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


def _get_all_members_sync() -> List[Tuple[int, str]]:
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_id, name FROM attendance ORDER BY name ASC")
            return cur.fetchall()
    finally:
        conn.close()


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


async def reply(update_or_query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
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
        logger.error(f"Error rendering message: {e}")


# =========================================================
# HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    formatted_date = datetime.now(TIMEZONE).strftime("%A, %b %d")
    month_prefix = today[:7]

    def _query_today_roster():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT user_id, name FROM attendance WHERE attendance_date LIKE %s",
                    (f"{month_prefix}%",),
                )
                all_known = cur.fetchall()

                cur.execute(
                    "SELECT user_id, name FROM attendance WHERE attendance_date = %s AND status = 'present'",
                    (today,),
                )
                today_records = cur.fetchall()

            completed = [name for _, name in today_records]
            completed_uids = {uid for uid, _ in today_records}
            pending = [name for uid, name in all_known if uid not in completed_uids]
            return completed, pending
        finally:
            conn.close()

    try:
        completed, pending = await asyncio.to_thread(_query_today_roster)
    except Exception as e:
        logger.error(f"DB error in start: {e}")
        completed, pending = [], []

    completed_str = " • ".join([f"`{esc(n)}`" for n in completed]) if completed else "`None`"
    pending_str = " • ".join([f"`{esc(n)}`" for n in pending]) if pending else "`None`"

    msg = (
        f"🧘 *MEDITATION DASHBOARD*\n"
        f"`{esc(formatted_date)} • 20m Sit`\n"
        f"─────────────────────────────\n"
        f"🟢 Done \\({len(completed)}\\): {completed_str}\n"
        f"🔴 Left \\({len(pending)}\\): {pending_str}\n"
        f"─────────────────────────────\n"
        f"Select an option below:"
    )

    await reply(update, context, msg, reply_markup=build_main_keyboard(today))


async def show_visual_grid(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, year: Optional[int] = None, month: Optional[int] = None):
    today = datetime.now(TIMEZONE).date()
    year = year or today.year
    month = month or today.month
    month_prefix = f"{year:04d}-{month:02d}"
    month_name = datetime(year, month, 1).strftime("%B %Y")
    _, num_days_in_month = calendar.monthrange(year, month)

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM attendance WHERE user_id = %s LIMIT 1", (target_user_id,))
                user_row = cur.fetchone()
                name = user_row[0] if user_row else "Member"

                cur.execute(
                    "SELECT DISTINCT attendance_date FROM attendance WHERE user_id = %s AND attendance_date LIKE %s AND status = 'present'",
                    (target_user_id, f"{month_prefix}%"),
                )
                rows = cur.fetchall()
            return name, {datetime.strptime(r[0], "%Y-%m-%d").date().day for r in rows}
        finally:
            conn.close()

    name, attended_days = await asyncio.to_thread(_query)
    is_current_month = (year == today.year and month == today.month)
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
        f"```text\n{grid_ascii}\n```\n"
        f"`XX🟩` Present  `XX🟥` Absent  `XX🟨` Today  `XX⬜` Future"
    )
    await reply(update_or_query, context, msg, reply_markup=nav_buttons)


async def show_member_stats_by_id(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    today = datetime.now(TIMEZONE).date()
    month = today.isoformat()[:7]
    month_name = today.strftime("%B %Y")
    days_passed = today.day

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM attendance WHERE user_id = %s LIMIT 1", (target_user_id,))
                user_row = cur.fetchone()
                name = user_row[0] if user_row else "Practitioner"

                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT attendance_date) FROM attendance WHERE user_id = %s AND attendance_date LIKE %s AND status = 'present'",
                    (target_user_id, f"{month}%"),
                )
                m_sits, m_days = cur.fetchone()

                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT attendance_date) FROM attendance WHERE user_id = %s AND status = 'present'",
                    (target_user_id,),
                )
                total_sits, total_days = cur.fetchone()

                cur.execute(
                    "SELECT user_id FROM attendance WHERE attendance_date LIKE %s AND status = 'present' GROUP BY user_id ORDER BY COUNT(*) DESC, COUNT(DISTINCT attendance_date) DESC",
                    (f"{month}%",),
                )
                rankings = [r[0] for r in cur.fetchall()]
            return name, m_sits, m_days, total_sits, total_days, rankings
        finally:
            conn.close()

    name, m_sits, m_days, total_sits, total_days, rankings = await asyncio.to_thread(_query)
    user_rank = f"#{rankings.index(target_user_id) + 1}" if target_user_id in rankings else "Unranked"
    streak = await calculate_streak(target_user_id)
    missed_month = max(0, days_passed - m_days)
    attendance_rate = int((m_days / days_passed) * 100) if days_passed > 0 else 0
    progress_bar = render_progress_bar(attendance_rate)

    nav_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Calendar", callback_data=f"user:grid:{target_user_id}"),
            InlineKeyboardButton("👥 Switch Member", callback_data="picker:stats"),
        ],
        [InlineKeyboardButton("← Main Menu", callback_data="view:menu")],
    ])

    msg = (
        f"👤 *MEMBER STATS*\n"
        f"*{esc(name)}*\n"
        f"────────────────────────────\n"
        f"Monthly Consistency \\({esc(month_name)}\\)\n"
        f"`{progress_bar}` `{attendance_rate}%`\n\n"
        f"🏆 *Key Metrics*\n"
        f"• *Active Streak:* `{streak} Days` 🔥\n"
        f"• *Current Standing:* `{esc(user_rank)}`\n"
        f"• *Completed:* `{m_days}/{days_passed} Days` \\(`{m_sits} Sessions`\\)\n"
        f"• *Missed:* `{missed_month} Days`\n"
        f"• *Lifetime Record:* `{total_sits} Sits` \\(`{total_days} Days`\\)"
    )
    await reply(update_or_query, context, msg, reply_markup=nav_buttons)


async def show_member_missed_analytics(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    today = datetime.now(TIMEZONE).date()
    days_in_month_so_far = today.day
    seven_days_ago = (today - timedelta(days=6)).isoformat()
    month_prefix = today.isoformat()[:7]

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM attendance WHERE user_id = %s LIMIT 1", (target_user_id,))
                user_row = cur.fetchone()
                name = user_row[0] if user_row else "Unknown Member"

                cur.execute("""
                    SELECT 
                        COUNT(DISTINCT CASE WHEN attendance_date >= %s AND status = 'present' THEN attendance_date END),
                        COUNT(DISTINCT CASE WHEN attendance_date LIKE %s AND status = 'present' THEN attendance_date END),
                        COUNT(DISTINCT CASE WHEN status = 'present' THEN attendance_date END)
                    FROM attendance
                    WHERE user_id = %s
                """, (seven_days_ago, f"{month_prefix}%", target_user_id))
                stats = cur.fetchone()
            return name, stats[0], stats[1], stats[2]
        finally:
            conn.close()

    name, week_sits, month_sits, total_sits = await asyncio.to_thread(_query)
    week_missed = max(0, 7 - week_sits)
    month_missed = max(0, days_in_month_so_far - month_sits)
    streak = await calculate_streak(target_user_id)
    rate = int((month_sits / days_in_month_so_far) * 100) if days_in_month_so_far > 0 else 0
    progress_bar = render_progress_bar(rate)
    status_tag = "Optimal Pace 🟢" if month_missed == 0 else ("Minor Gaps 🟡" if month_missed <= 4 else "Attention Needed 🔴")

    nav_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 View Calendar", callback_data=f"user:grid:{target_user_id}"),
            InlineKeyboardButton("👥 Switch Member", callback_data="picker:missed"),
        ],
        [InlineKeyboardButton("← Main Menu", callback_data="view:menu")],
    ])

    msg = (
        f"📊 *PERFORMANCE INSIGHTS*\n"
        f"👤 *{esc(name)}*\n"
        f"────────────────────────────\n"
        f"Consistency Rating\n"
        f"`{progress_bar}` `{rate}%`\n\n"
        f"• *Status:* {esc(status_tag)}\n"
        f"• *Active Streak:* `{streak} Days` 🔥\n\n"
        f"🗓 *Session Breakdown*\n"
        f"• *Last 7 Days:* `{week_sits}/7 completed` \\(`{week_missed} missed`\\)\n"
        f"• *Current Month:* `{month_sits}/{days_in_month_so_far} completed` \\(`{month_missed} missed`\\)\n"
        f"• *Lifetime Practice:* `{total_sits} Unique Days`"
    )
    await reply(update_or_query, context, msg, reply_markup=nav_buttons)


async def show_leaderboard(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days "
                    "FROM attendance WHERE attendance_date LIKE %s AND status = 'present' GROUP BY user_id, name ORDER BY sessions DESC, days DESC LIMIT 10",
                    (f"{today[:7]}%",),
                )
                return cur.fetchall()
        finally:
            conn.close()

    leaders = await asyncio.to_thread(_query)
    if not leaders:
        await reply(update_or_query, context, f"🏆 *LEADERBOARD \\({esc(month_name)}\\)*\n\n_No check\\-ins recorded yet this month\\._", reply_markup=build_main_keyboard())
        return

    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for rank, (name, s_count, d_count) in enumerate(leaders, start=1):
        badge = medals[rank - 1] if rank <= 3 else f"`{rank:02d}.`"
        rows.append(f"{badge} *{esc(name)}*\n    └ `{s_count} Sessions` • `{d_count} Days`")

    msg = f"🏆 *LEADERBOARD \\({esc(month_name)}\\)*\n────────────────────────────\n" + "\n\n".join(rows)
    await reply(update_or_query, context, msg, reply_markup=build_main_keyboard())


async def show_group_report(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date = %s AND status = 'present'", (today,))
                today_sits = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date LIKE %s AND status = 'present'", (f"{month}%",))
                month_sits = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM attendance WHERE status = 'present'")
                total_sits = cur.fetchone()[0]

                cur.execute(
                    "SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days "
                    "FROM attendance WHERE attendance_date LIKE %s AND status = 'present' GROUP BY user_id, name ORDER BY sessions DESC, days DESC",
                    (f"{month}%",),
                )
                active_members = cur.fetchall()
            return today_sits, month_sits, total_sits, active_members
        finally:
            conn.close()

    today_sits, month_sits, total_sits, active_members = await asyncio.to_thread(_query)
    member_lines = "\n".join([f"• *{esc(name)}* — `{s} sits` \\(`{d}d`\\)" for name, s, d in active_members]) if active_members else "_No active check\\-ins yet this month\\._"

    msg = (
        f"📊 *GROUP RECAP \\({esc(month_name)}\\)*\n"
        f"────────────────────────────\n"
        f"• *Today's Sits:* `{today_sits}`\n"
        f"• *Monthly Volume:* `{month_sits} Sessions`\n"
        f"• *Active Members:* `{len(active_members)}`\n"
        f"• *Lifetime Community Sits:* `{total_sits}`\n\n"
        f"👥 *Active Roster*\n{member_lines}"
    )
    await reply(update_or_query, context, msg, reply_markup=build_main_keyboard())


async def handle_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    kb = await build_member_picker_keyboard("stats", current_user_id=user_id)
    await reply(update, context, "🔍 *Select a member to view Stats:*", reply_markup=kb)


async def handle_missed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = await build_member_picker_keyboard("missed")
    await reply(update, context, "🔍 *Select a member to view Analytics:*", reply_markup=kb)


async def handle_grid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = await build_member_picker_keyboard("grid")
    await reply(update, context, "🔍 *Select a member to view Calendar:*", reply_markup=kb)


async def button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    name = user.full_name or user.username or "Unknown"

    if data == "noop":
        await query.answer()
        return

    if data.startswith("picker:"):
        await query.answer()
        action = data.split(":")[1]
        titles = {
            "missed": "Missed Analytics",
            "grid": "Calendar Matrix",
            "stats": "Member Stats"
        }
        title = titles.get(action, "Member")
        kb = await build_member_picker_keyboard(action, current_user_id=user.id)
        await reply(query, context, f"🔍 *Select a member for {esc(title)}:*", reply_markup=kb)
        return

    if data.startswith("navgrid:"):
        await query.answer()
        _, target_user_id, year, month = data.split(":")
        await show_visual_grid(query, context, int(target_user_id), int(year), int(month))
        return

    if data.startswith("user:"):
        await query.answer()
        _, action, target_user_id = data.split(":")
        target_user_id = int(target_user_id)
        if action == "missed":
            await show_member_missed_analytics(query, context, target_user_id)
        elif action == "grid":
            await show_visual_grid(query, context, target_user_id)
        elif action == "stats":
            await show_member_stats_by_id(query, context, target_user_id)
        return

    if data.startswith("view:"):
        await query.answer()
        action = data.split(":")[1]
        if action == "leaderboard":
            await show_leaderboard(query, context)
        elif action == "report":
            await show_group_report(query, context)
        elif action == "menu":
            await start(query, context)
        return

    if data.startswith("attend:"):
        _, date_str, session = data.split(":")
        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏳ Saving Check-In...", callback_data="noop")]
            ])
        )
        await asyncio.sleep(0.2)

        success = await record_attendance(user.id, name, date_str, session)

        if success:
            streak = await calculate_streak(user.id)
            streak_text = f"{streak}d Streak! 🔥" if streak > 1 else "Checked In! ✨"
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ {streak_text}", callback_data="noop")]
                ])
            )
            await asyncio.sleep(0.8)
            await start(query, context)
        else:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ Already Checked In Today", callback_data="noop")]
                ])
            )
            await asyncio.sleep(1.0)
            await start(query, context)


# =========================================================
# SCHEDULED NOTIFICATIONS
# =========================================================

async def scheduled_community_prompt(context: ContextTypes.DEFAULT_TYPE):
    title = context.job.data
    today = get_current_date()
    msg = (
        f"🧘 *{esc(title)}*\n"
        f"`{esc(today)}` • `20m Session`\n"
        f"────────────────────────────\n"
        f"Take a breath and check in below:"
    )
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_main_keyboard(today),
    )


async def scheduled_unmarked_catchup(context: ContextTypes.DEFAULT_TYPE):
    title = context.job.data
    today = get_current_date()
    month = today[:7]

    def _query():
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT user_id, name FROM attendance 
                    WHERE attendance_date LIKE %s AND user_id NOT IN (
                        SELECT user_id FROM attendance WHERE attendance_date = %s AND status = 'present'
                    )
                """, (f"{month}%", today))
                return cur.fetchall()
        finally:
            conn.close()

    unmarked = await asyncio.to_thread(_query)
    if not unmarked:
        return

    names = ", ".join([esc(name) for _, name in unmarked])
    msg = (
        f"🔔 *{esc(title)}*\n"
        f"────────────────────────────\n"
        f"Pending practice check\\-ins for today:\n{names}"
    )
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_main_keyboard(today),
    )


# =========================================================
# WEB SERVER & ERROR HANDLING
# =========================================================

async def handle_ping(request):
    return web.Response(text="Bot is running.")


async def run_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health check server active on port {port}")
    return runner


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


async def main():
    web_runner = await run_web_server()
    await setup_database()

    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(error_handler)

    schedules = [
        (scheduled_community_prompt, time(5, 0, tzinfo=TIMEZONE), "Morning Meditation (5:00 AM)"),
        (scheduled_community_prompt, time(8, 30, tzinfo=TIMEZONE), "Mid-Morning Meditation (8:30 AM)"),
        (scheduled_community_prompt, time(13, 30, tzinfo=TIMEZONE), "Midday Pause (1:30 PM)"),
        (scheduled_community_prompt, time(18, 0, tzinfo=TIMEZONE), "Evening Meditation (6:00 PM)"),
        (scheduled_unmarked_catchup, time(19, 30, tzinfo=TIMEZONE), "Evening Catch-up (7:30 PM)"),
        (scheduled_unmarked_catchup, time(21, 30, tzinfo=TIMEZONE), "Night Reminder (9:30 PM)"),
        (scheduled_unmarked_catchup, time(23, 0, tzinfo=TIMEZONE), "Final Reminder (11:00 PM)"),
    ]
    for callback, schedule_time, title in schedules:
        app.job_queue.run_daily(callback, schedule_time, data=title)

    app.add_handler(CommandHandler(["start", "menu", "attendance"], start))
    app.add_handler(CommandHandler(["stats", "mystats", "my"], handle_stats_command))
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
    app.add_handler(CommandHandler("report", show_group_report))
    app.add_handler(CommandHandler("missed", handle_missed_command))
    app.add_handler(CommandHandler(["grid", "calendars"], handle_grid_command))
    app.add_handler(CommandHandler("id", lambda u, c: reply(u, c, f"Chat ID: `{esc(u.effective_chat.id)}`")))
    app.add_handler(CallbackQueryHandler(button_pressed))

    logger.info("Starting Telegram polling with Neon Postgres...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Idle loop that keeps the container and event loop alive
        try:
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            logger.info("Shutting down...")
            await app.updater.stop()
            await app.stop()
            await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated.")
