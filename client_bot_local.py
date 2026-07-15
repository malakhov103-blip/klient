# -*- coding: utf-8 -*-
"""
Binio — бот клиентов
Собирает анкету через кнопки (там где уместно) и текст (где нужен свободный ответ),
затем отправляет заявку в чат заявок.
"""

import asyncio
import hashlib
import logging
import os
import platform
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)
from telegram.error import NetworkError, RetryAfter, TimedOut

# ==================== НАСТРОЙКИ ====================


def clean_env(name, default=""):
    return os.getenv(name, default).strip()


BOT_TOKEN = clean_env("CLIENT_BOT_TOKEN")
REQUESTS_CHAT_ID_RAW = clean_env("CLIENT_REQUESTS_CHAT_ID", "-1003908716746")
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
BOT_DATA_FILE = clean_env("BOT_DATA_FILE", "client_bot_data.pickle")
BOT_DATA_PATH = os.path.join(BOT_DATA_DIR, BOT_DATA_FILE)
MAX_TEXT_ANSWER_LENGTH = int(os.getenv("MAX_TEXT_ANSWER_LENGTH", "700"))
REQUESTS_CHAT_ID = int(REQUESTS_CHAT_ID_RAW) if REQUESTS_CHAT_ID_RAW.lstrip("-").isdigit() else 0

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def is_transient_network_error(error) -> bool:
    """Распознаёт временные сбои Telegram/httpx, включая вложенную причину."""
    current = error
    seen = set()
    transient_httpx_names = {
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
    }
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (NetworkError, TimedOut, RetryAfter)):
            return True
        if current.__class__.__module__.startswith("httpx") and current.__class__.__name__ in transient_httpx_names:
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


async def telegram_call_with_retry(call_factory, label, attempts=3):
    """Повторяет только безопасные сетевые сбои, не скрывая ошибки данных/прав."""
    last_error = None
    for attempt in range(attempts):
        try:
            return await call_factory()
        except RetryAfter as error:
            last_error = error
            delay = min(30.0, float(error.retry_after) + 0.25)
        except Exception as error:
            if not is_transient_network_error(error):
                raise
            last_error = error
            delay = 0.8 * (attempt + 1)

        logger.warning(
            f"{label}: временная сетевая ошибка, попытка {attempt + 1}/{attempts}: "
            f"{type(last_error).__name__}: {last_error}"
        )
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    raise last_error


def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("CLIENT_BOT_TOKEN")
    if not REQUESTS_CHAT_ID:
        missing.append("CLIENT_REQUESTS_CHAT_ID")
    if missing:
        raise RuntimeError(
            "Не заполнены настройки Railway: " + ", ".join(missing) +
            ". Добавьте их во вкладке Variables именно у сервиса анкетного бота."
        )

# ==================== ВОПРОСЫ ====================
# kind: "single" (кнопки, одиночный выбор) / "multi" (кнопки, мульти-выбор)
#       "text" (свободный текст) / "contact" (кнопка "поделиться номером" + текст)
# allow_other: для "single" — добавляет кнопку "Другое" с текстовым вводом

