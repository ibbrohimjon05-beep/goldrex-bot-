
import os
import logging
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
    ContextTypes,
)

# =========================
# SOZLAMALAR
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)

# =========================
# WEB SERVER
# =========================

@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/health")
def health():
    return {"status": "GOLDREX is running"}


# =========================
# TELEGRAM BOT
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

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
            "📢 Kanal",
            url="https://t.me/"
        )
    ])

    text = (
        f"👑 GOLDREX ga xush kelibsiz, "
        f"{user.first_name}!\n\n"
        "🎮 Onlayn o'yinlar platformasi\n"
        "♟ Shahmat\n"
        "⚫ Shashka\n"
        "🔴 4 ta shar\n"
        "🐍 Snake Lite\n\n"
        "O'yinlarni boshlash uchun tugmani bosing."
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "⛔ Siz admin emassiz."
        )
        return

    await update.message.reply_text(
        "👑 GOLDREX Admin Panel\n\n"
        "Keyingi bosqichda bu yerga:\n"
        "• Foydalanuvchilar\n"
        "• O'yin sozlamalari\n"
        "• Majburiy obuna\n"
        "• Vazifalar\n"
        "• Balans boshqaruvi\n"
        "• Reyting\n"
        "• Xabar yuborish\n"
        "qo'shiladi."
    )


def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable sozlanmagan"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin)
    )

    application.run_polling(
        drop_pending_updates=True
    )


# =========================
# ISHGA TUSHIRISH
# =========================

if __name__ == "__main__":
    import threading

    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=PORT,
            use_reloader=False
        ),
        daemon=True
    ).start()

    run_bot()
