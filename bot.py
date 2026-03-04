"""
unrlly Studio Bot v3
- Вставляй всё от клиента одним куском
- Бот читает ссылки, определяет тип, вытаскивает суть
- Структурирует бриф и задаёт максимум 3 вопроса
- КП: публичное всем, полное с ценой — только владельцу в личку
"""

import os
import re
import httpx
import logging
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_ID          = int(os.environ.get("OWNER_ID", "0"))
_ids              = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x.strip()) for x in _ids.split(",") if x.strip()}

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Промпты ───────────────────────────────────────────────────────────────────

BRIEF_SYSTEM = """Ты — ассистент студии unrlly (сайты, UI/UX, motion, webapp).

Команда вставляет тебе всё что есть от клиента: переписку, ссылки с их содержимым, описания.
Твоя задача — сделать две вещи СРАЗУ:

1. СТРУКТУРИРОВАТЬ бриф из всего что есть
2. Задать максимум 3 уточняющих вопроса — только самые критичные

Если в данных есть содержимое ссылок — используй его для понимания контекста клиента.

Формат ответа строго:

## 📋 Бриф — [название проекта или компании]
**Тип:** [Site Basic / Site Plus / Motion Lite / Motion Full / UI/UX / Webapp / Уточнить]
**Задача:** [суть в 1–2 предложениях]
**Для кого:** [сфера, продукт, аудитория]
**Цель результата:** [что должно произойти после запуска]
**Объём:** [экраны / секунды / функционал — или «уточнить»]
**Дедлайн:** [дата или «не указан»]
**Бюджет:** [диапазон или «не обсуждали»]
**ЛПР:** [имя/контакт или «неизвестен»]

🔗 **Ссылки:**
• [url] — [тип: их сайт / референс / Figma / соцсеть / видео] — [1 строка: что там]

❓ **Нужно уточнить:**
1. [вопрос]
2. [вопрос]
3. [вопрос — только если реально критично]

⚠️ **Риски:** [что может вырасти в объём или цену]

Язык: русский. Конкретно и кратко."""

FOLLOWUP_SYSTEM = """Ты ассистент студии unrlly. Команда добавляет новую информацию по проекту.
Обнови бриф — внеси новые данные, убери закрытые вопросы, добавь новые ссылки если есть.
Если все ключевые вопросы закрыты — добавь [BRIEF_COMPLETE] в самом конце.
Используй тот же формат брифа. Язык: русский."""

KP_PUBLIC_SYSTEM = """КП для студии unrlly — БЕЗ стоимости.
Разделы: обзор проекта, объём работ (что входит и НЕ входит), таймлайн таблицей, риски.
В конце: «💰 Стоимость согласовывается индивидуально.»
Профессиональный тон, без воды. Язык: русский."""

KP_FULL_SYSTEM = """Полное КП для студии unrlly СО стоимостью.
Разделы: обзор, объём (что входит и НЕ входит), таймлайн таблицей, стоимость, риски, доп.услуги.
Стоимость: плейсхолдер [СУММА] — владелец заменит вручную.
Оплата: 50% предоплата → 30% после макетов → 20% после сдачи.
Доп.правки: 4 000 ₽/час. Срочность < 1 нед: ×1.5. Язык: русский."""

# ── URL fetching ──────────────────────────────────────────────────────────────

def detect_url_type(url: str) -> str:
    u = url.lower()
    if "figma.com"    in u: return "Figma"
    if "youtube.com"  in u or "youtu.be" in u: return "видео (YouTube)"
    if "vimeo.com"    in u: return "видео (Vimeo)"
    if "instagram.com" in u: return "соцсеть (Instagram)"
    if "t.me"         in u or "telegram" in u: return "Telegram"
    if "behance.net"  in u: return "портфолио (Behance)"
    if "dribbble.com" in u: return "портфолио (Dribbble)"
    if "notion.so"    in u: return "документ (Notion)"
    if "docs.google"  in u: return "документ (Google Docs)"
    return "сайт"

