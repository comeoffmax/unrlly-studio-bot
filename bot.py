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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
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

_ids = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x.strip()) for x in _ids.split(",") if x.strip()}

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Тарифная сетка (из finmodel v3) ──────────────────────────────────────────
#
# Ставки ₽/час по типу работы:
#   Катя — UI/UX базовый (экраны, промо):              3 000
#   Катя — UI/UX сложный (дизайн-система/webapp):      4 000
#   Катя — Брендинг / айдентика:                       4 500
#   Катя — AI-иллюстрации:                             3 000
#   Катя — PM (совмещение):                            2 200
#   Стас — UI/UX / брендинг – стандарт:                3 500
#   Стас — UI/UX – сложный / дизайн-система:           4 500
#   Эд   — UI/UX базовый:                              3 000
#   Эд   — Верстка Tilda – стандарт:                   4 000
#   Эд   — Webflow / кастом + анимации:                5 000
#   Эд   — Motion 2D:                                  4 000
#   Макс — AD-ревью (финальная проверка):               6 000
#   Нео  — Motion 2D / 3D:                             уточнить (в смете 0)
#   Алексей Х — Fullstack / ML:                        уточнить (в смете 0)
#
# Маржа K = 1.5 (типовой проект), налог 15%
# Цена клиенту = Себестоимость × 1.5 × 1.15, округление до 10 000
#
# БЕНЧМАРКИ ЧАСОВ по типам проектов (реалистичные, senior-уровень):
#
# Site Basic (до 5 экранов, Tilda):
#   Катя UI/UX базовый: 16–24ч  |  Эд Tilda: 16–20ч  |  Макс AD: 2ч
#   Себест: ~130–175к  |  Клиенту: ~225–300к  |  Срок: 10–14 дней
#
# Site Plus (до 10 экранов, Webflow):
#   Катя UI/UX базовый: 24–40ч  |  Эд Webflow: 24–32ч  |  Макс AD: 3ч
#   Себест: ~190–270к  |  Клиенту: ~330–465к  |  Срок: 14–24 дня
#
# Motion Lite (15–20 сек):
#   Эд Motion 2D: 20–30ч  |  Катя UI/UX: 6–8ч  |  Макс AD: 2ч
#   Себест: ~110–160к  |  Клиенту: ~190–275к  |  Срок: 5–8 дней
#
# Motion Full (30–45 сек):
#   Эд Motion 2D: 35–50ч  |  Катя UI/UX: 8–12ч  |  Макс AD: 3ч
#   Себест: ~160–230к  |  Клиенту: ~275–395к  |  Срок: 12–18 дней
#
# UI/UX / Дизайн-система:
#   Катя UI/UX сложный: 40–80ч  |  Стас сложный: 20–40ч  |  Макс AD: 4ч
#   Себест: ~285–545к  |  Клиенту: ~490–940к  |  Срок: 20–40 дней
#
# Лендинг простой (3–4 экрана, без разработки):
#   Катя UI/UX базовый: 10–14ч  |  Эд Tilda: 8–12ч  |  Макс AD: 1–2ч
#   Себест: ~80–110к  |  Клиенту: ~140–190к  |  Срок: 7–10 дней
#
# Допы: 4 000 ₽/час. Срочность: ×1.5.

