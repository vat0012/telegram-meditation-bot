import asyncio
import calendar
from datetime import datetime, time, timedelta
import logging
import os
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "8046423951:AAFK9boL0QaXuidtpKvDmRYjj06txjYI01A")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_KCSP91Nqtzfk@ep-bold-hall-azziesr6-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
)
GROUP_ID = int(os.getenv("GROUP_ID", "-4721378655"))
TIMEZONE = ZoneInfo("Asia/Kolkata")
SESSION_MINUTES = 20

DB_POOL = None


def get_current_date() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def esc(text: object) -> str:
    return escape_markdown(str(text), version=2)


def get_zen_title(total_sessions: int) -> str:
    if total_sessions >= 150:
        return "Master of Stillness 🏔️"
    if total_sessions >= 75:
        return "Dhyan Practitioner 🌿"
    if total_sessions >= 30:
        return "Mindful Seeker 🌊"
    if total_sessions >= 10:
        return "Consistent Meditator 🌱"
    return "Mindful Beginner ✨"


# =========================================================
# DATABASE (POSTGRESQL / ASYNCPG)
# =========================================================

async def setup_database():
    global DB_POOL
    clean_url = DATABASE_URL.replace("&channel_binding=require", "").replace("postgres://", "postgresql://")
    DB_POOL = await asyncpg.create_pool(dsn=clean_url, min_size=1, max_size=10, command_timeout=30)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                session TEXT NOT NULL,
                duration_minutes INT DEFAULT 20,
                UNIQUE(user_id, attendance_date, session)
            );
            CREATE INDEX IF NOT EXISTS idx_user_date ON attendance(user_id, attendance_date);
            CREATE INDEX IF NOT EXISTS idx_date_session ON attendance(attendance_date, session);
        """)
    logger.info("Database schema verified.")


async def calculate_streak(user_id: int) -> int:
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT attendance_date 
            FROM attendance 
            WHERE user_id = $1 
            ORDER BY attendance_date DESC
        """, user_id)

    if not rows:
        return 0

    dates = {datetime.strptime(r["attendance_date"], "%Y-%m-%d").date() for r in rows}
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


# =========================================================
# KEYBOARDS & UI HELPERS
# =========================================================

def build_attendance_keyboard(date_str: str, session: str = "daily", count: int = 0) -> InlineKeyboardMarkup:
    count_label = f" ({count})" if count > 0 else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Mark Attendance{count_label}", callback_data=f"attend:{date_str}:{session}")],
        [
            InlineKeyboardButton("📊 Group Report", callback_data="menu:report"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard")
        ],
        [
            InlineKeyboardButton("👤 My Stats", callback_data="menu:mystats"),
            InlineKeyboardButton("📅 Monthly Grid", callback_data="menu:grid")
        ]
    ])


def build_main_menu_keyboard(date_str: str = None, session: str = "daily", count: int = 0) -> InlineKeyboardMarkup:
    target_date = date_str or get_current_date()
    count_label = f" ({count})" if count > 0 else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Mark Today's Attendance{count_label}", callback_data=f"attend:{target_date}:{session}")],
        [
            InlineKeyboardButton("👤 My Stats", callback_data="menu:mystats"),
            InlineKeyboardButton("📅 Monthly Grid", callback_data="menu:grid")
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
            InlineKeyboardButton("📊 Group Report", callback_data="menu:report")
        ]
    ])


def build_month_grid_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1

    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️ Prev Month", callback_data=f"cal:{prev_year}:{prev_month:02d}"),
            InlineKeyboardButton("Next Month ▶️", callback_data=f"cal:{next_year}:{next_month:02d}"),
        ],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")]
    ])


