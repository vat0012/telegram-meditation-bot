import asyncio
import calendar
from datetime import datetime, time, timedelta
import logging
import os
from zoneinfo import ZoneInfo

import aiosqlite
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# BOT CONFIGURATION
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = int(os.getenv("GROUP_ID", "-1001234567890"))
DB_NAME = "attendance.db"
TIMEZONE = ZoneInfo("Asia/Kolkata")
SESSION_MINUTES = 20


def get_current_date() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def esc(text: object) -> str:
    return escape_markdown(str(text), version=2)


def get_zen_title(total_sessions: int) -> str:
    if total_sessions >= 150:
        return "Master of Stillness 🏔️"
    if total_sessions >= 75:
        return "Zen Practitioner 🌿"
    if total_sessions >= 30:
        return "Mindful Seeker 🌊"
    if total_sessions >= 10:
        return "Consistent Meditator 🌱"
    return "Mindful Beginner ✨"


# =========================================================
# DATABASE OPERATIONS (ASYNC)
# =========================================================

async def setup_database():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                session TEXT NOT NULL,
                duration_minutes INTEGER DEFAULT 20,
                UNIQUE(user_id, attendance_date, session)
            )
        """)
        # Create indexes for instant queries as database grows
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_date ON attendance(user_id, attendance_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_date_session ON attendance(attendance_date, session)")
        await db.commit()


async def calculate_streak(user_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT DISTINCT attendance_date 
            FROM attendance 
            WHERE user_id = ? 
            ORDER BY attendance_date DESC
        """, (user_id,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return 0

    dates = {datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows}
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
        [
            InlineKeyboardButton(f"✅ Instant Check-In{count_label}", callback_data=f"attend:{date_str}:{session}"),
            InlineKeyboardButton("⏳ Start 20-Min Timer", callback_data=f"timer:start:{session}")
        ],
        [
            InlineKeyboardButton("📊 Group Report", callback_data="menu:report"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard")
        ]
    ])


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 My Stats", callback_data="menu:mystats"),
            InlineKeyboardButton("📅 Monthly Grid", callback_data="menu:grid")
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
            InlineKeyboardButton("📊 Group Report", callback_data="menu:report")
        ],
        [InlineKeyboardButton("🧘 Practice Options", callback_data="menu:mark_prompt")]
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

async def send_visual_grid(update_or_query, context: ContextTypes.DEFAULT_TYPE, user):
    today = datetime.now(TIMEZONE).date()
    month_prefix = today.isoformat()[:7]
    month_name = today.strftime("%B %Y")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT attendance_date FROM attendance WHERE user_id = ? AND attendance_date LIKE ?",
            (user.id, f"{month_prefix}%")
        ) as cursor:
            rows = await cursor.fetchall()
            attended_days = {datetime.strptime(r[0], "%Y-%m-%d").date().day for r in rows}

    cal = calendar.monthcalendar(today.year, today.month)
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
            elif day in attended_days:
                cell = f"[{day:02d}]"
            elif day <= today.day:
                cell = "  · "
            else:
                cell = f"  {day:02d}"
            row_str += cell
        row_str += " ║"
        lines.append(row_str)

    lines.append("╚═════════════════════════════╝")
    calendar_ascii = "\n".join(lines)
    name = user.first_name or user.username or "Member"

    msg = (
        f"📅 *PRACTICE CALENDAR*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧘 Member: *{esc(name)}*\n"
        f"✨ Completed: *{esc(len(attended_days))} days*\n\n"
        f"```text\n{calendar_ascii}\n```\n"
        f"Legend: `[05]` Completed \\| ` · ` Missed \\| ` 12` Upcoming"
    )
    await send_response(update_or_query, context, msg)