RATES_CONTEXT = """
ТАРИФНАЯ СЕТКА unrlly (ставки ₽/час):
- Дизайн UI/UX базовый (экраны, промо): 3 000 ₽/ч
- Дизайн UI/UX сложный (webapp, система): 4 000 ₽/ч
- Дизайн брендинг/айдентика: 4 500 ₽/ч
- Верстка Tilda стандарт: 4 000 ₽/ч
- Верстка Webflow / кастом + анимации: 5 000 ₽/ч
- Motion 2D: 4 000 ₽/ч
- AD-ревью Макса: 6 000 ₽/ч (всегда 2–4 ч на проект)
- PM/коммуникация: 2 200 ₽/ч

ФОРМУЛА ЦЕНЫ:
Себестоимость = сумма (часы × ставка) по каждой позиции
Цена клиенту = Себестоимость × 1.5 (маржа) × 1.15 (налог УСН)
Округление: до 10 000 ₽

РЕАЛИСТИЧНЫЕ БЕНЧМАРКИ ЧАСОВ:

Лендинг простой (3–4 экрана, Tilda, без разработки):
  Дизайн UI/UX базовый: 10–14 ч
  Верстка Tilda: 8–12 ч
  AD-ревью: 2 ч
  → Себест: 82–110к | Клиенту: 140–190к | Срок: 7–10 дней

Site Basic (до 5 экранов + разработка Tilda):
  Дизайн UI/UX базовый: 16–24 ч
  Верстка Tilda: 16–20 ч
  AD-ревью: 2 ч
  → Себест: 128–175к | Клиенту: 220–300к | Срок: 10–14 дней

Site Plus (до 10 экранов, Webflow):
  Дизайн UI/UX базовый: 24–40 ч
  Верстка Webflow: 24–32 ч
  AD-ревью: 3 ч
  → Себест: 192–272к | Клиенту: 330–470к | Срок: 14–24 дня

Motion Lite (15–20 сек):
  Motion 2D: 20–30 ч
  Дизайн UI/UX базовый: 6–8 ч
  AD-ревью: 2 ч
  → Себест: 110–158к | Клиенту: 190–270к | Срок: 5–8 дней

Motion Full (30–45 сек):
  Motion 2D: 35–50 ч
  Дизайн UI/UX базовый: 8–12 ч
  AD-ревью: 3 ч
  → Себест: 158–230к | Клиенту: 270–395к | Срок: 12–18 дней

UI/UX / Дизайн-система:
  Дизайн UI/UX сложный: 40–80 ч
  Дизайн UI/UX стандарт (Стас): 20–40 ч
  AD-ревью: 4 ч
  → Себест: 294–544к | Клиенту: 505–940к | Срок: 20–40 дней

Доп. правки вне объёма: 4 000 ₽/ч
Срочность (дедлайн < 1 нед): × 1.5 к итоговой цене
"""

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

KP_SYSTEM = f"""Ты генерируешь коммерческое предложение для клиента студии unrlly.

{RATES_CONTEXT}

ПРАВИЛА РАСЧЁТА:
1. Определи тип проекта по брифу
2. Выбери реалистичный диапазон часов из бенчмарков выше (не завышай — берёшь середину диапазона)
3. Посчитай себестоимость по формуле: сумма (часы × ставка)
4. Посчитай цену клиенту: Себестоимость × 1.5 × 1.15, округли до 10 000 ₽
5. В таймлайне указывай РАБОЧИЕ ДНИ (не часы), исходя из загрузки ~6–8 ч/день

ПРАВИЛА ОФОРМЛЕНИЯ:
- Тон: профессиональный, конкретный, без воды
- В таймлайне — этапы в РАБОЧИХ ДНЯХ
- В смете — позиции с часами и ставками, итоговая сумма
- Пиши на русском

ШАБЛОН КП:
---
## Коммерческое предложение — [название проекта]
*unrlly · [дата сегодня]*

### О проекте
[2–3 предложения: что делаем, для кого, зачем]

### Что входит
[список позиций]
*Не входит: тексты, фото, домен/хостинг, [специфичное для проекта]*

### Таймлайн
| Этап | Рабочих дней | Результат |
|------|-------------|-----------|
[этапы — конкретные дни, не «1–2 недели»]
| **Итого** | **[X дней]** | |

### Смета
| Позиция | Часы | Ставка | Сумма |
|---------|------|--------|-------|
[каждая позиция отдельной строкой]
| | | **Себестоимость** | **[X ₽]** |
| | | **Итого клиенту** | **[X ₽]** |

*Оплата: 50% предоплата → 30% после макетов → 20% при сдаче*

### Риски
| Риск | Решение |
|------|---------|
| Задержка материалов от клиента | Переносим сроки, уведомляем письменно |
| Расширение объёма | Доп. работы — отдельный договор, 4 000 ₽/ч |
[добавь 1–2 специфичных для проекта]

### Дополнительно
- Правки вне объёма: 4 000 ₽/час
- Срочность (дедлайн < 7 дней): × 1.5
---
*Для старта — ответьте «Согласовано», пришлём счёт на предоплату.*
---"""

# ── Хранилище сессий ─────────────────────────────────────────────────────────

sessions: dict[int, dict] = {}

def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {"mode": None, "history": [], "brief_data": None, "last_kp": None}
    return sessions[user_id]

def reset_session(user_id: int):
    sessions[user_id] = {"mode": None, "history": [], "brief_data": None, "last_kp": None}

# ── Auth ──────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
    return user_id in ALLOWED_IDS

# ── Генерация КП (принимает Message, работает и с обычным сообщением и с callback) ──