async def send_response(update_or_query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    try:
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
        else:
            chat_id = update_or_query.message.chat_id if update_or_query.message else update_or_query.from_user.id
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    except TelegramError as e:
        logger.error(f"Error sending message: {e}")


# =========================================================
# VISUAL CALENDAR, STATS, LEADERBOARD, REPORT
# =========================================================

async def send_visual_grid(update_or_query, context: ContextTypes.DEFAULT_TYPE, user, target_year: int = None, target_month: int = None, edit_message: bool = False):
    now = datetime.now(TIMEZONE).date()
    year = target_year or now.year
    month = target_month or now.month

    month_prefix = f"{year}-{month:02d}"
    month_dt = datetime(year, month, 1)
    month_name = month_dt.strftime("%B %Y")
    days_in_month = calendar.monthrange(year, month)[1]

    async with DB_POOL.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT attendance_date, session, duration_minutes 
            FROM attendance 
            WHERE user_id = $1 AND attendance_date LIKE $2
            ORDER BY attendance_date ASC, id ASC
            """,
            user.id, f"{month_prefix}%"
        )

    month_data = {}
    total_minutes = 0
    total_sessions = len(records)

    for r in records:
        day_num = int(r["attendance_date"].split("-")[2])
        if day_num not in month_data:
            month_data[day_num] = []
        month_data[day_num].append(r["session"])
        total_minutes += (r["duration_minutes"] or SESSION_MINUTES)

    attended_days_count = len(month_data)
    completion_rate = int((attended_days_count / days_in_month) * 100)

    cal = calendar.monthcalendar(year, month)
    lines = [
        "╔═════════════════════════════╗",
        f"║    {month_name.center(25)}║",
        "╠═════════════════════════════╣",
        "║  Mo  Tu  We  Th  Fr  Sa  Su ║",
        "╟─────────────────────────────╢"
    ]

    for week in cal:
        row_str = "║ "
        for day in week:
            if day == 0:
                cell = "    "
            elif day in month_data:
                cell = f"[{day:02d}]"
            elif (year < now.year) or (year == now.year and month < now.month) or (year == now.year and month == now.month and day <= now.day):
                cell = "  · "
            else:
                cell = f"  {day:02d}"
            row_str += cell
        row_str += " ║"
        lines.append(row_str)

    lines.append("╚═════════════════════════════╝")
    calendar_ascii = "\n".join(lines)

    log_lines = []
    if month_data:
        for day in sorted(month_data.keys()):
            count = len(month_data[day])
            count_label = f" \\({esc(count)} sits\\)" if count > 1 else ""
            log_lines.append(f"• `{day:02d} {esc(month_dt.strftime('%b'))}` ✅ Completed{count_label}")
        full_log_text = "\n".join(log_lines)
    else:
        full_log_text = "_No sessions recorded for this month\\._"

    name = user.first_name or user.username or "Member"

    msg = (
        f"📅 *FULL MONTH DHYAN RECORD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧘 Member: *{esc(name)}*\n"
        f"📆 Month: *{esc(month_name)}*\n\n"
        f"```text\n{calendar_ascii}\n```\n"
        f"📊 *Month Summary:*\n"
        f" └ Active Days: `{esc(attended_days_count)} / {days_in_month}` \\({esc(completion_rate)}\\%\\)\n"
        f" └ Total Sits: `{esc(total_sessions)}` \\| Time: `{esc(total_minutes)} mins`\n\n"
        f"📋 *Daily Dhyan Ledger:*\n"
        f"{full_log_text}\n\n"
        f"Legend: `[05]` Completed \\| ` · ` Missed \\| ` 12` Upcoming"
    )

    markup = build_month_grid_keyboard(year, month)

    if edit_message and hasattr(update_or_query, "edit_message_text"):
        try:
            await update_or_query.edit_message_text(text=msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            return
        except Exception as e:
            logger.warning(f"Could not edit grid message: {e}")

    await send_response(update_or_query, context, msg, reply_markup=markup)


async def send_user_stats(update_or_query, context: ContextTypes.DEFAULT_TYPE, user):
    user_id = user.id
    today = get_current_date()
    month = today[:7]

    async with DB_POOL.acquire() as conn:
        r_month = await conn.fetchrow(
            "SELECT COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days FROM attendance WHERE user_id = $1 AND attendance_date LIKE $2",
            user_id, f"{month}%"
        )
        month_sessions = r_month["sessions"] if r_month else 0
        month_days = r_month["days"] if r_month else 0

        r_all = await conn.fetchrow(
            "SELECT COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days FROM attendance WHERE user_id = $1",
            user_id
        )
        all_time_sessions = r_all["sessions"] if r_all else 0
        all_time_days = r_all["days"] if r_all else 0

        rank_rows = await conn.fetch("""
            SELECT user_id, COUNT(*) as sessions 
            FROM attendance WHERE attendance_date LIKE $1 
            GROUP BY user_id ORDER BY sessions DESC
        """, f"{month}%")
        rankings = [r["user_id"] for r in rank_rows]

    user_rank = (rankings.index(user_id) + 1) if user_id in rankings else "—"
    streak = await calculate_streak(user_id)
    name = user.first_name or user.username or "Practitioner"
    title = get_zen_title(all_time_sessions)

    stats_card = (
        f"┌──────────────────────────────┐\n"
        f"│      🧘 DHYAN PASSPORT       │\n"
        f"├──────────────────────────────┤\n"
        f"│                              │\n"
        f"│  • Active Streak : {streak:>4d} days │\n"
        f"│  • Month Rank    : #{str(user_rank):>3s}     │\n"
        f"│  • Month Days    : {month_days:>4d} days │\n"
        f"│  • Month Sits    : {month_sessions:>4d} sits │\n"
        f"│  • Total Sits    : {all_time_sessions:>4d} sits │\n"
        f"│                              │\n"
        f"└──────────────────────────────┘"
    )

    grid_button = InlineKeyboardMarkup([[InlineKeyboardButton("📅 View Full Month Dhyan Grid", callback_data="menu:grid")]])

    msg = (
        f"👤 *PRACTITIONER PROFILE*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧘 Member: *{esc(name)}*\n"
        f"🎖 Title: *{esc(title)}*\n"
        f"🔥 Active Streak: `{esc(streak)} days`\n\n"
        f"```text\n{stats_card}\n```\n"
        f"📊 *Milestone Progress:*\n"
        f"   └ `{esc(all_time_sessions)}` total 20\\-min sits completed\n"
        f"   └ `{esc(all_time_days)}` unique days of stillness"
    )
    await send_response(update_or_query, context, msg, reply_markup=grid_button)


async def send_leaderboard(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with DB_POOL.acquire() as conn:
        leaders = await conn.fetch("""
            SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days
            FROM attendance WHERE attendance_date LIKE $1 GROUP BY user_id, name ORDER BY sessions DESC, days DESC LIMIT 10
        """, f"{month}%")

    if not leaders:
        msg = f"🏆 *DHYAN LEADERBOARD*\n📅 *Month:* `{esc(month_name.upper())}`\n\n_No practice records found yet for this month\\._"
        await send_response(update_or_query, context, msg)
        return

    p1 = leaders[0]["name"] if len(leaders) > 0 else "---"
    p2 = leaders[1]["name"] if len(leaders) > 1 else "---"
    p3 = leaders[2]["name"] if len(leaders) > 2 else "---"

    p1_fmt = (p1[:10] + "…") if len(p1) > 10 else p1
    p2_fmt = (p2[:8] + "…") if len(p2) > 8 else p2
    p3_fmt = (p3[:8] + "…") if len(p3) > 8 else p3

    podium_card = (
        f"┌──────────────────────────────┐\n"
        f"│      🏆 MONTHLY PODIUM       │\n"
        f"├──────────────────────────────┤\n"
        f"│            🥇 1st            │\n"
        f"│          {p1_fmt.center(12)}        │\n"
        f"│                              │\n"
        f"│    🥈 2nd          🥉 3rd   │\n"
        f"│   {p2_fmt.center(10)}      {p3_fmt.center(10)} │\n"
        f"└──────────────────────────────┘"
    )

    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for rank, r in enumerate(leaders, start=1):
        badge = medals[rank - 1] if rank <= 3 else f"`{rank:02d}.`"
        rows.append(f"{badge} *{esc(r['name'])}*\n   └ `{esc(r['sessions'])}` sessions completed • `{esc(r['days'])}` days")

    msg = (
        f"🏆 *DHYAN PRACTICE LEADERBOARD*\n"
        f"📅 *Period:* `{esc(month_name.upper())}`\n\n"
        f"```text\n{podium_card}\n```\n"
        f"✨ *Top 10 Dedicated Practitioners:*\n\n" + "\n".join(rows)
    )
    await send_response(update_or_query, context, msg)


async def send_group_report(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with DB_POOL.acquire() as conn:
        today_sessions = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date = $1", today) or 0
        month_sessions = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date LIKE $1", f"{month}%") or 0
        all_time_sessions = await conn.fetchval("SELECT COUNT(*) FROM attendance") or 0

        active_members = await conn.fetch("""
            SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days
            FROM attendance WHERE attendance_date LIKE $1 GROUP BY user_id, name ORDER BY sessions DESC, days DESC
        """, f"{month}%")

    summary_card = (
        f"┌──────────────────────────────┐\n"
        f"│    🏛 DHYAN MONTHLY RECAP    │\n"
        f"├──────────────────────────────┤\n"
        f"│                              │\n"
        f"│  • Today Check-ins : {today_sessions:>4d}    │\n"
        f"│  • Month Sits      : {month_sessions:>4d}    │\n"
        f"│  • Active Members  : {len(active_members):>4d}    │\n"
        f"│  • All-Time Sits   : {all_time_sessions:>4d}    │\n"
        f"│                              │\n"
        f"└──────────────────────────────┘"
    )

    if active_members:
        member_rows = [
            f"{'🌿' if r <= 3 else '▫️'} *{esc(m['name'])}*\n   └ `{esc(m['sessions'])}` sessions • `{esc(m['days'])}` days active"
            for r, m in enumerate(active_members, start=1)
        ]
        roster_text = "\n".join(member_rows)
    else:
        roster_text = "_No active sits recorded yet this month\\._"

    msg = (
        f"🕯 *COMMUNITY DHYAN REPORT*\n"
        f"📅 *Period:* `{esc(month_name.upper())}`\n\n"
        f"```text\n{summary_card}\n```\n"
        f"✨ *Active Practitioners Roster:*\n\n{roster_text}"
    )
    await send_response(update_or_query, context, msg)


# =========================================================
# HANDLERS & CALLBACKS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    total = 0
    if DB_POOL:
        async with DB_POOL.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date = $1", today) or 0

    msg = (
        "✨ *20\\-Minute Daily Dhyan Tracker* ✨\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 *Date:* `{esc(today)}`\n"
        f"⏱ *Duration:* `20 Minutes`\n\n"
        "Mark your attendance directly below or explore your records:"
    )
    await send_response(update, context, msg, reply_markup=build_main_menu_keyboard(today, "daily", total))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def attendance_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    total = 0
    if DB_POOL:
        async with DB_POOL.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date = $1", today) or 0

    msg = f"🧘 *20\\-MINUTE DHYAN CHECK\\-IN*\n━━━━━━━━━━━━━━━━━━━━\n📅 *Date:* `{esc(today)}`\n⏱ *Duration:* `20 Minutes`\n\nTap below to mark attendance:"
    await send_response(update, context, msg, reply_markup=build_attendance_keyboard(today, "daily", total))


async def button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("menu:"):
        await query.answer()
        action = data.split(":")[1]
        if action == "mystats":
            await send_user_stats(query, context, query.from_user)
        elif action == "grid":
            await send_visual_grid(query, context, query.from_user)
        elif action == "leaderboard":
            await send_leaderboard(query, context)
        elif action == "report":
            await send_group_report(query, context)
        elif action == "mark_prompt":
            await attendance_prompt(query, context)
        elif action == "main":
            today = get_current_date()
            await send_response(query, context, "🎛 *Dhyan Control Center*", reply_markup=build_main_menu_keyboard(today, "daily"))
        return

    if data.startswith("cal:"):
        await query.answer()
        _, yr, mo = data.split(":")
        await send_visual_grid(
            update_or_query=query,
            context=context,
            user=query.from_user,
            target_year=int(yr),
            target_month=int(mo),
            edit_message=True
        )
        return

    if not data.startswith("attend:"):
        await query.answer()
        return

    parts = data.split(":")
    attendance_date, session = parts[1], parts[2]
    user = query.from_user
    name = user.full_name or user.username or "Unknown"

    async with DB_POOL.acquire() as conn:
        try:
            existing = await conn.fetchval(
                "SELECT id FROM attendance WHERE user_id = $1 AND attendance_date = $2 AND session = $3",
                user.id, attendance_date, session
            )
            if existing:
                await query.answer("⚠️ You have already checked in for today!", show_alert=True)
                return

            await conn.execute(
                "INSERT INTO attendance (user_id, name, attendance_date, session, duration_minutes) VALUES ($1, $2, $3, $4, $5)",
                user.id, name, attendance_date, session, SESSION_MINUTES
            )

            total = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date = $1 AND session = $2", attendance_date, session)
            streak = await calculate_streak(user.id)
            
            try:
                await query.edit_message_reply_markup(reply_markup=build_attendance_keyboard(attendance_date, session, total))
            except Exception:
                pass

            streak_text = f" 🔥 Streak: {streak} days!" if streak > 1 else ""
            await query.answer(f"✅ Attendance logged, {name}!{streak_text} (Total today: {total})", show_alert=False)
        except Exception as e:
            logger.error(f"Error in button_pressed: {e}")
            await query.answer("⚠️ Failed to record attendance.", show_alert=True)


async def my_attendance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await send_user_stats(update, context, update.effective_user)


async def grid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await send_visual_grid(update, context, update.effective_user)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_leaderboard(update, context)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_group_report(update, context)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_response(update, context, f"🆔 Chat ID: `{esc(update.effective_chat.id)}`")


# =========================================================
# SCHEDULED ROUTINES
# =========================================================

async def scheduled_community_prompt(context: ContextTypes.DEFAULT_TYPE):
    prompt_title, = context.job.data
    today = get_current_date()

    msg = (
        f"🧘 *{esc(prompt_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date: `{esc(today)}`\n"
        f"⏱ Duration: `20 Minutes`\n\n"
        f"Tap below to mark your 20\\-minute Dhyan practice:"
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_attendance_keyboard(today, "daily", 0),
    )


async def scheduled_unmarked_catchup(context: ContextTypes.DEFAULT_TYPE):
    alert_title, = context.job.data
    today = get_current_date()
    month = today[:7]

    async with DB_POOL.acquire() as conn:
        unmarked_members = await conn.fetch("""
            SELECT DISTINCT user_id, name 
            FROM attendance 
            WHERE attendance_date LIKE $1 
              AND user_id NOT IN (
                  SELECT user_id FROM attendance WHERE attendance_date = $2
              )
        """, f"{month}%", today)

    if not unmarked_members:
        return

    member_lines = "\n".join([f"• {esc(r['name'])}" for r in unmarked_members])

    msg = (
        f"🔔 *{esc(alert_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Unmarked Dhyan practitioners for today:\n\n"
        f"{member_lines}\n\n"
        f"Tap below to mark your practice:"
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_attendance_keyboard(today, "daily", 0),
    )


# =========================================================
# WEB SERVER & APP LIFECYCLE
# =========================================================

async def handle_ping(request):
    return web.Response(text="Bot is running smoothly 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health check running on port {port}")
    return runner


async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return

    await setup_database()
    web_runner = await start_web_server()

    app = Application.builder().token(TOKEN).build()

    # Daily community prompts (IST)
    prompts = [
        (time(5, 0, tzinfo=TIMEZONE), ("Morning Dawn Dhyan",)),
        (time(8, 30, tzinfo=TIMEZONE), ("Mid-Morning Dhyan Call",)),
        (time(13, 30, tzinfo=TIMEZONE), ("Midday Stillness Pause",)),
        (time(18, 0, tzinfo=TIMEZONE), ("Evening Sunset Dhyan",)),
    ]
    for schedule_time, data in prompts:
        app.job_queue.run_daily(scheduled_community_prompt, schedule_time, data=data)

    catchups = [
        (time(19, 30, tzinfo=TIMEZONE), ("Evening Dhyan Check",)),
        (time(21, 30, tzinfo=TIMEZONE), ("Night Attendance Reminder",)),
        (time(23, 0, tzinfo=TIMEZONE), ("Final Daily Call (11:00 PM)",)),
    ]
    for schedule_time, data in catchups:
        app.job_queue.run_daily(scheduled_unmarked_catchup, schedule_time, data=data)

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("attendance", attendance_prompt))
    app.add_handler(CommandHandler(["grid", "mygrid"], grid_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler(["my", "myattendance", "mystats", "status"], my_attendance_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(/my|/myattendance|/mystats|/status|/grid|/mygrid|my grid|my stats|my attendance)"), my_attendance_cmd))
    app.add_handler(CallbackQueryHandler(button_pressed))

    try:
        async with app:
            await app.start()
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Dhyan Bot started successfully.")
            while True:
                await asyncio.sleep(3600)
    finally:
        if DB_POOL:
            await DB_POOL.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
