"""
unrlly Studio Bot — внутренний инструмент команды
/brief  — сбор брифа (доступен всем авторизованным)
/kp     — генерация КП (структура видна всем, цена летит только владельцу в личку)
/reset  — сбросить сессию
/status — что уже собрано
"""

import os
import logging
from datetime import datetime
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Все кто может пользоваться ботом
_ids = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x.strip()) for x in _ids.split(",") if x.strip()}

# Только этот ID получает КП с ценой в личку (обычно Макс)
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Промпты ───────────────────────────────────────────────────────────────────

BRIEF_SYSTEM = """Ты — ассистент продуктовой студии unrlly (сайты, UI/UX, motion, webapp).
Работаешь с командой студии.

Твоя задача: команда вставляет сообщения от клиента, ты:
1. Анализируешь что уже известно
2. Задаёшь ОДИН уточняющий вопрос — самый важный из недостающих

Нужно собрать:
- Что делаем (тип: сайт / motion / UI / webapp)
- Для кого (сфера, продукт)
- Цель результата
- Объём (экраны / секунды / функционал)
- Дедлайн
- Бюджетный диапазон (до 100к / 100–200к / 200к+)
- Референсы
- ЛПР (кто принимает решение)

Когда собрано достаточно — напиши [BRIEF_READY] и структурируй бриф.

Формат брифа:
## 📋 Бриф — [название]
**Тип:** [Site Basic / Site Plus / Motion / UI/UX / Webapp]
**Задача:** [1–2 предложения]
**Для кого:** [сфера]
**Цель:** [что должно произойти]
**Объём:** [детали]
**Дедлайн:** [дата или «не указан»]
**Бюджет:** [диапазон]
**Референсы:** [или «нет»]
**ЛПР:** [имя/контакт]
**⚠️ Риски:** [что неясно]

Язык: русский. Один вопрос за раз. Кратко."""

KP_FULL_SYSTEM = """Генерируй полное КП для студии unrlly.
Включай все разделы: обзор, объём работ, таймлайн, стоимость, риски, доп.услуги.
В разделе стоимость пиши реальные цифры — [СУММА] как плейсхолдер.
Язык: русский, профессиональный тон."""

KP_PUBLIC_SYSTEM = """Генерируй КП для студии unrlly БЕЗ стоимости.
Включай: обзор проекта, объём работ, таймлайн, риски.
Раздел стоимости НЕ включай вообще — только в конце напиши:
«💰 Стоимость: согласовывается отдельно»
Язык: русский, профессиональный тон."""

# ── Сессии ────────────────────────────────────────────────────────────────────

sessions: dict[int, dict] = {}

def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {"mode": None, "history": [], "brief_data": None}
    return sessions[user_id]

def reset_session(user_id: int):
    sessions[user_id] = {"mode": None, "history": [], "brief_data": None}

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
    return user_id in ALLOWED_IDS

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

# ── Команды ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return

    owner_note = "\n👑 У тебя доступ к полному КП с ценой." if is_owner(update.effective_user.id) else ""

    await update.message.reply_text(
        f"👋 *unrlly Studio Bot*\n\n"
        f"Вставляй сообщения от клиента, я помогу разобраться.\n\n"
        f"Команды:\n"
        f"• /brief — начать сбор брифа\n"
        f"• /kp — сгенерировать КП\n"
        f"• /status — что собрано\n"
        f"• /reset — начать заново{owner_note}",
        parse_mode="Markdown"
    )


async def brief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)
    session["mode"] = "brief"
    session["history"] = []

    await update.message.reply_text(
        "📋 *Режим брифа*\n\n"
        "Вставляй сообщения от клиента — дословно или пересказом.\n"
        "Я буду задавать уточняющие вопросы.\n\n"
        "Вставь первое сообщение клиента:",
        parse_mode="Markdown"
    )


async def kp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session.get("brief_data"):
        await update.message.reply_text(
            "⚠️ Сначала собери бриф через /brief.\n"
            "Или вставь данные о проекте прямо сейчас:"
        )
        session["mode"] = "kp_manual"
        return

    session["mode"] = "kp"
    await _generate_kp(update, context, session["brief_data"])


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session["history"] and not session["brief_data"]:
        await update.message.reply_text("Сессия пустая. Начни с /brief")
        return

    mode_label = {"brief": "📋 Сбор брифа", "kp": "📄 КП", None: "—"}.get(session["mode"], "—")
    text = f"*Сессия:* {mode_label}\n*Сообщений:* {len(session['history'])}\n"
    if session["brief_data"]:
        text += "\n✅ Бриф собран — готов к /kp"
    await update.message.reply_text(text, parse_mode="Markdown")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    reset_session(update.effective_user.id)
    await update.message.reply_text("🔄 Сессия сброшена.")


