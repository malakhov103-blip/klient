# -*- coding: utf-8 -*-
"""
Binio — бот клиентов
Собирает анкету через кнопки (там где уместно) и текст (где нужен свободный ответ),
затем отправляет заявку в чат заявок.
"""

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
logger = logging.getLogger(__name__)


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
        "text": "Вы ищете квартиру или комнату?",
        "options": ["Квартира", "Комната"],
        "label": "Тип жилья",
    },
    {
        "key": "district", "kind": "multi", "emoji": "📍",
        "text": "Какие районы Праги вас интересуют?\nМожно выбрать несколько, затем нажать «Готово».",
        "options": ["Praha 1", "Praha 2", "Praha 3", "Praha 4", "Praha 5",
                     "Praha 6", "Praha 7", "Praha 8", "Praha 9", "Praha 10", "Не важно"],
        "label": "Районы",
    },
    {
        "key": "budget", "kind": "single", "emoji": "💰",
        "text": "Какой у вас максимальный бюджет в месяц, включая коммунальные платежи?",
        "options": ["до 15 000 Kč", "15 000–20 000 Kč", "20 000–25 000 Kč",
                     "25 000–30 000 Kč", "30 000+ Kč"],
        "label": "Бюджет", "allow_other": True,
    },
    {
        "key": "rooms", "kind": "multi", "emoji": "🚪",
        "text": "Какая планировка вам подойдёт?\nМожно выбрать несколько, затем нажать «Готово».\n\n(kk — кухня совмещена с комнатой, +1 — отдельная кухня)",
        "options": ["1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+kk"],
        "label": "Планировка",
    },
    {
        "key": "area", "kind": "single", "emoji": "📐",
        "text": "Какой метраж рассматриваете?",
        "options": ["до 30 м²", "30–50 м²", "50–70 м²", "70–100 м²", "100+ м²"],
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
        "options": ["1", "2", "3", "4", "5+"],
        "label": "Кол-во проживающих",
    },
    {
        "key": "from_country", "kind": "text", "emoji": "🌍",
        "text": "Откуда вы?",
        "label": "Откуда",
    },
    {
        "key": "how_long", "kind": "single", "emoji": "⏳",
        "text": "Как давно вы находитесь в Чехии?",
        "options": ["Ещё не приехал(а)", "Менее 1 месяца", "1–6 месяцев",
                     "6–12 месяцев", "1–5 лет", "Более 5 лет"],
        "label": "Как давно в Чехии",
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
                     "ВНЖ (другое основание)", "Гражданство ЕС", "Беженская виза"],
        "label": "Виза/ВНЖ", "allow_other": True,
    },
    {
        "key": "kids", "kind": "single", "emoji": "👶",
        "text": "Есть ли у вас дети?",
        "options": ["Нет", "Да"],
        "label": "Дети",
    },
    {
        "key": "pets", "kind": "single", "emoji": "🐾",
        "text": "Есть ли у вас животные?",
        "options": ["Нет", "Да"],
        "label": "Животные",
    },
    {
        "key": "work", "kind": "text", "emoji": "💼",
        "text": "Где вы работаете?",
        "label": "Работа",
    },
    {
        "key": "income", "kind": "single", "emoji": "💵",
        "text": "Какой у вас официальный доход?",
        "options": ["до 25 000 Kč", "25 000–35 000 Kč", "35 000–50 000 Kč",
                     "50 000+ Kč", "Не могу подтвердить документально"],
        "label": "Официальный доход",
    },
    {
        "key": "term", "kind": "single", "emoji": "🗓",
        "text": "На какой срок ищете жильё?",
        "options": ["Краткосрочно (до 6 мес.)", "Долгосрочно (от года)", "Пока не уверен(а)"],
        "label": "Срок аренды",
    },
    {
        "key": "smoking", "kind": "single", "emoji": "🚬",
        "text": "Курите ли вы?",
        "options": ["Не курю", "Курю"],
        "label": "Курение",
    },
    {
        "key": "extra", "kind": "text", "emoji": "📝",
        "text": "Есть ли ещё важные пожелания или условия, которые нужно учитывать?",
        "label": "Доп. пожелания",
    },
    {
        "key": "client_name", "kind": "text", "emoji": "🙋",
        "text": "Как вас зовут?",
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
    return ["Как можно скорее", f"С {next_month_name}"]


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

    rows.append([InlineKeyboardButton("✅ Готово", callback_data=f"multi_done|{index}")])
    return InlineKeyboardMarkup(rows)


def normalize_text_answer(text, max_length=MAX_TEXT_ANSWER_LENGTH):
    text = " ".join((text or "").split())
    if not text:
        return "—"
    if len(text) > max_length:
        return text[:max_length].rstrip() + "..."
    return text


async def answer_stale_button(query):
    await query.answer("Это старая кнопка. Продолжите с последнего вопроса или отправьте /start.", show_alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def send_long_message(bot, chat_id, text, limit=3900):
    text = text or ""
    if len(text) <= limit:
        return [await bot.send_message(chat_id=chat_id, text=text)]

    messages = []
    current = []
    current_len = 0
    for line in text.splitlines():
        extra_len = len(line) + 1
        if current and current_len + extra_len > limit:
            messages.append(await bot.send_message(chat_id=chat_id, text="\n".join(current)))
            current = []
            current_len = 0
        current.append(line)
        current_len += extra_len
    if current:
        messages.append(await bot.send_message(chat_id=chat_id, text="\n".join(current)))
    return messages


async def send_question(message, context: ContextTypes.DEFAULT_TYPE, index: int):
    step = STEPS[index]
    context.user_data["_current_index"] = index
    progress = f"Вопрос {index + 1} из {len(STEPS)}"
    text = f"{step['emoji']} {step['text']}\n\n{progress}"

    if step["kind"] == "single":
        await message.reply_text(text, reply_markup=build_single_keyboard(step, index))
    elif step["kind"] == "multi":
        context.user_data[f"_multi_{step['key']}"] = set()
        await message.reply_text(text, reply_markup=build_multi_keyboard(step, index, set()))
    elif step["kind"] == "text":
        await message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    elif step["kind"] == "contact":
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Поделиться номером", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.reply_text(
            text + "\n\nМожно нажать кнопку ниже или написать номер вручную.",
            reply_markup=kb,
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
    try:
        await send_long_message(context.bot, REQUESTS_CHAT_ID, summary)
    except Exception as e:
        logger.error(f"Не удалось отправить заявку в чат {REQUESTS_CHAT_ID}: {e}")
        await reply_target.reply_text(
            "⚠️ Анкета заполнена, но бот не смог отправить заявку в чат.\n\n"
            "Пожалуйста, напишите администратору или попробуйте отправить /start позже.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    await reply_target.reply_text(
        "✅ Спасибо! Ваша заявка принята.\n\n"
        "Мы свяжемся с вами, как только подберём подходящие варианты.\n\n"
        "Если позже захотите оставить ещё одну заявку — например, для другого "
        "запроса или в другой раз — просто отправьте /start, и мы заново пройдём "
        "по вопросам.",
        reply_markup=ReplyKeyboardRemove(),
    )

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
            await query.answer()
            context.user_data["_other_field"] = step["key"]
            context.user_data["_other_next"] = next_index
            context.user_data["_other_index"] = index
            context.user_data["_current_index"] = OTHER_STATE
            await query.edit_message_text(
                f"{step['emoji']} {step['text']}\n\n✏️ Напишите свой вариант:"
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

        await query.answer()
        value = options[idx]
        context.user_data[step["key"]] = value
        await query.edit_message_text(
            f"{step['emoji']} {step['text']}\n\n✅ {value}"
        )

        push_history(context, index)

        if next_index is None:
            return await finish(context, query.message, update.effective_user)

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

        if parts[0] == "multi_done":
            await query.answer()
            if selected:
                chosen = [step["options"][i] for i in sorted(selected)]
                value = ", ".join(chosen)
            else:
                value = "Не важно"
            context.user_data[step["key"]] = value
            context.user_data.pop(storage_key, None)

            await query.edit_message_text(
                f"{step['emoji']} {step['text']}\n\n✅ Выбрано: {value}"
            )

            push_history(context, index)

            if next_index is None:
                return await finish(context, query.message, update.effective_user)

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
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_multi_keyboard(step, index, selected))
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
    if field:
        context.user_data[field] = normalize_text_answer(update.message.text)

    if origin_index is not None:
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
    await query.answer()
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
                    pattern=rf"^(multi\|{index}\|\d+|multi_done\|{index})$",
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
    import asyncio
    if platform.system() == "Windows" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
