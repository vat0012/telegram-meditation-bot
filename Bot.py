import asyncio
import calendar
from datetime import datetime, time, timedelta
import logging
import os
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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


def get_title(sessions: int) -> str:
    if sessions >= 150:
        return "Master of Stillness 🏔️"
    if sessions >= 75:
        return "Dhyan Practitioner 🌿"
    if sessions >= 30:
        return "Mindful Seeker 🌊"
    if sessions >= 10:
        return "Consistent Sitter 🌱"
    return "Beginner ✨"


# =========================================================
# DATABASE
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
    logger.info("Database schema initialized.")


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

    current = today if today in dates else yesterday
    if current not in dates:
        return 0

    streak = 0
    while current in dates:
        streak += 1
        current -= timedelta(days=1)
    return streak


# =========================================================
# KEYBOARDS
# =========================================================

def build_menu_keyboard(date_str: str = None, session: str = "daily", count: int = 0) -> InlineKeyboardMarkup:
    target_date = date_str or get_current_date()
    count_tag = f" ({count})" if count > 0 else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Check In Today{count_tag}", callback_data=f"attend:{target_date}:{session}")],
        [
            InlineKeyboardButton("👤 My Stats", callback_data="menu:mystats"),
            InlineKeyboardButton("📅 Calendar", callback_data="menu:grid")
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
            InlineKeyboardButton("📊 Community", callback_data="menu:report")
        ]
    ])