# ── Обработка сообщений ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return

    user_id = update.effective_user.id
    text = update.message.text
    session = get_session(user_id)
    mode = session.get("mode")

    if mode == "brief":
        await handle_brief(update, session, text)
    elif mode == "kp_manual":
        session["brief_data"] = text
        session["mode"] = "kp"
        await _generate_kp(update, context, text)
    elif mode is None:
        # Свободный вопрос
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system="Ты ассистент студии unrlly. Помогай команде кратко и по делу.",
            messages=[{"role": "user", "content": text}],
        )
        await update.message.reply_text(response.content[0].text, parse_mode="Markdown")


async def handle_brief(update: Update, session: dict, text: str):
    session["history"].append({"role": "user", "content": text})

    thinking = await update.message.reply_text("⏳")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=BRIEF_SYSTEM,
        messages=session["history"],
    )
    reply = response.content[0].text
    await thinking.delete()

    if "[BRIEF_READY]" in reply:
        clean = reply.replace("[BRIEF_READY]", "").strip()
        session["brief_data"] = clean
        session["mode"] = None

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Генерировать КП", callback_data="gen_kp"),
            InlineKeyboardButton("✏️ Добавить инфо", callback_data="add_info"),
        ]])
        await update.message.reply_text(
            clean + "\n\n✅ *Бриф готов!*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        session["history"].append({"role": "assistant", "content": reply})
        await update.message.reply_text(
            f"❓ *Вопрос для клиента:*\n\n{reply}\n\n↩️ Вставь ответ клиента:",
            parse_mode="Markdown"
        )


async def _generate_kp(update: Update, context: ContextTypes.DEFAULT_TYPE, brief_data: str):
    """
    Генерирует два варианта КП:
    - Публичное (без цены) — отправляется в текущий чат, видят все
    - Полное (с плейсхолдером цены) — отправляется только владельцу в личку
    """
    user_id = update.effective_user.id
    thinking = await update.message.reply_text("⏳ Генерирую КП...")

    # Публичное КП — без цены
    public_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=KP_PUBLIC_SYSTEM,
        messages=[{"role": "user", "content": f"Бриф:\n\n{brief_data}"}],
    )
    public_kp = public_response.content[0].text

    await thinking.delete()

    # Отправляем публичное КП в текущий чат
    if len(public_kp) > 4000:
        await update.message.reply_text(public_kp[:4000], parse_mode="Markdown")
        await update.message.reply_text(public_kp[4000:], parse_mode="Markdown")
    else:
        await update.message.reply_text(public_kp, parse_mode="Markdown")

    # Полное КП с ценой — только владельцу в личку
    if OWNER_ID and OWNER_ID != 0:
        full_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=KP_FULL_SYSTEM,
            messages=[{"role": "user", "content": f"Бриф:\n\n{brief_data}"}],
        )
        full_kp = full_response.content[0].text

        header = "🔒 *Полное КП с ценой — только для тебя:*\n\n"
        try:
            if len(full_kp) > 3800:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=header + full_kp[:3800],
                    parse_mode="Markdown"
                )
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=full_kp[3800:],
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=header + full_kp,
                    parse_mode="Markdown"
                )
            # Уведомляем в текущем чате что полное КП отправлено
            if user_id != OWNER_ID:
                await update.message.reply_text(
                    "📨 Полное КП с ценой отправлено владельцу в личку."
                )
        except Exception as e:
            logger.error(f"Не удалось отправить КП владельцу: {e}")
            await update.message.reply_text(
                "⚠️ Не удалось отправить полное КП в личку. "
                "Убедись что ты написал боту /start в личной переписке."
            )

    await update.message.reply_text(
        "⚠️ *Не забудь:* в полном КП замени `[СУММА]` на финальную цифру.",
        parse_mode="Markdown"
    )


# ── Кнопки ────────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = get_session(user_id)

    if query.data == "gen_kp":
        await query.edit_message_reply_markup(reply_markup=None)
        await _generate_kp(query.message, context, session.get("brief_data", ""))

    elif query.data == "add_info":
        session["mode"] = "brief"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Вставь следующее сообщение от клиента:")


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brief", brief_cmd))
    app.add_handler(CommandHandler("kp", kp_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Studio bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
