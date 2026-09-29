import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

(
    REGISTER_NAME,
    SELECT_TIMEZONE,
    SITE_NAME,
    WORKERS,
    COMPLETED_WORK,
    PHOTOS,
    ISSUES,
    TOMORROW_PLAN,
) = range(8)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID")
GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else None

DB_FILE = os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."),
    "attendance.db"
)

MANAGER_TIMEZONE = ZoneInfo("America/New_York")

START_BUTTON = "Start Daily Update"
CHANGE_TIMEZONE_BUTTON = "Change Time Zone"

START_CALLBACK = "start_daily_update"
STATS_CALLBACK = "show_worker_stats"
PHOTOS_DONE_CALLBACK = "photos_done"
WORKER_CALLBACK_PREFIX = "worker:"
WORKERS_DONE_CALLBACK = "workers_done"
TIMEZONE_CALLBACK_PREFIX = "timezone:"

SUPPORTED_TIMEZONES = {
    "Eastern Time": "America/New_York",
    "Central Time": "America/Chicago",
    "Mountain Time": "America/Denver",
    "Pacific Time": "America/Los_Angeles",
}

EMPLOYEES = [
    name.strip()
    for name in os.getenv(
        "EMPLOYEES",
        "Employee One,Employee Two,Employee Three"
    ).split(",")
    if name.strip()
]

CREW_LEAD_KEYBOARD = ReplyKeyboardMarkup(
    [
        [START_BUTTON],
        [CHANGE_TIMEZONE_BUTTON],
    ],
    resize_keyboard=True
)

REMINDER_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                text="Start Daily Update",
                callback_data=START_CALLBACK
            )
        ]
    ]
)

MANAGER_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                text="Stats",
                callback_data=STATS_CALLBACK
            )
        ]
    ]
)

PHOTOS_DONE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                text="Done uploading photos",
                callback_data=PHOTOS_DONE_CALLBACK
            )
        ]
    ]
)

TIMEZONE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                text=timezone_label,
                callback_data=TIMEZONE_CALLBACK_PREFIX + timezone_name
            )
        ]
        for timezone_label, timezone_name in SUPPORTED_TIMEZONES.items()
    ]
)


def build_workers_keyboard(
    selected_worker_indexes: set[int]
) -> InlineKeyboardMarkup:
    keyboard = []

    for index, employee_name in enumerate(EMPLOYEES):
        marker = "✅" if index in selected_worker_indexes else "⬜"

        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {employee_name}",
                    callback_data=f"{WORKER_CALLBACK_PREFIX}{index}"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                text="Done",
                callback_data=WORKERS_DONE_CALLBACK
            )
        ]
    )

    return InlineKeyboardMarkup(keyboard)


EMPTY_WORKER_VALUES = {
    "none",
    "no workers",
    "no worker",
    "n/a",
    "na",
    "нет",
    "никого",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

def initialize_database() -> None:
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_key TEXT PRIMARY KEY,
                worker_name TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                work_date TEXT NOT NULL,
                worker_key TEXT NOT NULL,
                PRIMARY KEY (work_date, worker_key),
                FOREIGN KEY (worker_key) REFERENCES workers(worker_key)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS crew_leads (
                telegram_user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                crew_lead_name TEXT NOT NULL,
                timezone TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_reports (
                report_date TEXT NOT NULL,
                crew_lead_user_id INTEGER NOT NULL,
                site_name TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                PRIMARY KEY (report_date, crew_lead_user_id)
            )
            """
        )

        connection.commit()


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


def normalize_name(name: str) -> str:
    return clean_name(name).casefold()


def parse_workers(workers_text: str) -> list[str]:
    workers = []

    for item in re.split(r"[,;\n]+", workers_text):
        worker_name = clean_name(item)

        if not worker_name:
            continue

        if normalize_name(worker_name) in EMPTY_WORKER_VALUES:
            continue

        workers.append(worker_name)

    return workers


def save_crew_lead(
    telegram_user_id: int,
    chat_id: int,
    crew_lead_name: str,
    timezone_name: str
) -> None:
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO crew_leads (
                telegram_user_id,
                chat_id,
                crew_lead_name,
                timezone
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                telegram_user_id,
                chat_id,
                clean_name(crew_lead_name),
                timezone_name,
            )
        )

        connection.commit()


def get_crew_lead(
    telegram_user_id: int
) -> tuple[int, int, str, str] | None:
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.execute(
            """
            SELECT
                telegram_user_id,
                chat_id,
                crew_lead_name,
                timezone
            FROM crew_leads
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,)
        )

        return cursor.fetchone()