async def _generate_kp(message: Message, brief_data: str, session: dict):
    """Генерирует КП. message — объект telegram.Message."""
    thinking_msg = await message.reply_text("⏳ Считаю часы и сумму...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2500,
        system=KP_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Сгенерируй КП на основе брифа:\n\n{brief_data}"
        }],
    )
    kp_text = response.content[0].text
    session["last_kp"] = kp_text

    await thinking_msg.delete()

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Перегенерировать", callback_data="regen_kp"),
        InlineKeyboardButton("✏️ Скорректировать", callback_data="adjust_kp"),
    ]])

    # Telegram ограничивает 4096 символов — режем если нужно
    if len(kp_text) > 4000:
        await message.reply_text(kp_text[:4000], parse_mode="Markdown")
        await message.reply_text(
            kp_text[4000:],
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await message.reply_text(
            kp_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    await message.reply_text(
        "💡 *Проверь суммы* перед отправкой клиенту — особенно если проект нестандартный.\n"
        "Нажми «Скорректировать» чтобы изменить объём или условия.",
        parse_mode="Markdown"
    )

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

    await _generate_kp(update.message, session["brief_data"], session)


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
    if session["last_kp"]:
        text += "\n📄 КП сгенерировано"
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
        session["mode"] = None
        await _generate_kp(update.message, text, session)
    elif mode == "adjust_kp":
        # Пользователь вводит корректировку к КП
        await handle_kp_adjustment(update, session, text)
    elif mode is None:
        await handle_free_message(update, session, text)
    else:
        await update.message.reply_text(
            "Используй /brief для брифа или /kp для КП.\n/reset — начать заново."
        )


async def handle_brief_message(update: Update, session: dict, text: str):
    """Обрабатывает сообщение в режиме сбора брифа."""
    session["history"].append({"role": "user", "content": text})

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
        clean = reply.replace("[BRIEF_READY]", "").strip()
        session["brief_data"] = clean
        session["mode"] = None

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Подготовить КП", callback_data="gen_kp"),
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
            f"❓ *Вопрос для клиента:*\n\n_{reply}_\n\n"
            "↩️ Вставь ответ клиента:",
            parse_mode="Markdown"
        )


async def handle_kp_adjustment(update: Update, session: dict, text: str):
    """Корректирует КП по инструкции пользователя."""
    if not session.get("last_kp"):
        await update.message.reply_text("Нет КП для корректировки. Сначала сгенерируй /kp")
        session["mode"] = None
        return

    thinking_msg = await update.message.reply_text("⏳ Корректирую...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2500,
        system=KP_SYSTEM,
        messages=[
            {"role": "user", "content": f"Исходный бриф:\n\n{session['brief_data']}"},
            {"role": "assistant", "content": session["last_kp"]},
            {"role": "user", "content": f"Скорректируй КП: {text}"},
        ],
    )
    kp_text = response.content[0].text
    session["last_kp"] = kp_text
    session["mode"] = None

    await thinking_msg.delete()

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Перегенерировать", callback_data="regen_kp"),
        InlineKeyboardButton("✏️ Скорректировать", callback_data="adjust_kp"),
    ]])

    if len(kp_text) > 4000:
        await update.message.reply_text(kp_text[:4000], parse_mode="Markdown")
        await update.message.reply_text(kp_text[4000:], parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(kp_text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_free_message(update: Update, session: dict, text: str):
    """Свободный режим — контекстная помощь."""
    brief_ctx = f"\n\nСобранный бриф:\n{session['brief_data']}" if session.get("brief_data") else ""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=(
            "Ты ассистент студии unrlly. Помогай команде с вопросами по проектам, "
            "клиентам, ценообразованию. Кратко и по делу.\n\n"
            + RATES_CONTEXT + brief_ctx
        ),
        messages=[{"role": "user", "content": text}],
    )
    await update.message.reply_text(
        response.content[0].text,
        parse_mode="Markdown"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = get_session(user_id)

    if query.data == "gen_kp":
        # FIX: убираем клавиатуру и передаём query.message (не update.message)
        await query.edit_message_reply_markup(reply_markup=None)
        brief = session.get("brief_data", "")
        if not brief:
            await query.message.reply_text("⚠️ Бриф не найден. Начни с /brief")
            return
        await _generate_kp(query.message, brief, session)

    elif query.data == "add_info":
        session["mode"] = "brief"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Добавь информацию — вставь следующее сообщение от клиента:"
        )

    elif query.data == "regen_kp":
        await query.edit_message_reply_markup(reply_markup=None)
        brief = session.get("brief_data", "")
        if not brief:
            await query.message.reply_text("⚠️ Бриф не найден. Начни с /brief")
            return
        await _generate_kp(query.message, brief, session)

    elif query.data == "adjust_kp":
        session["mode"] = "adjust_kp"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "✏️ Напиши что скорректировать.\n"
            "Например: «уменьши объём до 3 экранов» или «добавь motion-анимацию»"
        )


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