STEPS = [
    {
        "key": "type", "kind": "single", "emoji": "🏠",
        "text": "Какой тип жилья вы ищете?",
        "options": ["Квартира", "Комната", "Дом", "Пока не важно"],
        "label": "Тип жилья", "allow_other": True,
    },
    {
        "key": "district", "kind": "multi", "emoji": "📍",
        "text": "Какие районы Праги вас интересуют?\nМожно выбрать несколько, затем нажать «Готово». Если нужен другой район, пригород или другой город, нажмите «Другой вариант».",
        "options": ["Praha 1", "Praha 2", "Praha 3", "Praha 4", "Praha 5",
                     "Praha 6", "Praha 7", "Praha 8", "Praha 9", "Praha 10",
                     "Praha 11-22", "Пригород Праги", "Не важно"],
        "label": "Локации", "allow_other": True,
    },
    {
        "key": "budget", "kind": "single", "emoji": "💰",
        "text": "Какой у вас максимальный бюджет в месяц, включая коммунальные платежи?",
        "options": ["до 18 000 Kč", "18 000–22 000 Kč", "22 000–27 000 Kč",
                     "27 000–35 000 Kč", "35 000+ Kč"],
        "label": "Бюджет", "allow_other": True,
    },
    {
        "key": "rooms", "kind": "multi", "emoji": "🚪",
        "text": "Какая планировка вам подойдёт?\nМожно выбрать несколько, затем нажать «Готово».\n\n(kk — кухня совмещена с комнатой, +1 — отдельная кухня)",
        "options": ["Комната", "Студия", "1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+ / больше", "Не важно"],
        "label": "Планировка", "allow_other": True,
    },
    {
        "key": "area", "kind": "single", "emoji": "📐",
        "text": "Какой метраж рассматриваете?",
        "options": ["до 30 м²", "30–50 м²", "50–70 м²", "70–100 м²", "100+ м²", "Не важно"],
        "label": "Метраж", "allow_other": True,
    },
    {
        "key": "move_in", "kind": "single", "emoji": "📅",
        "text": "Когда планируете заезд?",
        "options": "MOVE_IN_DYNAMIC",
        "label": "Заезд", "allow_other": True,
    },
    {
        "key": "age", "kind": "single", "emoji": "🎂",
        "text": "Сколько вам лет?",
        "options": ["до 25", "25–34", "35–44", "45–54", "55+"],
        "label": "Возраст", "allow_other": True,
    },
    {
        "key": "people", "kind": "single", "emoji": "👥",
        "text": "Сколько человек будет проживать?",
        "options": ["1", "2", "3", "4", "5+", "Пока не знаю"],
        "label": "Кол-во проживающих", "allow_other": True,
    },
    {
        "key": "from_country", "kind": "text", "emoji": "🌍",
        "text": "Откуда вы?\nМожно написать страну и город.",
        "label": "Откуда",
    },
    {
        "key": "how_long", "kind": "single", "emoji": "⏳",
        "text": "Как давно вы находитесь в Чехии?",
        "options": ["Ещё не приехал(а)", "Менее 1 месяца", "1–6 месяцев",
                     "6–12 месяцев", "1–5 лет", "Более 5 лет", "Живу в Чехии постоянно"],
        "label": "Как давно в Чехии", "allow_other": True,
    },
    {
        "key": "citizenship", "kind": "text", "emoji": "🛂",
        "text": "Какое у вас гражданство?",
        "label": "Гражданство",
    },
    {
        "key": "visa", "kind": "single", "emoji": "📄",
        "text": "Какой у вас тип визы или вида на жительство?",
        "options": ["Студенческая виза", "Рабочая виза/ВНЖ", "Бизнес-виза",
                     "ВНЖ (другое основание)", "ПМЖ", "Гражданство Чехии",
                     "Гражданство ЕС", "Беженская виза", "В процессе оформления"],
        "label": "Виза/ВНЖ", "allow_other": True,
    },
    {
        "key": "kids", "kind": "single", "emoji": "👶",
        "text": "Есть ли у вас дети?",
        "options": ["Нет", "Да, 1 ребёнок", "Да, 2+ детей", "Планируем"],
        "label": "Дети", "allow_other": True,
    },
    {
        "key": "pets", "kind": "single", "emoji": "🐾",
        "text": "Есть ли у вас животные?",
        "options": ["Нет", "Кошка", "Маленькая собака", "Средняя/крупная собака", "Другое животное"],
        "label": "Животные", "allow_other": True,
    },
    {
        "key": "work", "kind": "text", "emoji": "💼",
        "text": "Где вы работаете или учитесь?\nМожно коротко: должность, компания/сфера, студент, OSVČ или другой вариант.",
        "label": "Работа/учёба",
    },
    {
        "key": "income", "kind": "single", "emoji": "💵",
        "text": "Какой у вас официальный доход?",
        "options": ["до 25 000 Kč", "25 000–35 000 Kč", "35 000–50 000 Kč",
                     "50 000+ Kč", "OSVČ / предприниматель", "Доход за границей",
                     "Пока нет официального дохода", "Не могу подтвердить документально"],
        "label": "Официальный доход", "allow_other": True,
    },
    {
        "key": "term", "kind": "single", "emoji": "🗓",
        "text": "На какой срок ищете жильё?",
        "options": ["До 3 месяцев", "3–6 месяцев", "6–12 месяцев", "Долгосрочно (от года)", "Пока не уверен(а)"],
        "label": "Срок аренды", "allow_other": True,
    },
    {
        "key": "smoking", "kind": "single", "emoji": "🚬",
        "text": "Курите ли вы?",
        "options": ["Не курю", "Курю", "Иногда / только на улице"],
        "label": "Курение", "allow_other": True,
    },
    {
        "key": "extra", "kind": "text", "emoji": "📝",
        "text": "Есть ли ещё важные пожелания или условия, которые нужно учитывать?\nНапример: мебель, лифт, балкон, парковка, безбарьерный доступ. Если ничего нет, напишите «Нет».",
        "label": "Доп. пожелания",
    },
    {
        "key": "client_name", "kind": "text", "emoji": "🙋",
        "text": "Как к вам обращаться?",
        "label": "Имя",
    },
    {
        "key": "phone", "kind": "contact", "emoji": "📱",
        "text": "Оставьте, пожалуйста, номер телефона для связи.",
        "label": "Телефон",
    },
]