def update_crew_lead_timezone(
    telegram_user_id: int,
    timezone_name: str
) -> None:
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            """
            UPDATE crew_leads
            SET timezone = ?
            WHERE telegram_user_id = ?
            """,
            (timezone_name, telegram_user_id)
        )

        connection.commit()


def get_crew_leads_by_timezone(
    timezone_name: str
) -> list[tuple[int, int, str, str]]:
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.execute(
            """
            SELECT
                telegram_user_id,
                chat_id,
                crew_lead_name,
                timezone
            FROM crew_leads
            WHERE timezone = ?
            ORDER BY crew_lead_name COLLATE NOCASE
            """,
            (timezone_name,)
        )

        return cursor.fetchall()


def save_daily_report(
    report_date: date,
    crew_lead_user_id: int,
    site_name: str,
    submitted_at: datetime
) -> None:
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO daily_reports (
                report_date,
                crew_lead_user_id,
                site_name,
                submitted_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                report_date.isoformat(),
                crew_lead_user_id,
                site_name,
                submitted_at.isoformat(),
            )
        )

        connection.commit()


def has_daily_report(
    report_date: date,
    crew_lead_user_id: int
) -> bool:
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.execute(
            """
            SELECT 1
            FROM daily_reports
            WHERE report_date = ?
              AND crew_lead_user_id = ?
            LIMIT 1
            """,
            (
                report_date.isoformat(),
                crew_lead_user_id,
            )
        )

        return cursor.fetchone() is not None


