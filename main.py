import os
import json
import sqlite3
import logging
import threading

from flask import Flask, send_from_directory
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# SOZLAMALAR
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = os.getenv("DB_PATH", "goldrex.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)

# Admin e'lon yuborish rejimi
ADMIN_BROADCAST_WAITING = set()

GAMES = {
    "chess": "♟ Shaxmat",
    "checkers": "⚫ Shashka",
    "connect4": "🔴 4 ta tosh",
    "snake": "🐍 Snake Lite",
}


# =========================
# DATABASE
# =========================

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS game_settings (
                game_id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1
            )
        """)

        for game_id in GAMES:
            con.execute(
                "INSERT OR IGNORE INTO game_settings (game_id, enabled) VALUES (?, 1)",
                (game_id,)
            )


def save_user(user):
    if not user:
        return

    with db_connect() as con:
        con.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or ""
        ))


def get_user_count():
    with db_connect() as con:
        row = con.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0


def get_user_ids():
    with db_connect() as con:
        rows = con.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]


def game_is_enabled(game_id):
    with db_connect() as con:
        row = con.execute(
            "SELECT enabled FROM game_settings WHERE game_id=?",
            (game_id,)
        ).fetchone()

    return bool(row[0]) if row else True


def toggle_game(game_id):
    current = game_is_enabled(game_id)
    new_value = 0 if current else 1

    with db_connect() as con:
        con.execute(
            "UPDATE game_settings SET enabled=? WHERE game_id=?",
            (new_value, game_id)
        )

    return bool(new_value)


# =========================
# FLASK WEB SERVER
# =========================

@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/health")
def health():
    return {"status": "GOLDREX is running"}


# =========================
# TELEGRAM START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    keyboard = []

    if WEBAPP_URL:
        keyboard.append([
            InlineKeyboardButton(
                "🎮 GOLDREX O'YINLAR",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "📋 O'yinlar ro'yxati",
            callback_data="show_games"
        )
    ])

    text = (
        f"👑 GOLDREX ga xush kelibsiz, {user.first_name}!\n\n"
        "🎮 Onlayn o'yinlar platformasi\n"
        "♟ Shaxmat\n"
        "⚫ Shashka\n"
        "🔴 4 ta tosh\n"
        "🐍 Snake Lite\n\n"
        "O'yinlarni boshlash uchun tugmani bosing."
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# ADMIN PANEL
# =========================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 Foydalanuvchilar",
                callback_data="admin_users"
            )
        ],
        [
            InlineKeyboardButton(
                "🎮 O'yinlarni boshqarish",
                callback_data="admin_games"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 E'lon yuborish",
                callback_data="admin_broadcast"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Yangilash",
                callback_data="admin_home"
            )
        ],
    ])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Siz admin emassiz.")
        return

    await update.message.reply_text(
        "👑 GOLDREX ADMIN PANEL\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )


async def show_admin_home(query):
    await query.edit_message_text(
        "👑 GOLDREX ADMIN PANEL\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )


async def show_admin_games(query):
    rows = []

    for game_id, game_name in GAMES.items():
        enabled = game_is_enabled(game_id)
        status = "🟢 Yoqilgan" if enabled else "🔴 O'chirilgan"
        button_text = f"{game_name} — {status}"

        rows.append([
            InlineKeyboardButton(
                button_text,
                callback_data=f"toggle_{game_id}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Admin panel",
            callback_data="admin_home"
        )
    ])

    await query.edit_message_text(
        "🎮 O'YINLARNI BOSHQARISH\n\n"
        "O'yinni yoqish yoki o'chirish uchun tugmasini bosing.",
        reply_markup=InlineKeyboardMarkup(rows)
    )


# =========================
# CALLBACK BUTTONS
# =========================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    # Admin callbacklarini tekshirish
    if data.startswith("admin_") or data.startswith("toggle_"):
        if user.id != ADMIN_ID:
            await query.answer("Admin uchun!", show_alert=True)
            return

    if data == "admin_home":
        await show_admin_home(query)

    elif data == "admin_users":
        count = get_user_count()

        await query.edit_message_text(
            f"👥 FOYDALANUVCHILAR\n\n"
            f"Jami bot foydalanuvchilari: {count}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Admin panel",
                    callback_data="admin_home"
                )
            ]])
        )

    elif data == "admin_games":
        await show_admin_games(query)

    elif data.startswith("toggle_"):
        game_id = data.replace("toggle_", "", 1)

        if game_id in GAMES:
            new_state = toggle_game(game_id)
            status = "yoqildi" if new_state else "o'chirildi"

            await query.answer(
                f"{GAMES[game_id]} {status}",
                show_alert=True
            )

        await show_admin_games(query)

    elif data == "admin_broadcast":
        ADMIN_BROADCAST_WAITING.add(user.id)

        await query.edit_message_text(
            "📢 E'lon yuborish\n\n"
            "Endi barcha foydalanuvchilarga yuboriladigan "
            "matnni yozing.\n\n"
            "Bekor qilish uchun /cancel yuboring."
        )

    elif data == "show_games":
        lines = []

        for game_id, game_name in GAMES.items():
            status = "🟢" if game_is_enabled(game_id) else "🔴"
            lines.append(f"{status} {game_name}")

        await query.edit_message_text(
            "🎮 O'YINLAR\n\n" + "\n".join(lines)
        )


# =========================
# ADMIN BROADCAST
# =========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ADMIN_BROADCAST_WAITING.discard(user_id)

    await update.message.reply_text("Bekor qilindi.")


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        return

    if user.id not in ADMIN_BROADCAST_WAITING:
        return

    message_text = update.message.text

    if not message_text:
        await update.message.reply_text("Faqat matn yuboring.")
        return

    ADMIN_BROADCAST_WAITING.discard(user.id)

    user_ids = get_user_ids()
    sent = 0
    failed = 0

    status_message = await update.message.reply_text(
        f"📢 E'lon yuborilmoqda...\n"
        f"Foydalanuvchilar: {len(user_ids)}"
    )

    for user_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 GOLDREX xabari\n\n{message_text}"
            )
            sent += 1
        except Exception:
            failed += 1

    await status_message.edit_text(
        "✅ E'lon yuborish tugadi.\n\n"
        f"Yuborildi: {sent}\n"
        f"Yuborilmadi: {failed}"
    )


# =========================
# BOT START
# =========================

def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable sozlanmagan"
        )

    if ADMIN_ID == 0:
        logging.warning("ADMIN_ID sozlanmagan!")

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.run_polling(drop_pending_updates=True)


# =========================
# ISHGA TUSHIRISH
# =========================

if __name__ == "__main__":
    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=PORT,
            use_reloader=False
        ),
        daemon=True
    ).start()

    run_bot()