async def fetch_url_content(url: str) -> str:
    """Загружает страницу и возвращает первые ~2000 символов текста."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; unrlly-bot/1.0)"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                return f"[не удалось загрузить, статус {r.status_code}]"
            text = r.text
            # Убираем теги
            text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
            text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:2000] if len(text) > 2000 else text
    except Exception as e:
        return f"[ошибка загрузки: {str(e)[:80]}]"

async def process_urls(urls: list[str], status_msg) -> str:
    """Загружает все ссылки и возвращает сводку для промпта."""
    if not urls:
        return ""

    await status_msg.edit_text(f"⏳ Читаю {len(urls)} ссылк{'у' if len(urls)==1 else 'и'}...")

    parts = []
    for url in urls[:5]:  # максимум 5 ссылок
        url_type = detect_url_type(url)
        content  = await fetch_url_content(url)
        parts.append(f"[{url_type}] {url}\nСодержимое: {content}")

    return "\n\n---\n".join(parts)

# ── Хранилище ─────────────────────────────────────────────────────────────────

sessions: dict[int, dict] = {}

def get_session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {"mode": None, "brief_data": None, "history": []}
    return sessions[uid]

def reset_session(uid: int):
    sessions[uid] = {"mode": None, "brief_data": None, "history": []}

def is_allowed(uid: int) -> bool:
    return not ALLOWED_IDS or uid in ALLOWED_IDS

# ── Команды ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return
    owner_note = "\n👑 Полное КП с ценой — только тебе в личку." if update.effective_user.id == OWNER_ID else ""
    await update.message.reply_text(
        "👋 *unrlly Studio Bot*\n\n"
        "Вставляй всё что есть от клиента — переписку, ссылки, описание.\n"
        "Бот прочитает ссылки, структурирует бриф и скажет что уточнить.\n\n"
        "• /brief — начать\n"
        "• /kp — сгенерировать КП\n"
        "• /reset — начать заново" + owner_note,
        parse_mode="Markdown"
    )

async def brief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    session = get_session(update.effective_user.id)
    session["mode"] = "brief"
    session["history"] = []
    session["brief_data"] = None
    await update.message.reply_text(
        "📋 *Режим брифа*\n\n"
        "Вставляй сюда всё что есть от клиента одним сообщением:\n"
        "— переписку целиком\n"
        "— ссылки на сайт, референсы, Figma\n"
        "— любое описание задачи\n\n"
        "Бот прочитает ссылки и структурирует бриф 👇",
        parse_mode="Markdown"
    )

async def kp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    session = get_session(update.effective_user.id)
    if not session.get("brief_data"):
        await update.message.reply_text("⚠️ Сначала собери бриф через /brief.\nИли вставь данные прямо сейчас:")
        session["mode"] = "kp_manual"
        return
    await _generate_kp(update, context, session["brief_data"])

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    reset_session(update.effective_user.id)
    await update.message.reply_text("🔄 Сброшено. Начни с /brief")

# ── Обработка сообщений ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return

    uid     = update.effective_user.id
    text    = update.message.text
    session = get_session(uid)
    mode    = session.get("mode")
    urls    = re.findall(r'https?://[^\s\)\]\>\"]+', text)

    if mode in ("brief", "brief_followup"):
        await _process_input(update, session, text, urls, is_followup=(mode == "brief_followup"))
    elif mode == "kp_manual":
        session["brief_data"] = text
        session["mode"] = None
        await _generate_kp(update, context, text)
    else:
        response = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=600,
            system="Ты ассистент студии unrlly. Кратко и по делу. Язык: русский.",
            messages=[{"role": "user", "content": text}],
        )
        await update.message.reply_text(response.content[0].text, parse_mode="Markdown")


async def _process_input(update, session, text, urls, is_followup: bool):
    status = await update.message.reply_text("⏳ Обрабатываю...")

    # Читаем ссылки если есть
    url_context = ""
    if urls:
        url_context = "\n\n=== СОДЕРЖИМОЕ ССЫЛОК ===\n" + await process_urls(urls, status)

    await status.edit_text("⏳ Структурирую бриф...")

    system   = FOLLOWUP_SYSTEM if is_followup else BRIEF_SYSTEM
    messages = session["history"] + [{"role": "user", "content": text + url_context}]

    response = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1400,
        system=system, messages=messages,
    )
    reply = response.content[0].text
    await status.delete()

    is_complete = "[BRIEF_COMPLETE]" in reply
    clean = reply.replace("[BRIEF_COMPLETE]", "").strip()

    session["brief_data"] = clean
    session["history"]    = messages + [{"role": "assistant", "content": clean}]

    if is_complete:
        session["mode"] = None
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Генерировать КП", callback_data="gen_kp"),
        ]])
        await update.message.reply_text(clean + "\n\n✅ *Бриф готов!*", parse_mode="Markdown", reply_markup=keyboard)
    else:
        session["mode"] = "brief_followup"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 КП без доработки", callback_data="gen_kp"),
            InlineKeyboardButton("✅ Бриф готов",        callback_data="brief_done"),
        ]])
        await update.message.reply_text(clean, parse_mode="Markdown", reply_markup=keyboard)
        await update.message.reply_text(
            "↩️ Вставь ответы на вопросы выше — или нажми «КП без доработки».",
            parse_mode="Markdown"
        )


async def _generate_kp(update, context, brief_data: str):
    uid     = update.effective_user.id
    status  = await update.message.reply_text("⏳ Генерирую КП...")

    public_resp = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=2000,
        system=KP_PUBLIC_SYSTEM,
        messages=[{"role": "user", "content": f"Бриф:\n\n{brief_data}"}],
    )
    await status.delete()

    for chunk in [public_resp.content[0].text[i:i+4000] for i in range(0, len(public_resp.content[0].text), 4000)]:
        await update.message.reply_text(chunk, parse_mode="Markdown")

    if OWNER_ID:
        full_resp = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2000,
            system=KP_FULL_SYSTEM,
            messages=[{"role": "user", "content": f"Бриф:\n\n{brief_data}"}],
        )
        full_kp = full_resp.content[0].text
        try:
            for i, chunk in enumerate([full_kp[j:j+3800] for j in range(0, len(full_kp), 3800)]):
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=("🔒 *Полное КП с ценой:*\n\n" if i == 0 else "") + chunk,
                    parse_mode="Markdown"
                )
            await context.bot.send_message(chat_id=OWNER_ID, text="⚠️ Замени `[СУММА]` перед отправкой.", parse_mode="Markdown")
            if uid != OWNER_ID:
                await update.message.reply_text("📨 Полное КП отправлено владельцу в личку.")
        except Exception as e:
            logger.error(f"Ошибка отправки КП: {e}")
            await update.message.reply_text("⚠️ Не удалось отправить КП в личку. Напиши боту /start в личной переписке.")

# ── Кнопки ────────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    session = get_session(uid)

    if query.data == "gen_kp":
        await query.edit_message_reply_markup(reply_markup=None)
        if session.get("brief_data"):
            await _generate_kp(query.message, context, session["brief_data"])
        else:
            await query.message.reply_text("Нет данных. Начни с /brief")
    elif query.data == "brief_done":
        session["mode"] = None
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Бриф зафиксирован.", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Генерировать КП", callback_data="gen_kp")
        ]]))

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brief", brief_cmd))
    app.add_handler(CommandHandler("kp",    kp_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Studio bot v3 started")
    app.run_polling()

if __name__ == "__main__":
    main()