def save_attendance(
    work_date: date,
    crew_lead: str,
    workers_text: str
) -> None:
    all_workers = [crew_lead] + parse_workers(workers_text)
    unique_workers = {}

    for worker_name in all_workers:
        display_name = clean_name(worker_name)
        worker_key = normalize_name(display_name)

        if worker_key:
            unique_workers.setdefault(worker_key, display_name)

    with sqlite3.connect(DB_FILE) as connection:
        for worker_key, worker_name in unique_workers.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO workers (
                    worker_key,
                    worker_name
                )
                VALUES (?, ?)
                """,
                (worker_key, worker_name)
            )

            connection.execute(
                """
                INSERT OR IGNORE INTO attendance (
                    work_date,
                    worker_key
                )
                VALUES (?, ?)
                """,
                (work_date.isoformat(), worker_key)
            )

        connection.commit()


def get_worker_statistics(
    start_date: date,
    end_date: date
) -> list[tuple[str, int]]:
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.execute(
            """
            SELECT
                workers.worker_name,
                COUNT(*) AS days_worked
            FROM attendance
            JOIN workers
                ON attendance.worker_key = workers.worker_key
            WHERE attendance.work_date BETWEEN ? AND ?
            GROUP BY
                workers.worker_key,
                workers.worker_name
            ORDER BY
                days_worked DESC,
                workers.worker_name COLLATE NOCASE
            """,
            (
                start_date.isoformat(),
                end_date.isoformat(),
            )
        )

        return cursor.fetchall()


def build_statistics_message(
    start_date: date,
    end_date: date,
    title: str = "WORKER STATISTICS"
) -> str:
    statistics = get_worker_statistics(
        start_date=start_date,
        end_date=end_date
    )

    lines = [
        title,
        f"{start_date.strftime('%m/%d/%Y')} - "
        f"{end_date.strftime('%m/%d/%Y')}",
        ""
    ]

    if not statistics:
        lines.append("No worker data for this period.")
        return "\n".join(lines)

    for worker_name, days_worked in statistics:
        word = "day" if days_worked == 1 else "days"
        lines.append(
            f"{worker_name} - {days_worked} {word}"
        )

    return "\n".join(lines)


def get_closed_period(
    report_date: date
) -> tuple[date, date]:
    if report_date.day == 1:
        previous_month_last_day = (
            report_date - timedelta(days=1)
        )

        start_date = previous_month_last_day.replace(day=16)
        end_date = previous_month_last_day

        return start_date, end_date

    if report_date.day == 16:
        start_date = report_date.replace(day=1)
        end_date = report_date.replace(day=15)

        return start_date, end_date

    raise ValueError(
        "Scheduled statistics can only run "
        "on the 1st or 16th day."
    )


def get_current_open_period(
    today: date
) -> tuple[date, date]:
    if today.day <= 15:
        return today.replace(day=1), today

    return today.replace(day=16), today


async def begin_daily_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await message.reply_text(
            "Please open a private chat with the bot "
            "to submit a Daily Update."
        )

        return ConversationHandler.END

    crew_lead = get_crew_lead(user.id)

    if crew_lead is None:
        context.user_data.clear()

        await message.reply_text(
            "First-time setup.\n\n"
            "Enter your crew lead name.",
            reply_markup=ReplyKeyboardRemove()
        )

        return REGISTER_NAME

    _, _, crew_lead_name, timezone_name = crew_lead

    context.user_data.clear()
    context.user_data["crew_lead_user_id"] = user.id
    context.user_data["crew_lead_name"] = crew_lead_name
    context.user_data["crew_timezone"] = timezone_name

    await message.reply_text(
        "Daily Update started.\n\n"
        "What is the site name?",
        reply_markup=ReplyKeyboardRemove()
    )

    return SITE_NAME


async def start_from_inline_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    return await begin_daily_update(
        update=update,
        context=context
    )


async def start_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message

    if update.effective_chat.type != "private":
        await message.reply_text(
            "Please open a private chat with the bot "
            "to register."
        )

        return ConversationHandler.END

    context.user_data.clear()

    await message.reply_text(
        "Enter your crew lead name.",
        reply_markup=ReplyKeyboardRemove()
    )

    return REGISTER_NAME


async def get_registration_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    crew_lead_name = clean_name(update.message.text)

    if not crew_lead_name:
        await update.message.reply_text(
            "Please enter your crew lead name."
        )

        return REGISTER_NAME

    context.user_data["pending_crew_lead_name"] = (
        crew_lead_name
    )

    await update.message.reply_text(
        "Select your current time zone:",
        reply_markup=TIMEZONE_KEYBOARD
    )

    return SELECT_TIMEZONE


async def change_timezone_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    user = update.effective_user

    if update.effective_chat.type != "private":
        await message.reply_text(
            "Please open a private chat with the bot "
            "to change your time zone."
        )

        return ConversationHandler.END

    crew_lead = get_crew_lead(user.id)

    if crew_lead is None:
        await message.reply_text(
            "You are not registered yet.\n"
            "Press Start Daily Update to register.",
            reply_markup=CREW_LEAD_KEYBOARD
        )

        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["timezone_update_only"] = True

    await message.reply_text(
        "Select your current time zone:",
        reply_markup=TIMEZONE_KEYBOARD
    )

    return SELECT_TIMEZONE


async def timezone_selected(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    await query.answer()

    timezone_name = query.data.removeprefix(
        TIMEZONE_CALLBACK_PREFIX
    )

    if timezone_name not in SUPPORTED_TIMEZONES.values():
        await query.message.reply_text(
            "Invalid time zone. Please try again."
        )

        return SELECT_TIMEZONE

    pending_name = context.user_data.get(
        "pending_crew_lead_name"
    )

    if pending_name:
        save_crew_lead(
            telegram_user_id=user.id,
            chat_id=chat.id,
            crew_lead_name=pending_name,
            timezone_name=timezone_name
        )

        context.user_data.clear()

        await query.message.reply_text(
            "Registration completed.\n\n"
            "Your reminder time zone has been saved.\n"
            "Press Start Daily Update when you are ready.",
            reply_markup=CREW_LEAD_KEYBOARD
        )

        return ConversationHandler.END

    update_crew_lead_timezone(
        telegram_user_id=user.id,
        timezone_name=timezone_name
    )

    context.user_data.clear()

    await query.message.reply_text(
        "Your time zone has been updated.",
        reply_markup=CREW_LEAD_KEYBOARD
    )

    return ConversationHandler.END


async def get_site_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["site_name"] = update.message.text
    context.user_data["selected_worker_indexes"] = set()

    await update.message.reply_text(
        "Who worked on the site today?\n"
        "Select all employees and press Done.",
        reply_markup=build_workers_keyboard(set())
    )

    return WORKERS


async def toggle_worker_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    worker_index_text = query.data.removeprefix(
        WORKER_CALLBACK_PREFIX
    )

    try:
        worker_index = int(worker_index_text)
    except ValueError:
        await query.answer(
            "Invalid employee selection.",
            show_alert=True
        )

        return WORKERS

    if worker_index < 0 or worker_index >= len(EMPLOYEES):
        await query.answer(
            "Invalid employee selection.",
            show_alert=True
        )

        return WORKERS

    selected_worker_indexes = context.user_data.setdefault(
        "selected_worker_indexes",
        set()
    )

    if worker_index in selected_worker_indexes:
        selected_worker_indexes.remove(worker_index)
    else:
        selected_worker_indexes.add(worker_index)

    await query.edit_message_reply_markup(
        reply_markup=build_workers_keyboard(
            selected_worker_indexes
        )
    )

    return WORKERS


async def workers_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    selected_worker_indexes = context.user_data.get(
        "selected_worker_indexes",
        set()
    )

    if not selected_worker_indexes:
        await query.message.reply_text(
            "Please select at least one employee "
            "before pressing Done."
        )

        return WORKERS

    selected_workers = [
        EMPLOYEES[index]
        for index in sorted(selected_worker_indexes)
    ]

    context.user_data["workers"] = "\n".join(
        selected_workers
    )

    await query.message.reply_text(
        "Employees selected:\n"
        + "\n".join(
            f"- {worker_name}"
            for worker_name in selected_workers
        )
        + "\n\nWhat work was completed today?"
    )

    return COMPLETED_WORK


async def workers_selection_required(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Please select employees using the buttons "
        "and then press Done."
    )

    return WORKERS


async def get_completed_work(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["completed_work"] = (
        update.message.text
    )

    context.user_data["photos"] = []

    await update.message.reply_text(
        "Send 2-3 clear photos of the work "
        "completed today.\n\n"
        "Send the photos one by one."
    )

    return PHOTOS


async def get_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    photos = context.user_data.setdefault(
        "photos",
        []
    )

    if len(photos) >= 3:
        return PHOTOS

    photo_file_id = update.message.photo[-1].file_id
    photos.append(photo_file_id)

    photo_count = len(photos)

    if photo_count == 1:
        await update.message.reply_text(
            "Photo 1 received.\n"
            "Send at least one more photo."
        )

        return PHOTOS

    if photo_count == 2:
        await update.message.reply_text(
            "Photo 2 received.\n\n"
            "You can send one more photo "
            "or press the button below.",
            reply_markup=PHOTOS_DONE_KEYBOARD
        )

        return PHOTOS

    await update.message.reply_text(
        "Photo 3 received.\n\n"
        "Were there any issues or delays?\n"
        "If there were none, enter: No issues"
    )

    return ISSUES


async def photos_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    photos = context.user_data.get("photos", [])

    if len(photos) < 2:
        await query.message.reply_text(
            "Please send at least 2 photos."
        )

        return PHOTOS

    await query.message.reply_text(
        "Photos received.\n\n"
        "Were there any issues or delays?\n"
        "If there were none, enter: No issues"
    )

    return ISSUES


async def photo_required(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Please send a photo.\n"
        "You need to upload 2-3 photos "
        "of the completed work."
    )

    return PHOTOS


async def get_issues(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["issues"] = update.message.text

    await update.message.reply_text(
        "What is the plan for tomorrow?"
    )

    return TOMORROW_PLAN


async def get_tomorrow_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["tomorrow_plan"] = (
        update.message.text
    )

    crew_lead_user_id = context.user_data[
        "crew_lead_user_id"
    ]

    crew_lead_name = context.user_data[
        "crew_lead_name"
    ]

    crew_timezone = context.user_data[
        "crew_timezone"
    ]

    local_now = datetime.now(
        ZoneInfo(crew_timezone)
    )

    report_date = local_now.date()
    photos = context.user_data.get("photos", [])

    save_attendance(
        work_date=report_date,
        crew_lead=crew_lead_name,
        workers_text=context.user_data["workers"]
    )

    save_daily_report(
        report_date=report_date,
        crew_lead_user_id=crew_lead_user_id,
        site_name=context.user_data["site_name"],
        submitted_at=local_now
    )

    daily_update = (
        f"DAILY UPDATE - "
        f"{report_date.strftime('%m/%d/%Y')}\n\n"
        f"Crew lead:\n"
        f"{crew_lead_name}\n\n"
        f"Site:\n"
        f"{context.user_data['site_name']}\n\n"
        f"Workers on site:\n"
        f"{context.user_data['workers']}\n\n"
        f"Completed today:\n"
        f"{context.user_data['completed_work']}\n\n"
        f"Photos attached:\n"
        f"{len(photos)}\n\n"
        f"Issues or delays:\n"
        f"{context.user_data['issues']}\n\n"
        f"Plan for tomorrow:\n"
        f"{context.user_data['tomorrow_plan']}"
    )

    await update.message.reply_text(
        "Daily Update completed and sent "
        "to the manager group:\n\n"
        + daily_update
    )

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=daily_update,
        reply_markup=MANAGER_KEYBOARD
    )

    media_group = []

    for index, photo_file_id in enumerate(photos):
        caption = None

        if index == 0:
            caption = (
                f"Work photos\n"
                f"Site: "
                f"{context.user_data['site_name']}\n"
                f"Crew lead: {crew_lead_name}"
            )

        media_group.append(
            InputMediaPhoto(
                media=photo_file_id,
                caption=caption
            )
        )

    await context.bot.send_media_group(
        chat_id=GROUP_CHAT_ID,
        media=media_group
    )

    context.user_data.clear()

    await update.message.reply_text(
        "Press the button below to start "
        "a new Daily Update.",
        reply_markup=CREW_LEAD_KEYBOARD
    )

    return ConversationHandler.END


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()

    await update.effective_message.reply_text(
        "Daily Update cancelled.",
        reply_markup=CREW_LEAD_KEYBOARD
    )

    return ConversationHandler.END


async def chat_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}"
    )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text(
            "This command is available only "
            "in the Daily Updates group."
        )

        return

    today = datetime.now(MANAGER_TIMEZONE).date()

    start_date, end_date = get_current_open_period(
        today
    )

    message = build_statistics_message(
        start_date=start_date,
        end_date=end_date,
        title="CURRENT WORKER STATISTICS"
    )

    await update.message.reply_text(
        message,
        reply_markup=MANAGER_KEYBOARD
    )


async def stats_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    if query.message.chat.id != GROUP_CHAT_ID:
        await query.message.reply_text(
            "This button is available only "
            "in the Daily Updates group."
        )

        return

    today = datetime.now(MANAGER_TIMEZONE).date()

    start_date, end_date = get_current_open_period(
        today
    )

    message = build_statistics_message(
        start_date=start_date,
        end_date=end_date,
        title="CURRENT WORKER STATISTICS"
    )

    await query.message.reply_text(
        message,
        reply_markup=MANAGER_KEYBOARD
    )


async def manager_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text(
            "This command is available only "
            "in the Daily Updates group."
        )

        return

    await update.message.reply_text(
        "Manager menu:",
        reply_markup=MANAGER_KEYBOARD
    )


async def send_scheduled_statistics(
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    today = datetime.now(MANAGER_TIMEZONE).date()

    start_date, end_date = get_closed_period(
        today
    )

    message = build_statistics_message(
        start_date=start_date,
        end_date=end_date
    )

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=message,
        reply_markup=MANAGER_KEYBOARD
    )


async def send_local_reminders(
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    job_data = context.job.data

    timezone_name = job_data["timezone"]
    reminder_hour = job_data["hour"]

    local_today = datetime.now(
        ZoneInfo(timezone_name)
    ).date()

    crew_leads = get_crew_leads_by_timezone(
        timezone_name=timezone_name
    )

    for (
        telegram_user_id,
        chat_id,
        crew_lead_name,
        _,
    ) in crew_leads:
        if has_daily_report(
            report_date=local_today,
            crew_lead_user_id=telegram_user_id
        ):
            continue

        if reminder_hour == 18:
            reminder_text = (
                "Daily Update reminder\n\n"
                "Please submit your Daily Update "
                "for today."
            )
        else:
            reminder_text = (
                "Second reminder\n\n"
                "Your Daily Update has not been "
                "submitted yet.\n"
                "Please complete it as soon "
                "as possible."
            )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=reminder_text,
                reply_markup=REMINDER_KEYBOARD
            )

            logger.info(
                "Reminder sent to %s at %s:00",
                crew_lead_name,
                reminder_hour
            )

        except Exception:
            logger.exception(
                "Could not send reminder to %s",
                crew_lead_name
            )


def schedule_reminders(
    app: Application
) -> None:
    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. "
            "Install python-telegram-bot[job-queue]."
        )

    for timezone_name in SUPPORTED_TIMEZONES.values():
        timezone = ZoneInfo(timezone_name)

        app.job_queue.run_daily(
            send_local_reminders,
            time=time(
                hour=18,
                minute=0,
                tzinfo=timezone
            ),
            data={
                "timezone": timezone_name,
                "hour": 18,
            },
            name=f"reminder_18_{timezone_name}"
        )

        app.job_queue.run_daily(
            send_local_reminders,
            time=time(
                hour=20,
                minute=0,
                tzinfo=timezone
            ),
            data={
                "timezone": timezone_name,
                "hour": 20,
            },
            name=f"reminder_20_{timezone_name}"
        )


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN was not found. "
            "Check the .env file."
        )

    if not GROUP_CHAT_ID:
        raise ValueError(
            "GROUP_CHAT_ID was not found. "
            "Check the .env file."
        )

    initialize_database()

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    conversation_handler = ConversationHandler(
        entry_points=[
            CommandHandler(
                "start",
                begin_daily_update
            ),
            CommandHandler(
                "register",
                start_registration
            ),
            CommandHandler(
                "timezone",
                change_timezone_start
            ),
            MessageHandler(
                filters.Regex(
                    f"^{re.escape(START_BUTTON)}$"
                ),
                begin_daily_update
            ),
            MessageHandler(
                filters.Regex(
                    f"^{re.escape(CHANGE_TIMEZONE_BUTTON)}$"
                ),
                change_timezone_start
            ),
            CallbackQueryHandler(
                start_from_inline_button,
                pattern=f"^{START_CALLBACK}$"
            ),
        ],
        states={
            REGISTER_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_registration_name
                )
            ],
            SELECT_TIMEZONE: [
                CallbackQueryHandler(
                    timezone_selected,
                    pattern=f"^{TIMEZONE_CALLBACK_PREFIX}"
                )
            ],
            SITE_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_site_name
                )
            ],
            WORKERS: [
                CallbackQueryHandler(
                    toggle_worker_selection,
                    pattern=f"^{WORKER_CALLBACK_PREFIX}\\d+$"
                ),
                CallbackQueryHandler(
                    workers_done,
                    pattern=f"^{WORKERS_DONE_CALLBACK}$"
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    workers_selection_required
                ),
            ],
            COMPLETED_WORK: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_completed_work
                )
            ],
            PHOTOS: [
                MessageHandler(
                    filters.PHOTO,
                    get_photo
                ),
                CallbackQueryHandler(
                    photos_done,
                    pattern=f"^{PHOTOS_DONE_CALLBACK}$"
                ),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    photo_required
                ),
            ],
            ISSUES: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_issues
                )
            ],
            TOMORROW_PLAN: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_tomorrow_plan
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel
            )
        ],
        allow_reentry=True
    )

    app.add_handler(
        CommandHandler(
            "chatid",
            chat_id
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    app.add_handler(
        CommandHandler(
            "menu",
            manager_menu
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            stats_button,
            pattern=f"^{STATS_CALLBACK}$"
        )
    )

    app.add_handler(
        conversation_handler
    )

    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. "
            "Install python-telegram-bot[job-queue]."
        )

    report_time = time(
        hour=10,
        minute=0,
        tzinfo=MANAGER_TIMEZONE
    )

    app.job_queue.run_monthly(
        send_scheduled_statistics,
        when=report_time,
        day=1,
        name="worker_statistics_day_1"
    )

    app.job_queue.run_monthly(
        send_scheduled_statistics,
        when=report_time,
        day=16,
        name="worker_statistics_day_16"
    )

    schedule_reminders(
        app=app
    )

    print(
        "Bot is running. Press Ctrl + C to stop it."
    )

    print(
        "Worker statistics: "
        "1st and 16th day at 10:00 AM."
    )

    print(
        "Crew lead reminders: "
        "6:00 PM and 8:00 PM local time."
    )

    app.run_polling()


if __name__ == "__main__":
    main()