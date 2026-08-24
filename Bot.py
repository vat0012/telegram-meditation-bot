import asyncio
import calendar
import html
import logging
import os
from datetime import datetime, time, timedelta
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

# =========================================================
# BASIC SETTINGS
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dhyan_bot")

# Put these in your hosting platform's Environment Variables.
# Do NOT put real secrets directly into this file.
TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GROUP_ID = int(os.getenv("GROUP_ID", "-4721378655"))

TIMEZONE = ZoneInfo("Asia/Kolkata")
SESSION_MINUTES = 20

DB_POOL = None


# =========================================================
# SMALL HELPERS
# =========================================================

def today_date() -> str:
    """Return today's date in India (YYYY-MM-DD)."""
    return datetime.now(TIMEZONE).date().isoformat()


def safe_text(value: object) -> str:
    """Escape text safely for Telegram HTML messages."""
    return html.escape(str(value))


def display_name(user) -> str:
    """Get the nicest available name for a Telegram user."""
    return user.first_name or user.username or "Practitioner"


def get_title(total_sessions: int) -> str:
    """Give a simple title based on lifetime practice."""
    if total_sessions >= 150:
        return "Master of Stillness 🏔️"
    if total_sessions >= 75:
        return "Dhyan Practitioner 🌿"
    if total_sessions >= 30:
        return "Mindful Seeker 🌊"
    if total_sessions >= 10:
        return "Consistent Sitter 🌱"
    return "Beginner ✨"


# =========================================================
# DATABASE
# =========================================================

async def setup_database() -> None:
    """Connect to PostgreSQL and create the attendance table if needed."""
    global DB_POOL

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")

    clean_url = DATABASE_URL.replace("&channel_binding=require", "")
    clean_url = clean_url.replace("postgres://", "postgresql://")

    DB_POOL = await asyncpg.create_pool(
        dsn=clean_url,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                session TEXT NOT NULL,
                duration_minutes INT DEFAULT 20,
                UNIQUE(user_id, attendance_date, session)
            );

            CREATE INDEX IF NOT EXISTS idx_user_date
                ON attendance(user_id, attendance_date);

            CREATE INDEX IF NOT EXISTS idx_date_session
                ON attendance(attendance_date, session);
            """
        )

    logger.info("Database is ready.")


async def calculate_streak(user_id: int) -> int:
    """Count consecutive practice days, starting from today or yesterday."""
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT attendance_date
            FROM attendance
            WHERE user_id = $1
            ORDER BY attendance_date DESC
            """,
            user_id,
        )

    if not rows:
        return 0

    dates = {
        datetime.strptime(row["attendance_date"], "%Y-%m-%d").date()
        for row in rows
    }

    today = datetime.now(TIMEZONE).date()
    current = today if today in dates else today - timedelta(days=1)

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


def main_menu_keyboard(
    date_str: str | None = None,
    session: str = "daily",
    count: int = 0,
) -> InlineKeyboardMarkup:
    """Main menu shown under attendance messages."""
    target_date = date_str or today_date()
    count_text = f" ({count})" if count else ""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ Check in today{count_text}",
                    callback_data=f"attend:{target_date}:{session}",
                )
            ],
            [
                InlineKeyboardButton("👤 My stats", callback_data="menu:mystats"),
                InlineKeyboardButton("📅 My calendar", callback_data="menu:grid"),
            ],
            [
                InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
                InlineKeyboardButton("📊 Community", callback_data="menu:report"),
            ],
        ]
    )


def calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """Navigation buttons for the monthly calendar."""
    previous_month = month - 1 if month > 1 else 12
    previous_year = year if month > 1 else year - 1

    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "◀️ Previous",
                    callback_data=f"cal:{previous_year}:{previous_month:02d}",
                ),
                InlineKeyboardButton(
                    "Next ▶️",
                    callback_data=f"cal:{next_year}:{next_month:02d}",
                ),
            ],
            [InlineKeyboardButton("🏠 Main menu", callback_data="menu:main")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Main menu", callback_data="menu:main")]]
    )


def calendar_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📅 Open my calendar", callback_data="menu:grid")]]
    )


# =========================================================
# TELEGRAM MESSAGE HELPER
# =========================================================