async def send_user_stats(update_or_query, context: ContextTypes.DEFAULT_TYPE, user):
    user_id = user.id
    today = get_current_date()
    month = today[:7]

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT attendance_date) FROM attendance WHERE user_id = ? AND attendance_date LIKE ?",
            (user_id, f"{month}%")
        ) as cursor:
            month_sessions, month_days = await cursor.fetchone()

        async with db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT attendance_date) FROM attendance WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            all_time_sessions, all_time_days = await cursor.fetchone()

        async with db.execute("""
            SELECT user_id, COUNT(*) as sessions 
            FROM attendance WHERE attendance_date LIKE ? 
            GROUP BY user_id ORDER BY sessions DESC
        """, (f"{month}%",)) as cursor:
            rankings = [row[0] for row in await cursor.fetchall()]

    user_rank = (rankings.index(user_id) + 1) if user_id in rankings else "—"
    streak = await calculate_streak(user_id)
    name = user.first_name or user.username or "Practitioner"
    title = get_zen_title(all_time_sessions)

    stats_card = (
        f"┌──────────────────────────────┐\n"
        f"│   🧘 MINDFULNESS PASSPORT    │\n"
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

    grid_button = InlineKeyboardMarkup([[InlineKeyboardButton("📅 View My Practice Calendar", callback_data="menu:grid")]])

    msg = (
        f"👤 *PRACTITIONER PROFILE*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧘 Member: *{esc(name)}*\n"
        f"🎖 Title: *{esc(title)}*\n"
        f"🔥 Active Streak: `{esc(streak)} days`\n\n"
        f"```text\n{stats_card}\n```\n"
        f"📊 *Milestone Progress:*\n"
        f"   └ `{esc(all_time_sessions)}` total 20\\-min sits completed\n"
        f"   └ `{esc(all_time_days)}` unique days of stillness\n\n"
        f"🕊 _Each mindful sit waters the seed of clarity\\._"
    )
    await send_response(update_or_query, context, msg, reply_markup=grid_button)


async def send_leaderboard(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days
            FROM attendance WHERE attendance_date LIKE ? GROUP BY user_id ORDER BY sessions DESC, days DESC LIMIT 10
        """, (f"{month}%",)) as cursor:
            leaders = await cursor.fetchall()

    if not leaders:
        msg = f"🏆 *SANGHA LEADERBOARD*\n📅 *Month:* `{esc(month_name.upper())}`\n\n_No practice records found yet for this month\\._"
        await send_response(update_or_query, context, msg)
        return

    p1 = leaders[0][0] if len(leaders) > 0 else "---"
    p2 = leaders[1][0] if len(leaders) > 1 else "---"
    p3 = leaders[2][0] if len(leaders) > 2 else "---"

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
    for rank, (name, s_count, d_count) in enumerate(leaders, start=1):
        badge = medals[rank - 1] if rank <= 3 else f"`{rank:02d}.`"
        rows.append(f"{badge} *{esc(name)}*\n   └ `{esc(s_count)}` sessions completed • `{esc(d_count)}` days")

    msg = (
        f"🏆 *SANGHA PRACTICE LEADERBOARD*\n"
        f"📅 *Period:* `{esc(month_name.upper())}`\n\n"
        f"```text\n{podium_card}\n```\n"
        f"✨ *Top 10 Dedicated Practitioners:*\n\n" + "\n".join(rows) +
        f"\n\n🌊 _Praise for walking the path with dedication\\._"
    )
    await send_response(update_or_query, context, msg)


async def send_group_report(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    month = today[:7]
    month_name = datetime.now(TIMEZONE).strftime("%B %Y")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date = ?", (today,)) as cursor:
            today_sessions = (await cursor.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date LIKE ?", (f"{month}%",)) as cursor:
            month_sessions = (await cursor.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM attendance") as cursor:
            all_time_sessions = (await cursor.fetchone())[0]

        async with db.execute("""
            SELECT name, COUNT(*) as sessions, COUNT(DISTINCT attendance_date) as days
            FROM attendance WHERE attendance_date LIKE ? GROUP BY user_id ORDER BY sessions DESC, days DESC
        """, (f"{month}%",)) as cursor:
            active_members = await cursor.fetchall()

    summary_card = (
        f"┌──────────────────────────────┐\n"
        f"│    🏛 SANGHA MONTHLY RECAP   │\n"
        f"├──────────────────────────────┤\n"
        f"│                              │\n"
        f"│  • Today Check-ins : {today_sessions:>4d}    │\n"
        f"│  • Month Sits      : {month_sessions:>4d}    │\n"
        f"│  • Active Sangha   : {len(active_members):>4d}    │\n"
        f"│  • All-Time Sits   : {all_time_sessions:>4d}    │\n"
        f"│                              │\n"
        f"└──────────────────────────────┘"
    )

    if active_members:
        member_rows = [
            f"{'🌿' if r <= 3 else '▫️'} *{esc(name)}*\n   └ `{esc(s)}` sessions • `{esc(d)}` days active"
            for r, (name, s, d) in enumerate(active_members, start=1)
        ]
        roster_text = "\n".join(member_rows)
    else:
        roster_text = "_No active sits recorded yet this month\\._"

    msg = (
        f"🕯 *COMMUNITY PRACTICE REPORT*\n"
        f"📅 *Period:* `{esc(month_name.upper())}`\n\n"
        f"```text\n{summary_card}\n```\n"
        f"✨ *Active Practitioners Roster:*\n\n{roster_text}\n\n"
        f"🕊 _Consistency is the seed of deep peace\\._"
    )
    await send_response(update_or_query, context, msg)


# =========================================================
# AESTHETIC CLOCK & RATE-LIMITED TIMER
# =========================================================

def get_mindful_phase(percent: int) -> str:
    if percent < 20:
        return "🌱 Settling the Breath"
    if percent < 50:
        return "🌊 Entering Stillness"
    if percent < 85:
        return "🏔️ Deep Awareness"
    return "✨ Gentle Integration"


def render_clock_canvas(name: str, mins: int, secs: int, percent: int) -> str:
    bar_width = 20
    filled_units = int((percent / 100) * bar_width)
    if filled_units >= bar_width:
        bar = "━━━━━━━━━━━━━━━━━━━━"
    elif filled_units == 0:
        bar = "╾───────────────────"
    else:
        bar = "━" * (filled_units - 1) + "╾" + "─" * (bar_width - filled_units)

    phase = get_mindful_phase(percent)
    canvas = (
        f"┌──────────────────────────────┐\n"
        f"│      🧘 MEDITATION SANGHA    │\n"
        f"├──────────────────────────────┤\n"
        f"│                              │\n"
        f"│           {mins:02d} : {secs:02d}            │\n"
        f"│                              │\n"
        f"│   [{bar}]   │\n"
        f"│                              │\n"
        f"│      Progress : {percent:>3d}%          │\n"
        f"└──────────────────────────────┘"
    )
    return (
        f"🕯 *{esc(name.upper())}'S PRACTICE ROOM*\n\n"
        f"```text\n{canvas}\n```\n"
        f"🕊 *State:* _{esc(phase)}_\n"
        f"🤫 _Hold stillness in the breath and posture\\._"
    )


async def run_live_timer(chat_id: int, user, session: str, context: ContextTypes.DEFAULT_TYPE):
    total_seconds = SESSION_MINUTES * 60
    # Update every 60 seconds to prevent Telegram 429 Flood Wait rate limits
    update_interval = 60
    user_display = user.first_name or user.username or "Practitioner"

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=render_clock_canvas(user_display, SESSION_MINUTES, 0, 0),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    remaining = total_seconds
    while remaining > 0:
        await asyncio.sleep(update_interval)
        remaining -= update_interval
        mins, secs = divmod(max(remaining, 0), 60)
        percent = int(((total_seconds - remaining) / total_seconds) * 100)

        if remaining > 0:
            try:
                await msg.edit_text(
                    text=render_clock_canvas(user_display, mins, secs, percent),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                pass

    today = get_current_date()
    name = user.full_name or user.username or "Unknown"

    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO attendance (user_id, name, attendance_date, session, duration_minutes) VALUES (?, ?, ?, ?, ?)",
                (user.id, name, today, session, SESSION_MINUTES),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass

    streak = await calculate_streak(user.id)
    streak_line = f"🔥 *Active Practice Streak:* `{streak} days`\n" if streak > 1 else ""
    completion_canvas = (
        f"┌──────────────────────────────┐\n"
        f"│      🔔 SESSION COMPLETE     │\n"
        f"├──────────────────────────────┤\n"
        f"│                              │\n"
        f"│           20 : 00            │\n"
        f"│                              │\n"
        f"│   [━━━━━━━━━━━━━━━━━━━━]   │\n"
        f"│                              │\n"
        f"│      Mindful Sit : 100%      │\n"
        f"└──────────────────────────────┘"
    )
    await msg.edit_text(
        text=f"🕊 *PEACEFUL PRACTICE CONCLUDED*\n\n```text\n{completion_canvas}\n```\n✨ Deep gratitude, *{esc(user_display)}*\\.\n✅ Today's session logged\\.\n{streak_line}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# =========================================================
# HANDLERS & CALLBACKS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✨ *20\\-Minute Daily Meditation Tracker* ✨\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Track daily sessions, launch live timers, view monthly grids, and build streaks\\.\n\n"
        "📌 *Quick Commands:*\n"
        "• `/attendance` — Post 20m check\\-in prompt\n"
        "• `/my` — Personal stats & streak\n"
        "• `/grid` — View monthly practice calendar\n"
        "• `/leaderboard` — Top practitioners\n"
        "• `/report` — Community summary\n"
        "• `/menu` — Interactive control panel"
    )
    await send_response(update, context, msg, reply_markup=build_main_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_response(update, context, "🎛 *Control Center*\nSelect an option below:", reply_markup=build_main_menu_keyboard())


async def attendance_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_current_date()
    msg = f"🧘 *20\\-MINUTE PRACTICE CHECK\\-IN*\n━━━━━━━━━━━━━━━━━━━━\n📅 *Date:* `{esc(today)}`\n⏱ *Duration:* `20 Minutes`\n\nSelect an option below:"
    await send_response(update, context, msg, reply_markup=build_attendance_keyboard(today, "daily", 0))


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
            today = get_current_date()
            await send_response(query, context, "🧘 *Practice Check\\-In:*\nChoose instant check\\-in or launch a timer:", reply_markup=build_attendance_keyboard(today, "daily", 0))
        return

    if data.startswith("timer:start:"):
        session = data.split(":")[2]
        await query.answer("🧘 Launching 20-minute meditation countdown...")
        asyncio.create_task(run_live_timer(query.message.chat_id, query.from_user, session, context))
        return

    if not data.startswith("attend:"):
        await query.answer()
        return

    parts = data.split(":")
    attendance_date, session = parts[1], parts[2]
    user = query.from_user
    name = user.full_name or user.username or "Unknown"

    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO attendance (user_id, name, attendance_date, session, duration_minutes) VALUES (?, ?, ?, ?, ?)",
                (user.id, name, attendance_date, session, SESSION_MINUTES),
            )
            await db.commit()

            async with db.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date = ? AND session = ?", (attendance_date, session)) as cursor:
                total = (await cursor.fetchone())[0]

            streak = await calculate_streak(user.id)
            try:
                await query.edit_message_reply_markup(reply_markup=build_attendance_keyboard(attendance_date, session, total))
            except Exception:
                pass

            streak_text = f" 🔥 Streak: {streak} days!" if streak > 1 else ""
            await query.answer(f"✅ Session logged, {name}!{streak_text} (Total today: {total})", show_alert=False)
        except aiosqlite.IntegrityError:
            await query.answer("⚠️ You have already checked in for today's practice!", show_alert=True)
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
# REUSABLE SCHEDULED REMINDERS
# =========================================================

async def scheduled_community_prompt(context: ContextTypes.DEFAULT_TYPE):
    prompt_title, greeting = context.job.data
    today = get_current_date()

    msg = (
        f"🧘 *{esc(prompt_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date: `{esc(today)}`\n"
        f"⏱ Duration: `20 Minutes`\n\n"
        f"{esc(greeting)}\n"
        f"Sit down for your 20\\-minute stillness practice now, or check in below:"
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_attendance_keyboard(today, "daily", 0),
    )


async def scheduled_unmarked_catchup(context: ContextTypes.DEFAULT_TYPE):
    alert_title, custom_note = context.job.data
    today = get_current_date()
    month = today[:7]

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT DISTINCT user_id, name 
            FROM attendance 
            WHERE attendance_date LIKE ? 
              AND user_id NOT IN (
                  SELECT user_id FROM attendance WHERE attendance_date = ?
              )
        """, (f"{month}%", today)) as cursor:
            unmarked_members = await cursor.fetchall()

    if not unmarked_members:
        return

    member_lines = "\n".join([f"• {esc(name)}" for _, name in unmarked_members])

    msg = (
        f"🔔 *{esc(alert_title.upper())}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"These practitioners haven't checked in for today's sit yet:\n\n"
        f"{member_lines}\n\n"
        f"{esc(custom_note)}\n"
        f"Tap below to start your 20\\-min timer or log check\\-in:"
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
    logger.info(f"Health check web server running on port {port}")
    return runner


async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return

    await setup_database()
    web_runner = await start_web_server()

    app = Application.builder().token(TOKEN).build()

    # Scheduled Jobs
    prompts = [
        (time(5, 0, tzinfo=TIMEZONE), ("Morning Dawn Meditation", "Good morning! Welcome the dawn with peaceful awareness.")),
        (time(8, 30, tzinfo=TIMEZONE), ("Mid-Morning Practice Call", "Ground your thoughts and center your presence before the workday accelerates.")),
        (time(13, 30, tzinfo=TIMEZONE), ("Midday Stillness Pause", "Step back for 20 minutes of calm breath to reset your mental clarity.")),
        (time(18, 0, tzinfo=TIMEZONE), ("Evening Sunset Meditation", "Unwind from daily activity and transition into restful evening stillness.")),
    ]
    for schedule_time, data in prompts:
        app.job_queue.run_daily(scheduled_community_prompt, schedule_time, data=data)

    catchups = [
        (time(19, 30, tzinfo=TIMEZONE), ("Evening Practice Check", "A gentle reminder to find 20 minutes of peace this evening.")),
        (time(21, 30, tzinfo=TIMEZONE), ("Night Attendance Reminder", "Wind down your day with mindful meditation before sleep.")),
        (time(23, 0, tzinfo=TIMEZONE), ("Final Daily Call (11:00 PM)", "Protect your daily practice streak before midnight!")),
    ]
    for schedule_time, data in catchups:
        app.job_queue.run_daily(scheduled_unmarked_catchup, schedule_time, data=data)

    # Command & Message Handlers
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
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Bot started successfully.")
            while True:
                await asyncio.sleep(3600)
    finally:
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