def build_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    prev_mo = month - 1 if month > 1 else 12
    prev_yr = year if month > 1 else year - 1
    next_mo = month + 1 if month < 12 else 1
    next_yr = year if month < 12 else year + 1

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️ Prev", callback_data=f"cal:{prev_yr}:{prev_mo:02d}"),
            InlineKeyboardButton("Next ▶️", callback_data=f"cal:{next_yr}:{next_mo:02d}"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main")]
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
# AESTHETIC CARDS & REPORTS
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
            ORDER BY attendance_date ASC
            """,
            user.id, f"{month_prefix}%"
        )

    month_data = {}
    total_minutes = 0
    for r in records:
        day_num = int(r["attendance_date"].split("-")[2])
        month_data.setdefault(day_num, []).append(r["session"])
        total_minutes += (r["duration_minutes"] or SESSION_MINUTES)

    active_days = len(month_data)
    rate = int((active_days / days_in_month) * 100)

    # Minimalist ASCII Calendar
    cal = calendar.monthcalendar(year, month)
    header = f"  {month_name.upper()}"
    lines = [
        "┌────────────────────────────┐",
        f"│{header.center(28)}│",
        "├────────────────────────────┤",
        "│  Mo  Tu  We  Th  Fr  Sa  Su│",
        "├────────────────────────────┤"
    ]

    for week in cal:
        row = "│ "
        for day in week:
            if day == 0:
                cell = "   "
            elif day in month_data:
                cell = f"[{day:02d}]"
            elif (year < now.year) or (year == now.year and month < now.month) or (year == now.year and month == now.month and day <= now.day):
                cell = " · "
            else:
                cell = f" {day:02d}"
            row += cell
        row += " │"
        lines.append(row)

    lines.append("└────────────────────────────┘")
    calendar_ascii = "\n".join(lines)

    name = user.first_name or user.username or "Practitioner"
    msg = (
        f"📅 *DHYAN CALENDAR*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧘 *{esc(name)}* • `{esc(month_name)}`\n\n"
        f"```text\n{calendar_ascii}\n```\n"
        f"📊 *Summary:* `{active_days}/{days_in_month} Days` \\({rate}\\%\\) • `{total_minutes} Mins`\n\n"
        f"Legend: `[05]` Done \\| ` · ` Missed \\| `12` Future"
    )

    markup = build_calendar_keyboard(year, month)
    if edit_message and hasattr(update_or_query, "edit_message_text"):
        try:
            await update_or_query.edit_message_text(text=msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            return
        except Exception:
            pass

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
        r_all = await conn.fetchrow(
            "SELECT COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days FROM attendance WHERE user_id = $1",
            user_id
        )
        rank_rows = await conn.fetch("""
            SELECT user_id, COUNT(*) as sessions 
            FROM attendance WHERE attendance_date LIKE $1 
            GROUP BY user_id ORDER BY sessions DESC
        """, f"{month}%")
        rankings = [r["user_id"] for r in rank_rows]

    month_sits = r_month["sessions"] if r_month else 0
    month_days = r_month["days"] if r_month else 0
    all_sits = r_all["sessions"] if r_all else 0
    all_days = r_all["days"] if r_all else 0
    user_rank = (rankings.index(user_id) + 1) if user_id in rankings else "—"

    streak = await calculate_streak(user_id)
    name = user.first_name or user.username or "Practitioner"
    title = get_title(all_sits)

    card = (
        f"┌────────────────────────────┐\n"
        f"│       DHYAN PASSPORT       │\n"
        f"├────────────────────────────┤\n"
        f"│  • Active Streak : {streak:>3d} days │\n"
        f"│  • Monthly Rank  : #{str(user_rank):>3s}     │\n"
        f"│  • Month Active  : {month_days:>3d} days │\n"
        f"│  • Total Sits    : {all_sits:>3d} sits │\n"
        f"└────────────────────────────┘"
    )

    grid_btn = InlineKeyboardMarkup([[InlineKeyboardButton("📅 View Calendar Grid", callback_data="menu:grid")]])
    msg = (
        f"👤 *PRACTITIONER PROFILE*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧘 *{esc(name)}* • _{esc(title)}_\n"
        f"🔥 Streak: *`{esc(streak)} Days`*\n\n"
        f"```text\n{card}\n```\n"
        f"✨ Lifetime: `{esc(all_sits)}` sits across `{esc(all_days)}` unique days\\."
    )
    await send_response(update_or_query, context, msg, reply_markup=grid_btn)


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
        msg = f"🏆 *LEADERBOARD* • `{esc(month_name)}`\n\n_No practice recorded yet for this month\\._"
        await send_response(update_or_query, context, msg)
        return

    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for rank, r in enumerate(leaders, start=1):
        tag = medals[rank - 1] if rank <= 3 else f"`{rank:02d}.`"
        rows.append(f"{tag} *{esc(r['name'])}* — `{esc(r['sessions'])} sits` \\({esc(r['days'])}d\\)")

    msg = (
        f"🏆 *MONTHLY LEADERBOARD*\n"
        f"📅 `{esc(month_name.upper())}`\n"
        f"━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(rows)
    )
    await send_response(update_or_query, context, msg)


async def send_group_report(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with DB_POOL.acquire() as conn:
        today_sits = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date = $1", today) or 0
        month_sits = await conn.fetchval("SELECT COUNT(*) FROM attendance WHERE attendance_date LIKE $1", f"{month}%") or 0
        all_sits = await conn.fetchval("SELECT COUNT(*) FROM attendance") or 0
        active_members = await conn.fetch("""
            SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days
            FROM attendance WHERE attendance_date LIKE $1 GROUP BY user_id, name ORDER BY sessions DESC, days DESC
        """, f"{month}%")

    card = (
        f"┌────────────────────────────┐\n"
        f"│      COMMUNITY RECAP       │\n"
        f"├────────────────────────────┤\n"
        f"│  • Today Check-ins : {today_sits:>4d}  │\n"
        f"│  • Month Sits      : {month_sits:>4d}  │\n"
        f"│  • Active Members  : {len(active_members):>4d}  │\n"
        f"│  • Total Lifetime  : {all_sits:>4d}  │\n"
        f"└────────────────────────────┘"
    )

    msg = (
        f"📊 *COMMUNITY REPORT*\n"
        f"📅 `{esc(month_name.upper())}`\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"```text\n{card}\n```"
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
        f"🧘 *DHYAN TRACKER*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 *Date:* `{esc(today)}`\n"
        f"⏱ *Session:* `20 Minutes`\n\n"
        f"Check in for today's practice or view records:"
    )
    await send_response(update, context, msg, reply_markup=build_menu_keyboard(today, "daily", total))


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    for new_member in update.message.new_chat_members:
        if new_member.id != context.bot.id:
            name = new_member.first_name or "Practitioner"
            msg = f"🙏 Welcome, *{esc(name)}*\\!\n\nMark your daily 20\\-minute Dhyan sit below:"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=build_menu_keyboard(today, "daily"))


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
        elif action == "main":
            today = get_current_date()
            await send_response(query, context, "🎛 *Dhyan Control Center*", reply_markup=build_menu_keyboard(today, "daily"))
        return

    if data.startswith("cal:"):
        await query.answer()
        _, yr, mo = data.split(":")
        await send_visual_grid(query, context, query.from_user, int(yr), int(mo), edit_message=True)
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
                await query.edit_message_reply_markup(reply_markup=build_menu_keyboard(attendance_date, session, total))
            except Exception:
                pass

            streak_txt = f" 🔥 Streak: {streak} days!" if streak > 1 else ""
            await query.answer(f"✅ Checked in, {name}!{streak_txt} (Total today: {total})", show_alert=False)
        except Exception as e:
            logger.error(f"Error in button_pressed: {e}")
            await query.answer("⚠️ Failed to record attendance.", show_alert=True)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_response(update, context, f"🆔 Chat ID: `{esc(update.effective_chat.id)}`")


# =========================================================
# SCHEDULED PROMPTS (SILENTLY AUTO-PINNED)
# =========================================================

async def scheduled_prompt(context: ContextTypes.DEFAULT_TYPE):
    prompt_title, = context.job.data
    today = get_current_date()

    msg = (
        f"🧘 *{esc(prompt_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date: `{esc(today)}` • `20 Minutes`\n\n"
        f"Tap below to mark your practice:"
    )

    sent = await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_menu_keyboard(today, "daily", 0),
    )

    try:
        await context.bot.pin_chat_message(chat_id=GROUP_ID, message_id=sent.message_id, disable_notification=True)
    except Exception:
        pass


async def scheduled_catchup(context: ContextTypes.DEFAULT_TYPE):
    alert_title, = context.job.data
    today = get_current_date()
    month = today[:7]

    async with DB_POOL.acquire() as conn:
        unmarked = await conn.fetch("""
            SELECT DISTINCT user_id, name 
            FROM attendance 
            WHERE attendance_date LIKE $1 
              AND user_id NOT IN (
                  SELECT user_id FROM attendance WHERE attendance_date = $2
              )
        """, f"{month}%", today)

    if not unmarked:
        return

    member_lines = "\n".join([f"• {esc(r['name'])}" for r in unmarked[:15]])
    msg = (
        f"🔔 *{esc(alert_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Unmarked practitioners today:\n\n"
        f"{member_lines}\n\n"
        f"Tap below to log your sit:"
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_menu_keyboard(today, "daily", 0),
    )


# =========================================================
# LIFECYCLE & WEB SERVER
# =========================================================

async def handle_ping(request):
    return web.Response(text="Dhyan Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server running on port {port}")
    return runner


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Open Dhyan check-in menu"),
        BotCommand("my", "My practice stats & streak"),
        BotCommand("grid", "View monthly calendar"),
        BotCommand("leaderboard", "Top practitioners"),
        BotCommand("report", "Community recap"),
    ])


async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is not set!")
        return

    await setup_database()
    web_runner = await start_web_server()

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Scheduled Prompts (IST)
    prompts = [
        (time(5, 0, tzinfo=TIMEZONE), ("Morning Dawn Dhyan",)),
        (time(8, 30, tzinfo=TIMEZONE), ("Mid-Morning Dhyan Call",)),
        (time(13, 30, tzinfo=TIMEZONE), ("Midday Stillness Pause",)),
        (time(18, 0, tzinfo=TIMEZONE), ("Evening Sunset Dhyan",)),
    ]
    for schedule_time, data in prompts:
        app.job_queue.run_daily(scheduled_prompt, schedule_time, data=data)

    catchups = [
        (time(19, 30, tzinfo=TIMEZONE), ("Evening Dhyan Check",)),
        (time(21, 30, tzinfo=TIMEZONE), ("Night Attendance Reminder",)),
        (time(23, 0, tzinfo=TIMEZONE), ("Final Call (11:00 PM)",)),
    ]
    for schedule_time, data in catchups:
        app.job_queue.run_daily(scheduled_catchup, schedule_time, data=data)

    # Handlers
    app.add_handler(CommandHandler(["start", "menu", "attendance"], start))
    app.add_handler(CommandHandler(["grid", "mygrid"], lambda u, c: send_visual_grid(u, c, u.effective_user) if u.effective_user else None))
    app.add_handler(CommandHandler("leaderboard", send_leaderboard))
    app.add_handler(CommandHandler("report", send_group_report))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler(["my", "myattendance", "mystats", "status"], lambda u, c: send_user_stats(u, c, u.effective_user) if u.effective_user else None))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(/my|/myattendance|/mystats|/status|/grid|/mygrid|my grid|my stats|my attendance)"), lambda u, c: send_user_stats(u, c, u.effective_user) if u.effective_user else None))
    app.add_handler(CallbackQueryHandler(button_pressed))

    try:
        async with app:
            await app.start()
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Dhyan Bot running smoothly.")
            while True:
                await asyncio.sleep(3600)
    finally:
        if DB_POOL:
            await DB_POOL.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