async def send_message(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
) -> None:
    """Send a message whether the caller is a command or a button press."""
    try:
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return

        if getattr(update_or_query, "message", None):
            chat_id = update_or_query.message.chat_id
        else:
            chat_id = update_or_query.from_user.id

        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    except TelegramError as exc:
        logger.error("Could not send message: %s", exc)


# =========================================================
# HOME / WELCOME
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the main attendance screen."""
    current_date = today_date()

    async with DB_POOL.acquire() as conn:
        total_today = await conn.fetchval(
            "SELECT COUNT(*) FROM attendance WHERE attendance_date = $1",
            current_date,
        ) or 0

    text = (
        "🧘 <b>Dhyan Tracker</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Today:</b> {safe_text(current_date)}\n"
        f"⏱ <b>Practice:</b> {SESSION_MINUTES} minutes\n\n"
        "Use <b>Check in today</b> after completing your practice.\n"
        "You can also open your stats, calendar, or the community report below."
    )

    await send_message(
        update,
        context,
        text,
        reply_markup=main_menu_keyboard(current_date, "daily", total_today),
    )


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome new members and show them the check-in button."""
    current_date = today_date()

    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue

        name = display_name(member)
        text = (
            f"🙏 <b>Welcome, {safe_text(name)}!</b>\n\n"
            f"Your daily Dhyan practice is <b>{SESSION_MINUTES} minutes</b>.\n"
            "After you finish, tap the button below to record your attendance."
        )

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(current_date, "daily"),
        )


# =========================================================
# PERSONAL STATS
# =========================================================

async def send_user_stats(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> None:
    """Show the user's current month and lifetime practice."""
    user_id = user.id
    month_prefix = datetime.now(TIMEZONE).strftime("%Y-%m")

    async with DB_POOL.acquire() as conn:
        month_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS sessions,
                COUNT(DISTINCT attendance_date) AS days
            FROM attendance
            WHERE user_id = $1
              AND attendance_date LIKE $2
            """,
            user_id,
            f"{month_prefix}%",
        )

        lifetime_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS sessions,
                COUNT(DISTINCT attendance_date) AS days
            FROM attendance
            WHERE user_id = $1
            """,
            user_id,
        )

        monthly_ranks = await conn.fetch(
            """
            SELECT user_id, COUNT(*) AS sessions
            FROM attendance
            WHERE attendance_date LIKE $1
            GROUP BY user_id
            ORDER BY sessions DESC, user_id
            """,
            f"{month_prefix}%",
        )

    month_sessions = int(month_row["sessions"] or 0)
    month_days = int(month_row["days"] or 0)
    lifetime_sessions = int(lifetime_row["sessions"] or 0)
    lifetime_days = int(lifetime_row["days"] or 0)

    ranking_ids = [row["user_id"] for row in monthly_ranks]
    rank = ranking_ids.index(user_id) + 1 if user_id in ranking_ids else None

    streak = await calculate_streak(user_id)
    name = display_name(user)
    title = get_title(lifetime_sessions)
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    rank_text = f"#{rank}" if rank else "Not ranked yet"
    next_text = "Keep going — every session counts." if lifetime_sessions < 10 else "Great consistency. Keep building your streak."

    text = (
        "👤 <b>My Dhyan Stats</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧘 <b>{safe_text(name)}</b>\n"
        f"🌿 {safe_text(title)}\n\n"
        "<b>This month</b>\n"
        f"• Practice days: <b>{month_days}</b>\n"
        f"• Total sessions: <b>{month_sessions}</b>\n"
        f"• Monthly rank: <b>{safe_text(rank_text)}</b>\n\n"
        "<b>All time</b>\n"
        f"• Practice days: <b>{lifetime_days}</b>\n"
        f"• Total sessions: <b>{lifetime_sessions}</b>\n"
        f"• Current streak: <b>{streak} days 🔥</b>\n\n"
        f"💡 {safe_text(next_text)}"
    )

    await send_message(update_or_query, context, text, reply_markup=calendar_button())


# =========================================================
# CALENDAR
# =========================================================