LABELS = {step["key"]: step["label"] for step in STEPS}
OTHER_STATE = len(STEPS)  # отдельное состояние для свободного ввода после кнопки "Другое"

RU_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def get_move_in_options():
    """Возвращает актуальные варианты заезда: 'как можно скорее' + следующий календарный месяц."""
    next_month_num = datetime.now().month % 12  # 1-12 -> следующий месяц, декабрь(12) -> январь(0->используем как индекс)
    next_month_name = RU_MONTHS_GENITIVE[next_month_num]
    return ["Как можно скорее", f"С {next_month_name}", "В течение 1-2 месяцев", "Пока не знаю"]


def resolve_options(step):
    """Возвращает список вариантов для шага, разворачивая динамические плейсхолдеры."""
    if step["options"] == "MOVE_IN_DYNAMIC":
        return get_move_in_options()
    return step["options"]

# Группировка полей для красивой сборки итоговой заявки
HOUSING_KEYS = ["type", "district", "budget", "rooms", "area", "move_in", "term"]
CLIENT_KEYS = ["age", "people", "from_country", "how_long", "citizenship",
               "visa", "kids", "pets", "smoking"]
WORK_KEYS = ["work", "income"]

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================


def build_single_keyboard(step, index) -> InlineKeyboardMarkup:
    options = resolve_options(step)
    max_len = max(len(o) for o in options)
    row_size = 4 if max_len <= 4 else (2 if max_len <= 14 else 1)

    rows, row = [], []
    for i, opt in enumerate(options):
        row.append(InlineKeyboardButton(opt, callback_data=f"single|{index}|{i}"))
        if len(row) == row_size:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if step.get("allow_other"):
        rows.append([InlineKeyboardButton("✏️ Другое", callback_data=f"other|{index}")])

    return InlineKeyboardMarkup(rows)


