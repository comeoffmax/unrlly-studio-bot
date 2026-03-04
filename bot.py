"""
unrlly Studio Bot — внутренний инструмент команды
Макс или Катя вставляют сообщения от клиента, бот задаёт уточняющие вопросы
и формирует структурированный бриф или КП.

/brief  — режим сбора брифа (диалог с уточнениями)
/kp     — сгенерировать КП по собранному брифу
/reset  — сбросить текущую сессию
/status — посмотреть что уже собрано
"""

import os
import json
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

# ── Конфиг ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Белый список: Telegram user_id через запятую. Пример: "123456789,987654321"
_ids = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x.strip()) for x in _ids.split(",") if x.strip()}

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Промпты ───────────────────────────────────────────────────────────────────

BRIEF_SYSTEM = """Ты — ассистент продуктовой студии unrlly (сайты, UI/UX, motion, webapp).
Работаешь с командой студии (Макс — владелец, Катя — дизайнер/PM).

Твоя задача в режиме BRIEF:
Команда вставляет тебе сообщения от клиента. Ты:
1. Анализируешь что уже известно
2. Задаёшь ОДИН уточняющий вопрос — тот, без которого нельзя оценить задачу
3. Команда берёт этот вопрос и задаёт его клиенту, потом вставляет ответ

Когда информации достаточно для оценки — пиши [BRIEF_READY] и структурируй бриф.

Нужно собрать:
- Что делаем (тип: сайт / motion / UI / webapp)
- Для кого (сфера, продукт)
- Цель результата
- Объём (экраны / секунды / функционал)
- Дедлайн
- Бюджетный диапазон (до 100к / 100–200к / 200к+ / не знают)
- Референсы
- ЛПР (кто принимает решение)

Говори кратко. Не объясняй свои действия, просто задавай вопрос или выдавай результат.
Язык: русский."""

BRIEF_READY_SYSTEM = """Структурируй собранные данные в бриф для команды дизайн-студии unrlly.

Формат (строго):
## 📋 Бриф — [название проекта или компании]
**Дата:** [сегодня]

**Тип проекта:** Site Basic / Site Plus / Motion Lite / Motion Full / UI/UX / Webapp / Уточнить
**Задача:** [1–2 предложения]
**Для кого:** [сфера, аудитория]
**Цель результата:** [что должно произойти после]
**Объём:** [экраны / секунды / функционал]
**Дедлайн:** [дата или «не указан»]
**Бюджет клиента:** [диапазон или «не обсуждали»]
**Референсы:** [ссылки/описание или «нет»]
**ЛПР:** [имя, контакт]

---
**⚡ Следующий шаг:** [квалификация / оценка / отправить бриф команде]
**⚠️ Риски:** [что неясно / что может вырасти]

Будь лаконичен."""

KP_SYSTEM = """Ты генерируешь коммерческое предложение для клиента студии unrlly.

Правила:
- Финальную стоимость НЕ включай — её добавит Макс вручную (место помечай [ЦЕНА])
- Пиши на языке клиента (русский если не указано иное)
- Тон: профессиональный, дружелюбный, без воды
- Структура КП строго по шаблону ниже

Шаблон КП:
---
## Коммерческое предложение — [название проекта]
*unrlly — [дата]*

### О проекте
[2–3 предложения: что делаем, для кого, зачем]

### Что входит в работу
[список блоков задач с кратким описанием]
*Что НЕ входит: [явно перечисли]*

### Таймлайн
| Этап | Срок | Результат |
|------|------|-----------|
[этапы]

### Стоимость
| Пакет | Стоимость |
|-------|-----------|
| [название] | [ЦЕНА] |

Оплата: 50% предоплата → 30% после макетов → 20% после сдачи.

### Риски и допущения
| Риск | Влияние | Решение |
|------|---------|---------|
| Задержка материалов от клиента | Сдвиг сроков | Фиксируем письменно |
| Изменение объёма | Стоимость растёт | Доп. работы — отдельный договор |
[добавь специфичные для проекта]

### Дополнительные услуги
- Доп. экраны / правки вне объёма: 4 000 ₽/час
- Срочность (дедлайн < 1 нед): × 1.5

---
*Для старта: ответьте «Согласовано» и мы пришлём счёт на предоплату.*
---"""

# ── Хранилище сессий ─────────────────────────────────────────────────────────

# user_id -> {"mode": "brief"|"kp"|None, "history": [...], "brief_data": str}
sessions: dict[int, dict] = {}

def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {"mode": None, "history": [], "brief_data": None}
    return sessions[user_id]

def reset_session(user_id: int):
    sessions[user_id] = {"mode": None, "history": [], "brief_data": None}

# ── Auth ──────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True  # если список пустой — пускаем всех (для теста)
    return user_id in ALLOWED_IDS

# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return

    text = (
        "👋 *unrlly Studio Bot*\n\n"
        "Вставляй сообщения от клиента, я помогу разобраться.\n\n"
        "Команды:\n"
        "• /brief — начать сбор брифа\n"
        "• /kp — сгенерировать КП по брифу\n"
        "• /status — что уже собрано\n"
        "• /reset — начать заново\n\n"
        f"Твой Telegram ID: `{update.effective_user.id}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def brief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)
    session["mode"] = "brief"
    session["history"] = []

    await update.message.reply_text(
        "📋 *Режим брифа*\n\n"
        "Вставляй сообщения от клиента — дословно или пересказом. "
        "Я буду задавать уточняющие вопросы пока не соберу всё нужное.\n\n"
        "Начни — вставь первое сообщение клиента:",
        parse_mode="Markdown"
    )


async def kp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session.get("brief_data"):
        await update.message.reply_text(
            "⚠️ Сначала собери бриф через /brief.\n"
            "Или вставь данные вручную прямо сейчас — и я сгенерирую КП."
        )
        session["mode"] = "kp_manual"
        return

    session["mode"] = "kp"
    await _generate_kp(update, session["brief_data"])


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session["history"] and not session["brief_data"]:
        await update.message.reply_text("Сессия пустая. Начни с /brief")
        return

    mode_label = {"brief": "📋 Сбор брифа", "kp": "📄 КП", None: "—"}.get(session["mode"], "—")
    msgs = len(session["history"])

    text = f"*Текущая сессия*\nРежим: {mode_label}\nСообщений: {msgs}\n"
    if session["brief_data"]:
        text += "\n✅ Бриф собран — готов к /kp"
    await update.message.reply_text(text, parse_mode="Markdown")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    reset_session(update.effective_user.id)
    await update.message.reply_text("🔄 Сессия сброшена. Начни с /brief или /kp")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return

    user_id = update.effective_user.id
    text = update.message.text
    session = get_session(user_id)

    mode = session.get("mode")

    if mode == "brief":
        await handle_brief_message(update, session, text)
    elif mode == "kp_manual":
        session["brief_data"] = text
        session["mode"] = "kp"
        await _generate_kp(update, text)
    elif mode is None:
        # Свободный режим — помогаем без конкретного режима
        await handle_free_message(update, session, text)
    else:
        await update.message.reply_text(
            "Используй /brief для брифа или /kp для КП.\n/reset — начать заново."
        )


async def handle_brief_message(update: Update, session: dict, text: str):
    """Обрабатывает сообщение в режиме сбора брифа."""
    session["history"].append({"role": "user", "content": text})

    # Показываем что думаем
    thinking_msg = await update.message.reply_text("⏳")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=BRIEF_SYSTEM,
        messages=session["history"],
    )
    reply = response.content[0].text

    await thinking_msg.delete()

    if "[BRIEF_READY]" in reply:
        # Финализируем бриф
        clean = reply.replace("[BRIEF_READY]", "").strip()
        session["brief_data"] = clean
        session["mode"] = None  # выходим из режима брифа

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
        # Форматируем вопрос красиво
        await update.message.reply_text(
            f"❓ *Уточняющий вопрос для клиента:*\n\n_{reply}_\n\n"
            "↩️ Вставь ответ клиента:",
            parse_mode="Markdown"
        )


async def handle_free_message(update: Update, session: dict, text: str):
    """Свободный режим — контекстная помощь."""
    brief_ctx = f"\n\nСобранный бриф:\n{session['brief_data']}" if session.get("brief_data") else ""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=(
            "Ты ассистент студии unrlly. Помогай команде с вопросами по проектам, "
            "клиентам, ценообразованию. Кратко и по делу." + brief_ctx
        ),
        messages=[{"role": "user", "content": text}],
    )
    await update.message.reply_text(
        response.content[0].text,
        parse_mode="Markdown"
    )


async def _generate_kp(update: Update, brief_data: str):
    """Генерирует КП на основе брифа."""
    thinking_msg = await update.message.reply_text("⏳ Генерирую КП...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=KP_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Сгенерируй КП на основе брифа:\n\n{brief_data}"
        }],
    )
    kp_text = response.content[0].text

    await thinking_msg.delete()

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Скопировать текст", callback_data="copy_kp"),
        InlineKeyboardButton("🔄 Перегенерировать", callback_data="regen_kp"),
    ]])

    # Telegram ограничивает 4096 символов — режем если нужно
    if len(kp_text) > 4000:
        await update.message.reply_text(kp_text[:4000], parse_mode="Markdown")
        await update.message.reply_text(
            kp_text[4000:],
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            kp_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    # Напоминание про цену
    await update.message.reply_text(
        "⚠️ *Не забудь:* замени `[ЦЕНА]` на финальную стоимость перед отправкой клиенту.",
        parse_mode="Markdown"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = get_session(user_id)

    if query.data == "gen_kp":
        await query.edit_message_reply_markup(reply_markup=None)
        await _generate_kp(query.message, session.get("brief_data", ""))

    elif query.data == "add_info":
        session["mode"] = "brief"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Добавь информацию — вставь следующее сообщение от клиента:"
        )

    elif query.data == "regen_kp":
        await query.edit_message_reply_markup(reply_markup=None)
        await _generate_kp(query.message, session.get("brief_data", ""))

    elif query.data == "copy_kp":
        await query.answer("Выдели текст выше и скопируй", show_alert=True)


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