async def send_calendar(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user,
    target_year: int | None = None,
    target_month: int | None = None,
    edit_message: bool = False,
) -> None:
    """Show one month of the user's attendance."""
    now = datetime.now(TIMEZONE).date()
    year = target_year or now.year
    month = target_month or now.month

    month_prefix = f"{year}-{month:02d}"
    month_name = datetime(year, month, 1).strftime("%B %Y")
    days_in_month = calendar.monthrange(year, month)[1]

    async with DB_POOL.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT attendance_date, duration_minutes
            FROM attendance
            WHERE user_id = $1
              AND attendance_date LIKE $2
            ORDER BY attendance_date ASC
            """,
            user.id,
            f"{month_prefix}%",
        )

    completed_days = set()
    total_minutes = 0

    for row in records:
        day = int(row["attendance_date"].split("-")[2])
        completed_days.add(day)
        total_minutes += row["duration_minutes"] or SESSION_MINUTES

    active_days = len(completed_days)
    percentage = round((active_days / days_in_month) * 100)

    # Compact calendar that works well inside Telegram.
    month_calendar = calendar.monthcalendar(year, month)
    lines = [
        f"     {month_name.upper()}",
        "Mo Tu We Th Fr Sa Su",
    ]

    for week in month_calendar:
        cells = []
        for day in week:
            if day == 0:
                cells.append("  ")
            elif day in completed_days:
                cells.append("✓ ")
            elif (
                year < now.year
                or (year == now.year and month < now.month)
                or (year == now.year and month == now.month and day <= now.day)
            ):
                cells.append("· ")
            else:
                cells.append(f"{day:02d}"[-2:])
        lines.append(" ".join(cells))

    calendar_text = "\n".join(lines)

    text = (
        "📅 <b>My Dhyan Calendar</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧘 {safe_text(display_name(user))}\n\n"
        f"<pre>{safe_text(calendar_text)}</pre>\n"
        f"✅ <b>{active_days}</b> of {days_in_month} days completed ({percentage}%)\n"
        f"⏱ <b>{total_minutes}</b> minutes practiced\n\n"
        "<b>How to read it:</b> ✓ = practiced  · = missed"
    )

    markup = calendar_keyboard(year, month)

    if edit_message and hasattr(update_or_query, "edit_message_text"):
        try:
            await update_or_query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except TelegramError as exc:
            logger.warning("Could not edit calendar message: %s", exc)

    await send_message(update_or_query, context, text, reply_markup=markup)


# =========================================================
# LEADERBOARD
# =========================================================

async def send_leaderboard(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the top practitioners for the current month."""
    month_prefix = datetime.now(TIMEZONE).strftime("%Y-%m")
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with DB_POOL.acquire() as conn:
        leaders = await conn.fetch(
            """
            SELECT
                name,
                COUNT(*) AS sessions,
                COUNT(DISTINCT attendance_date) AS days
            FROM attendance
            WHERE attendance_date LIKE $1
            GROUP BY user_id, name
            ORDER BY sessions DESC, days DESC, name ASC
            LIMIT 10
            """,
            f"{month_prefix}%",
        )

    if not leaders:
        text = (
            "🏆 <b>Leaderboard</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📅 {safe_text(month_name)}\n\n"
            "No practice has been recorded this month yet.\n"
            "Be the first to check in!"
        )
        await send_message(update_or_query, context, text, reply_markup=back_to_menu_keyboard())
        return

    medals = ["🥇", "🥈", "🥉"]
    rows = []

    for position, row in enumerate(leaders, start=1):
        medal = medals[position - 1] if position <= 3 else f"{position}."
        rows.append(
            f"{medal} <b>{safe_text(row['name'])}</b> — "
            f"{row['sessions']} sessions · {row['days']} days"
        )

    text = (
        "🏆 <b>Monthly Leaderboard</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 {safe_text(month_name)}\n\n"
        + "\n".join(rows)
        + "\n\n🌱 Consistency matters more than speed."
    )

    await send_message(update_or_query, context, text, reply_markup=back_to_menu_keyboard())


# =========================================================
# COMMUNITY REPORT
# =========================================================