def build_multi_keyboard(step, index, selected: set) -> InlineKeyboardMarkup:
    options = step["options"]
    rows, row = [], []
    for i, opt in enumerate(options):
        label = ("✅ " if i in selected else "") + opt
        row.append(InlineKeyboardButton(label, callback_data=f"multi|{index}|{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if step.get("allow_other"):
        rows.append([InlineKeyboardButton("✏️ Другой вариант", callback_data=f"multi_other|{index}")])

    rows.append([InlineKeyboardButton("✅ Готово", callback_data=f"multi_done|{index}")])
    return InlineKeyboardMarkup(rows)


def question_text(index: int) -> str:
    step = STEPS[index]
    return f"{step['emoji']} {step['text']}\n\nВопрос {index + 1} из {len(STEPS)}"


async def safe_query_answer(query, text=None, **kwargs):
    """Закрывает индикатор кнопки; краткий сетевой сбой не ломает переход."""
    try:
        await telegram_call_with_retry(
            lambda: query.answer(text, **kwargs),
            "callback answer",
            attempts=2,
        )
    except Exception as error:
        logger.warning(f"Не удалось подтвердить нажатие кнопки: {error}")


async def edit_to_question(query, context, index: int) -> bool:
    """Показывает следующий вопрос редактированием текущего сообщения.

    Это уменьшает число Telegram-запросов и делает анкету визуально плавнее.
    Контактный шаг требует ReplyKeyboardMarkup, поэтому для него остаётся новое
    сообщение через send_question().
    """
    step = STEPS[index]
    if step["kind"] == "contact":
        return False

    context.user_data["_current_index"] = index
    reply_markup = None
    if step["kind"] == "single":
        reply_markup = build_single_keyboard(step, index)
    elif step["kind"] == "multi":
        context.user_data.setdefault(f"_multi_{step['key']}", set())
        reply_markup = build_multi_keyboard(step, index, context.user_data[f"_multi_{step['key']}"])

    await telegram_call_with_retry(
        lambda: query.edit_message_text(question_text(index), reply_markup=reply_markup),
        f"edit_to_question {index}",
    )
    return True


def normalize_text_answer(text, max_length=MAX_TEXT_ANSWER_LENGTH):
    text = " ".join((text or "").split())
    if not text:
        return "—"
    if len(text) > max_length:
        return text[:max_length].rstrip() + "..."
    return text


async def answer_stale_button(query):
    await safe_query_answer(
        query,
        "Это старая кнопка. Продолжите с последнего вопроса или отправьте /start.",
        show_alert=True,
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


def split_long_message(text, limit=3900):
    text = text or ""
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra_len = len(line) + 1
        if current and current_len + extra_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += extra_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


async def send_long_message(bot, chat_id, text, limit=3900, start_index=0, on_sent=None):
    chunks = split_long_message(text, limit=limit)
    messages = []
    for index, chunk in enumerate(chunks):
        if index < start_index:
            continue
        messages.append(await telegram_call_with_retry(
            lambda chunk=chunk: bot.send_message(chat_id=chat_id, text=chunk),
            f"send_long_message chunk {index + 1}/{len(chunks)}",
        ))
        if on_sent is not None:
            on_sent(index + 1)
    return messages


async def send_question(message, context: ContextTypes.DEFAULT_TYPE, index: int):
    step = STEPS[index]
    context.user_data["_current_index"] = index
    text = question_text(index)

    if step["kind"] == "single":
        await telegram_call_with_retry(
            lambda: message.reply_text(text, reply_markup=build_single_keyboard(step, index)),
            f"send_question single {index}",
        )
    elif step["kind"] == "multi":
        context.user_data.setdefault(f"_multi_{step['key']}", set())
        await telegram_call_with_retry(
            lambda: message.reply_text(
                text,
                reply_markup=build_multi_keyboard(step, index, context.user_data[f"_multi_{step['key']}"]),
            ),
            f"send_question multi {index}",
        )
    elif step["kind"] == "text":
        await telegram_call_with_retry(
            lambda: message.reply_text(text, reply_markup=ReplyKeyboardRemove()),
            f"send_question text {index}",
        )
    elif step["kind"] == "contact":
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Поделиться номером", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await telegram_call_with_retry(
            lambda: message.reply_text(
                text + "\n\nМожно нажать кнопку ниже или написать номер вручную.",
                reply_markup=kb,
            ),
            f"send_question contact {index}",
        )


def push_history(context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    """Запоминаем шаг, который клиент только что прошёл, чтобы можно было вернуться назад."""
    history = context.user_data.setdefault("_history", [])
    history.append(index)


async def finish(context: ContextTypes.DEFAULT_TYPE, reply_target, user) -> int:
    data = context.user_data

    if user.username:
        contact_line = f"Telegram: https://t.me/{user.username}"
    else:
        contact_line = f"Telegram: tg://user?id={user.id} (без username, ID: {user.id})"

    lines = [
        "🆕 Новая заявка от клиента",
        "",
        f"🙋 Имя: {data.get('client_name', '—')}",
        f"📱 Телефон: {data.get('phone', '—')}",
        contact_line,
        "",
        "🏠 Жильё",
    ]
    for k in HOUSING_KEYS:
        lines.append(f"{LABELS[k]}: {data.get(k, '—')}")

    lines += ["", "👥 О клиенте"]
    for k in CLIENT_KEYS:
        lines.append(f"{LABELS[k]}: {data.get(k, '—')}")

    lines += ["", "💼 Работа и доход"]
    for k in WORK_KEYS:
        lines.append(f"{LABELS[k]}: {data.get(k, '—')}")

    lines += ["", f"📝 {LABELS['extra']}: {data.get('extra', '—')}"]

    summary = "\n".join(lines)
    delivery_fingerprint = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    if data.get("_delivery_fingerprint") != delivery_fingerprint:
        data["_delivery_fingerprint"] = delivery_fingerprint
        data["_delivery_next_chunk"] = 0
    delivery_start = int(data.get("_delivery_next_chunk", 0) or 0)

    def mark_chunk_sent(next_index):
        # PicklePersistence сохранит прогресс. Если, например, первый кусок уже
        # доставлен, а второй временно упал, повтор последнего ответа продолжит
        # со второго куска и не продублирует первый.
        data["_delivery_next_chunk"] = next_index

    try:
        await send_long_message(
            context.bot,
            REQUESTS_CHAT_ID,
            summary,
            start_index=delivery_start,
            on_sent=mark_chunk_sent,
        )
    except Exception as e:
        logger.error(f"Не удалось отправить заявку в чат {REQUESTS_CHAT_ID}: {e}")
        try:
            await telegram_call_with_retry(
                lambda: reply_target.reply_text(
                    "⚠️ Анкета заполнена, но временно не отправилась. Ваши ответы сохранены.\n\n"
                    "Пожалуйста, отправьте последний ответ ещё раз через несколько секунд.",
                    reply_markup=ReplyKeyboardRemove(),
                ),
                "finish failure notice",
            )
        except Exception as notice_error:
            logger.error(f"Не удалось уведомить клиента о сбое отправки: {notice_error}")
        # Не стираем заполненную анкету при сетевом сбое. Возвращаемся на
        # последний шаг, чтобы повторная отправка ответа снова вызвала finish().
        return len(STEPS) - 1

    try:
        await telegram_call_with_retry(
            lambda: reply_target.reply_text(
                "✅ Спасибо! Ваша заявка принята.\n\n"
                "Если нам понадобятся уточнения, мы свяжемся с вами. "
                "Когда найдём подходящие варианты, сразу отправим их вам.\n\n"
                "Чтобы оставить новую заявку с другими пожеланиями, отправьте /start.",
                reply_markup=ReplyKeyboardRemove(),
            ),
            "finish success notice",
        )
    except Exception as e:
        # Заявка уже доставлена: не повторяем её и не создаём дубль только из-за
        # того, что подтверждение клиенту временно не отправилось.
        logger.error(f"Заявка доставлена, но подтверждение клиенту не отправлено: {e}")

    context.user_data.clear()
    return ConversationHandler.END


# ==================== ХЕНДЛЕРЫ ====================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Здравствуйте!\n"
        "Ответьте, пожалуйста, на несколько вопросов — так мы сможем лучше понять "
        "ваш запрос и подобрать подходящее жильё.\n\n"
        "В вопросах с кнопками выберите нужный вариант. Если ничего не подходит, "
        "нажмите «Другое».\n"
        "Ошиблись или хотите исправить ответ — отправьте /back.\n"
        "Чтобы остановить заполнение, отправьте /cancel. Вернуться к заявке можно будет позже.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_question(update.message, context, 0)
    return 0


def make_single_handler(step, index, next_index):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        data = query.data

        parts = data.split("|")
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != index:
            await answer_stale_button(query)
            return index

        if parts[0] == "other":
            await safe_query_answer(query)
            context.user_data["_other_field"] = step["key"]
            context.user_data["_other_next"] = next_index
            context.user_data["_other_index"] = index
            context.user_data["_current_index"] = OTHER_STATE
            await telegram_call_with_retry(
                lambda: query.edit_message_text(
                    f"{step['emoji']} {step['text']}\n\n✏️ Напишите свой вариант:"
                ),
                f"single other {index}",
            )
            return OTHER_STATE

        if len(parts) != 3 or not parts[2].isdigit():
            await answer_stale_button(query)
            return index

        options = resolve_options(step)
        idx = int(parts[2])
        if idx < 0 or idx >= len(options):
            await answer_stale_button(query)
            return index

        await safe_query_answer(query)
        value = options[idx]
        context.user_data[step["key"]] = value
        push_history(context, index)

        if next_index is None:
            await telegram_call_with_retry(
                lambda: query.edit_message_text(f"{step['emoji']} {step['text']}\n\n✅ {value}"),
                f"single final answer {index}",
            )
            return await finish(context, query.message, update.effective_user)

        if await edit_to_question(query, context, next_index):
            return next_index

        # Контактный шаг требует отдельной reply-клавиатуры. Перед ним убираем
        # старые inline-кнопки и фиксируем выбранный ответ.
        await telegram_call_with_retry(
            lambda: query.edit_message_text(f"{step['emoji']} {step['text']}\n\n✅ {value}"),
            f"single answer before contact {index}",
        )
        await send_question(query.message, context, next_index)
        return next_index

    return handler


def make_multi_handler(step, index, next_index):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        data = query.data
        parts = data.split("|")
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != index:
            await answer_stale_button(query)
            return index

        storage_key = f"_multi_{step['key']}"
        selected = context.user_data.setdefault(storage_key, set())

        if parts[0] == "multi_other":
            await safe_query_answer(query)
            context.user_data["_other_field"] = step["key"]
            context.user_data["_other_next"] = next_index
            context.user_data["_other_index"] = index
            context.user_data["_other_mode"] = "multi"
            context.user_data["_other_selected"] = ", ".join(step["options"][i] for i in sorted(selected))
            context.user_data["_current_index"] = OTHER_STATE
            await telegram_call_with_retry(
                lambda: query.edit_message_text(
                    f"{step['emoji']} {step['text']}\n\n✏️ Напишите свой вариант:"
                ),
                f"multi other {index}",
            )
            return OTHER_STATE

        if parts[0] == "multi_done":
            await safe_query_answer(query)
            if selected:
                chosen = [step["options"][i] for i in sorted(selected)]
                value = ", ".join(chosen)
            else:
                value = "Не важно"
            context.user_data[step["key"]] = value
            context.user_data.pop(storage_key, None)

            push_history(context, index)

            if next_index is None:
                await telegram_call_with_retry(
                    lambda: query.edit_message_text(
                        f"{step['emoji']} {step['text']}\n\n✅ Выбрано: {value}"
                    ),
                    f"multi final answer {index}",
                )
                return await finish(context, query.message, update.effective_user)

            if await edit_to_question(query, context, next_index):
                return next_index

            await telegram_call_with_retry(
                lambda: query.edit_message_text(
                    f"{step['emoji']} {step['text']}\n\n✅ Выбрано: {value}"
                ),
                f"multi answer before contact {index}",
            )
            await send_question(query.message, context, next_index)
            return next_index

        if len(parts) != 3 or not parts[2].isdigit():
            await answer_stale_button(query)
            return index

        idx = int(parts[2])
        if idx < 0 or idx >= len(step["options"]):
            await answer_stale_button(query)
            return index

        not_important_index = step["options"].index("Не важно") if "Не важно" in step["options"] else None
        if idx in selected:
            selected.discard(idx)
        else:
            if not_important_index is not None:
                if idx == not_important_index:
                    selected.clear()
                else:
                    selected.discard(not_important_index)
            selected.add(idx)
        await safe_query_answer(query)
        await telegram_call_with_retry(
            lambda: query.edit_message_reply_markup(reply_markup=build_multi_keyboard(step, index, selected)),
            f"multi toggle {index}",
        )
        return index

    return handler


def make_text_handler(step, index, next_index):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data[step["key"]] = normalize_text_answer(update.message.text)
        push_history(context, index)
        if next_index is None:
            return await finish(context, update.message, update.effective_user)
        await send_question(update.message, context, next_index)
        return next_index

    return handler


def make_contact_handlers(step):
    async def from_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data[step["key"]] = normalize_text_answer(update.message.contact.phone_number, max_length=80)
        return await finish(context, update.message, update.effective_user)

    async def from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data[step["key"]] = normalize_text_answer(update.message.text, max_length=80)
        return await finish(context, update.message, update.effective_user)

    return from_contact, from_text


async def other_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.pop("_other_field", None)
    next_index = context.user_data.pop("_other_next", None)
    origin_index = context.user_data.pop("_other_index", None)
    other_mode = context.user_data.pop("_other_mode", None)
    selected_value = context.user_data.pop("_other_selected", "")
    custom_value = normalize_text_answer(update.message.text)
    if field:
        if other_mode == "multi" and selected_value:
            context.user_data[field] = f"{selected_value}, {custom_value}"
        else:
            context.user_data[field] = custom_value

    if origin_index is not None:
        origin_step = STEPS[origin_index]
        context.user_data.pop(f"_multi_{origin_step['key']}", None)
        push_history(context, origin_index)

    if next_index is None:
        return await finish(context, update.message, update.effective_user)

    await send_question(update.message, context, next_index)
    return next_index


async def back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Отменяем незавершённый ввод "Другое", если он был в процессе
    context.user_data.pop("_other_field", None)
    context.user_data.pop("_other_next", None)
    context.user_data.pop("_other_index", None)
    context.user_data.pop("_other_mode", None)
    context.user_data.pop("_other_selected", None)

    history = context.user_data.get("_history", [])

    if not history:
        await update.message.reply_text("Это первый вопрос анкеты — возвращаться некуда.")
        await send_question(update.message, context, 0)
        return 0

    prev_index = history.pop()
    step = STEPS[prev_index]
    context.user_data.pop(step["key"], None)

    await send_question(update.message, context, prev_index)
    return prev_index


async def stale_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает, если клиент нажал старую кнопку, а бот уже не помнит контекст
    (например, был перезапущен). Без этого кнопка просто ничего не делает."""
    query = update.callback_query
    await safe_query_answer(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        "⚠️ Похоже, эта анкета устарела. Пожалуйста, отправьте /start, чтобы начать заново."
    )


def make_unexpected_input_handler(index, message_text):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.message:
            await update.message.reply_text(message_text)
        return index

    return handler


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Я не знаю такую команду.\n\n"
        "Используйте /back, чтобы вернуться на предыдущий вопрос, "
        "/cancel, чтобы остановить анкету, или /start, чтобы начать заново."
    )
    return context.user_data.get("_current_index", ConversationHandler.END)


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит любые необработанные ошибки, чтобы бот не 'зависал' молча."""
    if is_transient_network_error(context.error):
        logger.warning(
            "Временная сетевая ошибка Telegram; polling восстановится автоматически: "
            f"{type(context.error).__name__}: {context.error}"
        )
        return

    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)

    try:
        error_text = str(context.error)[:300]
        await context.bot.send_message(
            chat_id=REQUESTS_CHAT_ID,
            text=f"⚠️ В боте клиентов произошла ошибка:\n{error_text}"
        )
    except Exception:
        pass

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла техническая ошибка. Пожалуйста, отправьте /start, чтобы начать заново."
            )
    except Exception:
        pass


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Заполнение анкеты прервано. Если захотите начать заново — напишите /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ==================== ЗАПУСК ====================


def main() -> None:
    validate_config()
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    logger.info(f"Файл памяти бота клиентов: {BOT_DATA_PATH}")
    persistence = PicklePersistence(filepath=BOT_DATA_PATH)
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    states = {}
    for index, step in enumerate(STEPS):
        next_index = index + 1 if index + 1 < len(STEPS) else None

        if step["kind"] == "single":
            states[index] = [
                CallbackQueryHandler(
                    make_single_handler(step, index, next_index),
                    pattern=rf"^(single\|{index}\|\d+|other\|{index})$",
                ),
                CallbackQueryHandler(stale_button_handler),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    make_unexpected_input_handler(index, "Пожалуйста, выберите один из вариантов кнопкой ниже.")
                ),
            ]
        elif step["kind"] == "multi":
            states[index] = [
                CallbackQueryHandler(
                    make_multi_handler(step, index, next_index),
                    pattern=rf"^(multi\|{index}\|\d+|multi_done\|{index}|multi_other\|{index})$",
                ),
                CallbackQueryHandler(stale_button_handler),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    make_unexpected_input_handler(index, "Пожалуйста, выберите один или несколько вариантов кнопками ниже.")
                ),
            ]
        elif step["kind"] == "text":
            states[index] = [
                MessageHandler(filters.TEXT & ~filters.COMMAND, make_text_handler(step, index, next_index)),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    make_unexpected_input_handler(index, "Пожалуйста, напишите ответ текстом.")
                ),
            ]
        elif step["kind"] == "contact":
            from_contact, from_text = make_contact_handlers(step)
            states[index] = [
                MessageHandler(filters.CONTACT, from_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, from_text),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    make_unexpected_input_handler(index, "Пожалуйста, поделитесь номером кнопкой ниже или напишите номер текстом.")
                ),
            ]

    states[OTHER_STATE] = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, other_input_handler),
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            make_unexpected_input_handler(OTHER_STATE, "Пожалуйста, напишите свой вариант текстом.")
        ),
    ]

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states=states,
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("back", back),
            CommandHandler("cancel", cancel),
            MessageHandler(filters.COMMAND, unknown_command),
        ],
        persistent=True,
        name="client_survey",
        allow_reentry=True,
    )

    application.add_handler(conv_handler)

    # Ловит нажатия на кнопки от анкет, которые бот уже "забыл" (например, после
    # перезапуска) — без этого такие кнопки просто не реагировали бы ни на что.
    application.add_handler(CallbackQueryHandler(stale_button_handler))

    application.add_error_handler(global_error_handler)

    logger.info("Бот клиентов запущен")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    if platform.system() == "Windows" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