async def send_group_report(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a simple community summary."""
    current_date = today_date()
    month_prefix = current_date[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with DB_POOL.acquire() as conn:
        today_sessions = await conn.fetchval(
            "SELECT COUNT(*) FROM attendance WHERE attendance_date = $1",
            current_date,
        ) or 0

        month_sessions = await conn.fetchval(
            "SELECT COUNT(*) FROM attendance WHERE attendance_date LIKE $1",
            f"{month_prefix}%",
        ) or 0

        lifetime_sessions = await conn.fetchval(
            "SELECT COUNT(*) FROM attendance"
        ) or 0

        active_members = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM (
                SELECT user_id
                FROM attendance
                WHERE attendance_date LIKE $1
                GROUP BY user_id
            ) AS members
            """,
            f"{month_prefix}%",
        ) or 0

    text = (
        "📊 <b>Community Report</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 {safe_text(month_name)}\n\n"
        f"👥 Active members: <b>{active_members}</b>\n"
        f"✅ Today's check-ins: <b>{today_sessions}</b>\n"
        f"🧘 This month's sessions: <b>{month_sessions}</b>\n"
        f"🌱 Lifetime sessions: <b>{lifetime_sessions}</b>\n\n"
        "Every check-in adds to the community's practice."
    )

    await send_message(update_or_query, context, text, reply_markup=back_to_menu_keyboard())


# =========================================================
# ATTENDANCE
# =========================================================

async def record_attendance(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record one attendance entry."""
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Something went wrong. Please try again.", show_alert=True)
        return

    _, attendance_date, session = parts
    user = query.from_user
    name = user.full_name or display_name(user)

    async with DB_POOL.acquire() as conn:
        try:
            existing = await conn.fetchval(
                """
                SELECT id
                FROM attendance
                WHERE user_id = $1
                  AND attendance_date = $2
                  AND session = $3
                """,
                user.id,
                attendance_date,
                session,
            )

            if existing:
                await query.answer(
                    "✅ You have already checked in today.",
                    show_alert=True,
                )
                return

            await conn.execute(
                """
                INSERT INTO attendance
                    (user_id, name, attendance_date, session, duration_minutes)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user.id,
                name,
                attendance_date,
                session,
                SESSION_MINUTES,
            )

            total_today = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM attendance
                WHERE attendance_date = $1
                  AND session = $2
                """,
                attendance_date,
                session,
            ) or 0

        except Exception as exc:
            logger.exception("Attendance insert failed: %s", exc)
            await query.answer(
                "⚠️ I could not save your attendance. Please try again.",
                show_alert=True,
            )
            return

    streak = await calculate_streak(user.id)
    message = f"✅ Check-in saved! Today: {total_today}"
    if streak > 1:
        message += f" · 🔥 {streak}-day streak"

    # Keep the button available so the menu remains useful.
    try:
        await query.edit_message_reply_markup(
            reply_markup=main_menu_keyboard(attendance_date, session, total_today)
        )
    except TelegramError:
        pass

    await query.answer(message, show_alert=False)


# =========================================================
# BUTTONS
# =========================================================

async def button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every inline button."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data.startswith("attend:"):
        await record_attendance(query, context)
        return

    if data.startswith("cal:"):
        try:
            _, year, month = data.split(":")
            await send_calendar(
                query,
                context,
                query.from_user,
                int(year),
                int(month),
                edit_message=True,
            )
        except ValueError:
            await query.answer("Could not open that month.", show_alert=True)
        return

    if data == "menu:mystats":
        await send_user_stats(query, context, query.from_user)
        return

    if data == "menu:grid":
        await send_calendar(query, context, query.from_user)
        return

    if data == "menu:leaderboard":
        await send_leaderboard(query, context)
        return

    if data == "menu:report":
        await send_group_report(query, context)
        return

    if data == "menu:main":
        current_date = today_date()
        text = (
            "🧘 <b>Dhyan Tracker</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose what you would like to see:\n\n"
            "✅ Check in after your practice\n"
            "👤 See your personal progress\n"
            "📅 See your monthly calendar\n"
            "🏆 See the monthly leaderboard\n"
            "📊 See the community summary"
        )
        await send_message(
            query,
            context,
            text,
            reply_markup=main_menu_keyboard(current_date, "daily"),
        )
        return


# =========================================================
# COMMANDS
# =========================================================

async def my_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_user_stats(update, context, update.effective_user)


async def my_calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_calendar(update, context, update.effective_user)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_leaderboard(update, context)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_group_report(update, context)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_message(
        update,
        context,
        f"🆔 <b>Chat ID</b>\n<code>{safe_text(update.effective_chat.id)}</code>",
    )


# =========================================================
# SCHEDULED GROUP MESSAGES
# =========================================================

async def scheduled_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a gentle practice reminder and pin it silently."""
    prompt_title = context.job.data[0]
    current_date = today_date()

    text = (
        f"🧘 <b>{safe_text(prompt_title)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 {safe_text(current_date)}\n"
        f"⏱ {SESSION_MINUTES} minutes\n\n"
        "Have you completed your Dhyan practice?\n"
        "Tap the button below to record it."
    )

    sent = await context.bot.send_message(
        chat_id=GROUP_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(current_date, "daily"),
    )

    try:
        await context.bot.pin_chat_message(
            chat_id=GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except TelegramError:
        logger.info("Could not pin reminder message.")


async def scheduled_catchup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remind members who have practiced before but have not checked in today."""
    alert_title = context.job.data[0]
    current_date = today_date()
    month_prefix = current_date[:7]

    async with DB_POOL.acquire() as conn:
        unmarked = await conn.fetch(
            """
            SELECT DISTINCT user_id, name
            FROM attendance
            WHERE attendance_date LIKE $1
              AND user_id NOT IN (
                  SELECT user_id
                  FROM attendance
                  WHERE attendance_date = $2
              )
            ORDER BY name ASC
            LIMIT 15
            """,
            f"{month_prefix}%",
            current_date,
        )

    if not unmarked:
        return

    member_lines = "\n".join(
        f"• {safe_text(row['name'])}" for row in unmarked
    )

    text = (
        f"🔔 <b>{safe_text(alert_title)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "These members have not checked in today:\n\n"
        f"{member_lines}\n\n"
        "If you have completed your practice, tap the button below to record it."
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(current_date, "daily"),
    )


# =========================================================
# HEALTH CHECK / WEB SERVER
# =========================================================

async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="Dhyan Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("Health server running on port %s", port)
    return runner


# =========================================================
# BOT COMMANDS
# =========================================================

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open the main menu"),
            BotCommand("my", "View my stats"),
            BotCommand("grid", "View my calendar"),
            BotCommand("leaderboard", "View the leaderboard"),
            BotCommand("report", "View the community report"),
            BotCommand("id", "Show chat ID"),
        ]
    )


# =========================================================
# START THE BOT
# =========================================================

async def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set.")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")

    await setup_database()
    web_runner = await start_web_server()

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # -----------------------------------------------------
    # Regular practice reminders (India time)
    # -----------------------------------------------------
    practice_reminders = [
        (time(5, 0, tzinfo=TIMEZONE), "Morning Dhyan"),
        (time(8, 30, tzinfo=TIMEZONE), "Mid-Morning Dhyan"),
        (time(13, 30, tzinfo=TIMEZONE), "Midday Dhyan"),
        (time(18, 0, tzinfo=TIMEZONE), "Evening Dhyan"),
    ]

    for schedule_time, title in practice_reminders:
        application.job_queue.run_daily(
            scheduled_prompt,
            time=schedule_time,
            data=(title,),
        )

    # -----------------------------------------------------
    # Evening attendance reminders
    # -----------------------------------------------------
    catchup_reminders = [
        (time(19, 30, tzinfo=TIMEZONE), "Evening attendance check"),
        (time(21, 30, tzinfo=TIMEZONE), "Night attendance reminder"),
        (time(23, 0, tzinfo=TIMEZONE), "Final attendance reminder"),
    ]

    for schedule_time, title in catchup_reminders:
        application.job_queue.run_daily(
            scheduled_catchup,
            time=schedule_time,
            data=(title,),
        )

    # -----------------------------------------------------
    # Commands
    # -----------------------------------------------------
    application.add_handler(CommandHandler(["start", "menu", "attendance"], start))
    application.add_handler(CommandHandler(["my", "myattendance", "mystats", "status"], my_stats_command))
    application.add_handler(CommandHandler(["grid", "mygrid"], my_calendar_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("id", show_id))

    # New members + inline buttons
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    application.add_handler(CallbackQueryHandler(button_pressed))

    try:
        async with application:
            await application.start()
            await application.bot.delete_webhook(drop_pending_updates=True)
            await application.updater.start_polling(drop_pending_updates=True)

            logger.info("Dhyan Bot is running.")

            while True:
                await asyncio.sleep(3600)

    finally:
        if DB_POOL:
            await DB_POOL.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
