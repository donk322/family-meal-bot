import os
import asyncio
import html
import json
import logging
import threading
import zlib
from collections import Counter
from datetime import time as dtime, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from anthropic import Anthropic

from catalog.scale_recipe import load_catalog, render_dish, countdown_lines, scaled_ingredients
from catalog.stock_check import check_dish, subtract_for_dish, to_stock_unit, CONV

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MINI_APP_URL = os.environ["MINI_APP_URL"]
STOCK_APP_URL = os.environ.get("STOCK_APP_URL", "")
COOK_CHAT_ID = int(os.environ.get("COOK_CHAT_ID", "0") or 0)

REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "20"))
REMINDER_MINUTE = int(os.environ.get("REMINDER_MINUTE", "0"))
FAMILY_HEAD_USERNAME = os.environ.get("FAMILY_HEAD_USERNAME", "").lstrip("@").lower()
CREATOR_CHAT_ID = 813950397
FAMILY_HEAD_CHAT_ID = 713575142
VLADA_CHAT_ID = 101205843  # пока только для опознания, персональный трек — позже
FAMILY_HEAD_VOTE_WEIGHT = int(os.environ.get("FAMILY_HEAD_VOTE_WEIGHT", "2"))
COMPILE_HOUR = int(os.environ.get("COMPILE_HOUR", "21"))
COMPILE_MINUTE = int(os.environ.get("COMPILE_MINUTE", "30"))
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Johannesburg"))

NUDGE_HOUR = int(os.environ.get("NUDGE_HOUR", "18"))
NUDGE_MINUTE = int(os.environ.get("NUDGE_MINUTE", "0"))

# Weekly shopping list — 0=Monday ... 6=Sunday
WEEKLY_SHOPPING_DAY = int(os.environ.get("WEEKLY_SHOPPING_DAY", "6"))
WEEKLY_SHOPPING_HOUR = int(os.environ.get("WEEKLY_SHOPPING_HOUR", "21"))
WEEKLY_SHOPPING_MINUTE = int(os.environ.get("WEEKLY_SHOPPING_MINUTE", "35"))

# Alert the cook right away if an ingredient has less than this many
# typical servings left, instead of waiting for the weekly list.
LOW_STOCK_SERVINGS = float(os.environ.get("LOW_STOCK_SERVINGS", "8"))

# Extra portions to cook on top of the number of people who voted, as a
# buffer for seconds.
PORTION_BUFFER_EXTRA = int(os.environ.get("PORTION_BUFFER_EXTRA", "2"))

# How many people usually eat (breakfast without the two kids, who eat earlier).
# Portions = max(this or the /people override, voters of that meal) + PORTION_BUFFER_EXTRA.
BREAKFAST_PEOPLE = int(os.environ.get("BREAKFAST_PEOPLE", "5"))
DINNER_PEOPLE = int(os.environ.get("DINNER_PEOPLE", "7"))
HEADCOUNT_MIN, HEADCOUNT_MAX = 1, 30

# /whatstobuy reports shortfall for a full family-size batch, not one serving.
WHATSTOBUY_SERVINGS = int(os.environ.get("WHATSTOBUY_SERVINGS", "8"))

# The two kids eat breakfast separately and earlier — just the winning
# main dish, at a fixed headcount regardless of how the vote went.
KIDS_BREAKFAST_COUNT = int(os.environ.get("KIDS_BREAKFAST_COUNT", "2"))
KIDS_BREAKFAST_BUFFER = int(os.environ.get("KIDS_BREAKFAST_BUFFER", "1"))

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

FAMILY_FILE = DATA_DIR / "family.json"
INVENTORY_FILE = DATA_DIR / "inventory.json"
LAST_SHOPPING_LIST_FILE = DATA_DIR / "last_shopping_list.json"
WEEKLY_SHORTFALL_FILE = DATA_DIR / "weekly_shortfall.json"
FOOD_PREFERENCES_FILE = DATA_DIR / "food_preferences.json"
SERVED_BY_DATE_FILE = DATA_DIR / "served_by_date.json"  # {"YYYY-MM-DD": [{"name", "meal"}, ...]}
SERVED_KEEP_DAYS = 7
RECENT_DISHES_FILE = DATA_DIR / "recent_dishes.json"
RATED_TODAY_FILE = DATA_DIR / "rated_today.json"  # {"date": "YYYY-MM-DD", "users": ["123", ...]}
HEADCOUNT_FILE = DATA_DIR / "headcount.json"  # {"breakfast": null|int, "dinner": null|int}, null = как обычно
RECENT_DISHES_DAYS = int(os.environ.get("RECENT_DISHES_DAYS", "5"))

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Фиксированное меню: 56 блюд из catalog/dishes_fixed.json (собраны из
# recipes_src/*.txt). ИИ меню не придумывает, количества пересчитывает код.
CATALOG = load_catalog()

# Порядок категорий в голосе, в сообщении повару и в мини-аппе.
MEAL_ORDER = {
    "breakfast": ("main", "extra", "dessert"),
    "dinner": ("soup", "salad", "main", "side", "dessert"),
}
COURSE_LABELS_EN = {
    "breakfast": {"main": "Main", "extra": "Extra", "dessert": "Dessert"},
    "dinner": {"soup": "Soup", "salad": "Salad", "main": "Main", "side": "Side", "dessert": "Dessert"},
}


def in_pool(dish, meal, course):
    """Подходит ли блюдо каталога в категорию (приём пищи, категория) голосования.
    Один десерт может быть и в пуле завтрака, и в пуле ужина."""
    cat = dish.get("category")
    if meal == "breakfast":
        if course == "main":
            return cat == "breakfast_main"
        if course == "extra":
            return cat == "breakfast_extra"
        if course == "dessert":
            return cat == "dessert" and bool(dish.get("breakfast_ok"))
        return False
    return course in MEAL_ORDER["dinner"] and cat == course


def pool(meal, course):
    return [(dish_id, dish) for dish_id, dish in CATALOG.items() if in_pool(dish, meal, course)]


# The cook is English-speaking — translate ingredient names/units wherever
# they're shown to them directly (outside the Claude-generated menu, which
# is translated via the prompt instead).
INGREDIENT_EN = {
    "авокадо": "avocado", "банан": "banana",
    "безглютеновый хлеб": "gluten-free bread",
    "бекон": "bacon", "болгарский перец": "bell pepper", "ветчина": "ham",
    "говядина": "beef",
    "говяжий стейк": "beef steak", "говяжий фарш": "ground beef",
    "горчица": "mustard", "греческий йогурт": "Greek yogurt",
    "гречневая крупа": "buckwheat groats", "зира": "cumin",
    "йогурт натуральный": "plain yogurt", "картофель": "potato",
    "колбаски": "sausages", "красный винный уксус": "red wine vinegar",
    "кукурузные тако-шеллы": "corn taco shells",
    "куриное бедро": "chicken thigh", "куриное филе": "chicken breast",
    "куриный фарш": "Chicken mince",
    "лайм": "lime", "лимон": "lemon",
    "лук репчатый": "onion", "молоко": "milk", "морковь": "carrot",
    "мёд": "honey", "нутовая паста": "Chickpea pasta",
    "овощной бульон": "vegetable broth", "огурец": "cucumber",
    "огурцы солёные": "pickled cucumbers", "оливковое масло": "olive oil",
    "орегано": "oregano", "паприка": "paprika", "пармезан": "parmesan",
    "петрушка": "parsley", "помидор": "tomato",
    "растительное масло": "vegetable oil", "рис": "rice",
    "рис арборио": "arborio rice", "рис длиннозёрный": "long-grain rice",
    "розмарин": "rosemary", "свежая зелень": "fresh herbs",
    "свёкла": "beetroot", "свиной фарш": "Pork mince",
    "слабосолёный лосось": "lightly salted salmon",
    "сливки": "cream", "сливочное масло": "butter", "сметана": "sour cream",
    "соль": "salt", "специи для тако": "taco seasoning",
    "стейк из страусятины": "Ostrich steak",
    "сыр твёрдый": "hard cheese", "томатная паста": "tomato paste",
    "укроп": "dill", "уксус": "vinegar",
    "фасоль в томате": "baked beans (in tomato sauce)",
    "филе белой рыбы": "white fish fillet", "филе лосося": "salmon fillet",
    "цукини": "zucchini", "чеснок": "garlic", "чили хлопья": "chili flakes",
    "чёрный перец": "black pepper", "шампиньоны": "mushrooms",
    "яблоко": "apple", "ягоды": "berries", "яйца": "eggs",
}

UNIT_EN = {"г": "g", "кг": "kg", "мл": "ml", "л": "l", "шт": "pcs"}


def to_en(name):
    return INGREDIENT_EN.get(name, name)


def unit_to_en(unit):
    return UNIT_EN.get(unit, unit)


def build_ingredient_typical_usage():
    """Average amount per portion (amount / base_portions) for each catalog
    stock_key across every dish that uses it, staples skipped, in stock units
    (grams, ml, pieces — see CONV in catalog/stock_check.py).

    Used to estimate "servings left" for a given stock level, so we can warn
    the cook before something actually runs out.
    """
    totals, counts = {}, {}
    for dish in CATALOG.values():
        for key, unit, amount, staple in scaled_ingredients(dish, 1):
            if staple:
                continue
            totals[key] = totals.get(key, 0) + to_stock_unit(key, unit, amount)
            counts[key] = counts.get(key, 0) + 1
    return {key: totals[key] / counts[key] for key in totals}


INGREDIENT_TYPICAL_USAGE = build_ingredient_typical_usage()

# Inventory keeps old products under Russian keys with Russian units and new
# ones under English keys as typed in /addstock; the catalog uses lowercase
# English stock_key in g / ml / pcs.
RU_UNIT = {"г": "g", "мл": "ml", "шт": "pcs", "кг": "kg", "л": "l"}


def inventory_stock_name(key):
    return (to_en(key) if key in INGREDIENT_EN else key).strip().lower()


def inventory_base_factor(item):
    """kg and l are stored as is in inventory, the catalog counts g and ml."""
    unit = RU_UNIT.get(item.get("unit", ""), item.get("unit", ""))
    return 1000.0 if unit in ("kg", "l") else 1.0


def inventory_as_english_stock(inv=None):
    """{catalog stock_key: amount in g/ml/pcs} for check_dish and
    subtract_for_dish, plus {stock_key: first inventory key with that name}."""
    inv = load_inventory() if inv is None else inv
    stock, en_to_inv = {}, {}
    for key, item in inv.items():
        name = inventory_stock_name(key)
        amount = float(item.get("amount", 0)) * inventory_base_factor(item)
        stock[name] = stock.get(name, 0) + amount
        en_to_inv.setdefault(name, key)
    return stock, en_to_inv


def apply_stock_usage(inv, stock_before, stock_after):
    """Write what subtract_for_dish used (stock_before - stock_after, in g/ml/pcs)
    back into the inventory. A name can sit under several inventory keys
    (Russian and English) — take from them in order."""
    inv = {k: dict(v) for k, v in inv.items()}
    for name, before in stock_before.items():
        used = before - stock_after.get(name, before)
        if used <= 1e-9:
            continue
        for key, item in inv.items():
            if used <= 1e-9:
                break
            if inventory_stock_name(key) != name:
                continue
            factor = inventory_base_factor(item)
            have = float(item.get("amount", 0)) * factor
            take = min(have, used)
            item["amount"] = round((have - take) / factor, 2)
            used -= take
    return inv


def stock_unit_for(key):
    """Unit the catalog counts this stock_key in (after CONV), for lists and forms."""
    units = Counter()
    for dish in CATALOG.values():
        for k, unit, _, staple in scaled_ingredients(dish, 1):
            if k == key and not staple:
                units[CONV.get(k, {}).get(unit, (unit,))[0]] += 1
    return units.most_common(1)[0][0] if units else "g"

# ---------- storage helpers ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_family():
    return load_json(FAMILY_FILE, {})


def save_family(family):
    save_json(FAMILY_FILE, family)


def load_inventory():
    return load_json(INVENTORY_FILE, {})


def save_inventory(inventory):
    save_json(INVENTORY_FILE, inventory)


DEFAULT_PEOPLE = {"breakfast": BREAKFAST_PEOPLE, "dinner": DINNER_PEOPLE}
HEADCOUNT_EMPTY = {"breakfast": None, "dinner": None, "days": 0}


def load_headcount():
    """/people state: {"breakfast": int|None, "dinner": int|None, "days": int}.
    None = as usual. days = how many more compiles the override applies to."""
    data = load_json(HEADCOUNT_FILE, {})
    hc = dict(HEADCOUNT_EMPTY)
    for meal in ("breakfast", "dinner"):
        v = data.get(meal)
        if isinstance(v, int) and not isinstance(v, bool) and HEADCOUNT_MIN <= v <= HEADCOUNT_MAX:
            hc[meal] = v
    if hc["breakfast"] is not None or hc["dinner"] is not None:
        days = data.get("days")
        hc["days"] = days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else 1
    return hc


def save_headcount(hc):
    save_json(HEADCOUNT_FILE, hc)


def headcount_active(hc):
    return hc["breakfast"] is not None or hc["dinner"] is not None


def planned_people(meal, hc):
    """(people planned for the meal, whether it is a /people override)."""
    v = hc.get(meal)
    return (v, True) if v is not None else (DEFAULT_PEOPLE[meal], False)


def meal_people(meal, voters, hc):
    planned, guests = planned_people(meal, hc)
    return max(planned, voters), guests


def consume_headcount_day(hc):
    """After a successful compile: one day of the override is used up;
    when no days are left, both meals go back to as usual."""
    if not headcount_active(hc):
        return dict(HEADCOUNT_EMPTY)
    hc = dict(hc, days=hc["days"] - 1)
    return hc if hc["days"] > 0 else dict(HEADCOUNT_EMPTY)


def days_ru(n):
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def headcount_summary(hc):
    """«Завтрак 6, ужин 9, ещё 3 дня» or «…, как обычно»."""
    b, _ = planned_people("breakfast", hc)
    d, _ = planned_people("dinner", hc)
    tail = f"ещё {hc['days']} {days_ru(hc['days'])}" if headcount_active(hc) else "как обычно"
    return f"Завтрак {b}, ужин {d}, {tail}"


def compute_unavailable_dishes():
    """Список id блюд, на которые не хватает запасов на базовое число порций
    блюда (base_portions). Продукта нет в запасах вообще — блюдо тоже
    недоступно. Staple (соль, перец, масло, сухие специи, вода) не проверяются."""
    stock, _ = inventory_as_english_stock()
    return [dish_id for dish_id, dish in CATALOG.items()
            if not check_dish(dish, dish["base_portions"], stock)[0]]


def today_responses_file():
    return DATA_DIR / f"responses_{datetime.now(TIMEZONE).date()}.json"


def load_today_responses():
    return load_json(today_responses_file(), {})


def save_today_responses(responses):
    save_json(today_responses_file(), responses)


def menu_keyboard():
    return ReplyKeyboardMarkup.from_button(
        KeyboardButton(text="🍽 Выбрать меню на завтра", web_app=WebAppInfo(url=MINI_APP_URL))
    )


def stock_keyboard():
    return ReplyKeyboardMarkup.from_button(
        KeyboardButton(text="📦 Update Stock", web_app=WebAppInfo(url=STOCK_APP_URL))
    )


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    family = load_family()
    family[str(user.id)] = {"name": user.first_name}
    save_family(family)

    greeting = (
        f"Привет, {user.first_name}! Записал тебя в список.\n\n"
        f"Каждый вечер в {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} буду присылать кнопку голосования. "
        "Меню фиксированное: блюда из нашего списка, выбираете по одному в "
        "каждой категории (завтрак: Основное блюдо, Дополнение, Десерт; "
        "ужин: Суп, Салат, Основное блюдо, Гарнир, Десерт). Обязательно "
        "только основное блюдо, остальное по желанию. Блюда, для которых "
        "сейчас нет продуктов, показаны серыми.\n\n"
        f"Если кто-то забудет проголосовать — в {NUDGE_HOUR:02d}:{NUDGE_MINUTE:02d} придёт ещё одно "
        "личное напоминание, но только тем, кто ещё не проголосовал.\n\n"
        "После еды можно написать боту /rate — придут кнопки по каждому "
        "сегодняшнему блюду: понравилось или нет, и хватило ли порции. Так "
        "видно, какие блюда семье нравятся.\n\n"
        "Можешь нажать кнопку голосования и сейчас, чтобы попробовать."
    )

    is_family_head = (
        FAMILY_HEAD_USERNAME
        and (user.username or "").lower() == FAMILY_HEAD_USERNAME
    )
    if is_family_head:
        greeting += (
            "\n\n👑 Как глава семьи, твой голос считается за двоих при "
            "определении победившего блюда в каждой категории."
        )

    await update.message.reply_text(greeting, reply_markup=menu_keyboard())


async def check_admin(update: Update, command: str) -> bool:
    """Доступ к ручным командам: создатель, глава семьи по id или по username.
    Отвечает отказом сам, если доступа нет."""
    user = update.effective_user
    allowed = is_admin(user)
    logger.info(f"/{command} от id={user.id} username={user.username!r}, доступ={'да' if allowed else 'нет'}")
    if not allowed:
        await update.message.reply_text(ADMIN_ONLY_TEXT)
    return allowed


ADMIN_ONLY_TEXT = "Эта команда доступна только главе семьи."


def is_admin(user):
    """Creator or family head, by id or by username."""
    return bool(
        user.id in (CREATOR_CHAT_ID, FAMILY_HEAD_CHAT_ID)
        or (FAMILY_HEAD_USERNAME
            and (user.username or "").lstrip("@").lower() == FAMILY_HEAD_USERNAME)
    )


def people_text(hc, done=False):
    b, _ = planned_people("breakfast", hc)
    d, _ = planned_people("dinner", hc)
    status = (f"Гости: ещё {hc['days']} {days_ru(hc['days'])}" if headcount_active(hc)
              else "Как обычно")
    head = "Готово." if done else "Сколько человек завтра?"
    return f"{head}\nЗавтрак: {b} · Ужин: {d}\n{status}"


def people_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Завтрак −", callback_data="hc|b|-"),
         InlineKeyboardButton("Завтрак +", callback_data="hc|b|+")],
        [InlineKeyboardButton("Ужин −", callback_data="hc|d|-"),
         InlineKeyboardButton("Ужин +", callback_data="hc|d|+")],
        [InlineKeyboardButton("Сбросить", callback_data="hc|reset"),
         InlineKeyboardButton("Готово", callback_data="hc|done")],
    ])


PEOPLE_USAGE = (
    "Как пользоваться:\n"
    "/people — кнопки\n"
    "/people 6 9 — завтра на завтрак 6, на ужин 9\n"
    "/people 6 9 3 — то же на 3 дня\n"
    "/people reset — как обычно"
)


async def people_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """How many people eat tomorrow (guests): buttons, or /people B D [days], or reset."""
    if not await check_admin(update, "people"):
        return
    args = context.args or []
    if not args:
        hc = load_headcount()
        await update.message.reply_text(people_text(hc), reply_markup=people_keyboard())
        return
    if len(args) == 1 and args[0].lower() == "reset":
        hc = dict(HEADCOUNT_EMPTY)
        save_headcount(hc)
        await update.message.reply_text(f"Сброшено. {headcount_summary(hc)}")
        return
    if len(args) in (2, 3) and all(a.isdigit() for a in args):
        b, d = int(args[0]), int(args[1])
        days = int(args[2]) if len(args) == 3 else 1
        if all(HEADCOUNT_MIN <= x <= HEADCOUNT_MAX for x in (b, d)) and 1 <= days <= HEADCOUNT_MAX:
            hc = {"breakfast": b, "dinner": d, "days": days}
            save_headcount(hc)
            await update.message.reply_text(headcount_summary(hc))
            return
        await update.message.reply_text(
            f"Число человек — от {HEADCOUNT_MIN} до {HEADCOUNT_MAX}, дней — от 1 до {HEADCOUNT_MAX}.\n"
            f"Сейчас: {headcount_summary(load_headcount())}")
        return
    await update.message.reply_text(f"{PEOPLE_USAGE}\n\nСейчас: {headcount_summary(load_headcount())}")


async def people_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons of /people: hc|b|- hc|b|+ hc|d|- hc|d|+ hc|reset hc|done."""
    query = update.callback_query
    if not is_admin(query.from_user):
        logger.info(f"hc-кнопка от id={query.from_user.id}: доступа нет")
        await query.answer(ADMIN_ONLY_TEXT, show_alert=True)
        return
    await query.answer()
    parts = query.data.split("|")
    hc = load_headcount()
    done = False
    if parts[1:] == ["reset"]:
        hc = dict(HEADCOUNT_EMPTY)
    elif parts[1:] == ["done"]:
        if headcount_active(hc):
            hc["days"] = 1
        done = True
    elif len(parts) == 3 and parts[1] in ("b", "d") and parts[2] in ("-", "+"):
        meal = "breakfast" if parts[1] == "b" else "dinner"
        current, _ = planned_people(meal, hc)
        hc[meal] = min(HEADCOUNT_MAX, max(HEADCOUNT_MIN, current + (1 if parts[2] == "+" else -1)))
        if hc["days"] <= 0:
            hc["days"] = 1
    else:
        return
    save_headcount(hc)
    try:
        await query.edit_message_text(people_text(hc, done=done),
                                      reply_markup=None if done else people_keyboard())
    except BadRequest as e:
        # The number did not change (already 1 or 30): Telegram refuses an identical edit.
        if "not modified" not in str(e).lower():
            raise


async def regenmenu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Раньше перезапускала генерацию меню ИИ. Меню теперь фиксированное."""
    await update.message.reply_text("Меню теперь фиксированное, генерация не нужна.")


async def forcecompile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Собрать и отправить план повару прямо сейчас, не дожидаясь
    COMPILE_HOUR:COMPILE_MINUTE — например, если плановая сборка прошла
    почти без голосов."""
    if not await check_admin(update, "forcecompile"):
        return

    if not COOK_CHAT_ID:
        await update.message.reply_text("COOK_CHAT_ID не задан — некому отправлять план.")
        return
    responses = load_today_responses()
    if not responses:
        await update.message.reply_text("Сегодня ещё никто не проголосовал — собирать нечего.")
        return

    await update.message.reply_text(f"Собираю план по {len(responses)} голосам и отправляю повару…")
    await compile_and_send_to_cook(context)
    await update.message.reply_text("Сборка завершена — подробности в логах, если повару ничего не пришло.")


async def previewcook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the caller the cook's messages for the current votes, built by the
    same code as compile_and_send_to_cook. Nothing is written: no stock
    subtraction, no served_by_date, no history, nothing goes to the cook."""
    if not await check_admin(update, "previewcook"):
        return
    responses = load_today_responses()
    if not responses:
        await update.message.reply_text("Пока никто не голосовал")
        return
    menu, dropped = drop_unknown_dishes(compute_daily_menu(responses, compile_headcount()))
    if not menu["breakfast"] and not menu["dinner"]:
        await update.message.reply_text("Пока никто не голосовал")
        return
    messages = cook_messages(menu)
    note = f"Предпросмотр для повара: {len(messages)} сообщ. Повару ничего не отправлено, запасы не списаны."
    if dropped:
        note += "\nПропущены блюда, которых нет в каталоге: " + ", ".join(dropped)
    await update.message.reply_text(note)
    for m in messages:
        await update.message.reply_text(m)
        await asyncio.sleep(0.5)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id этого чата: {update.effective_chat.id}")


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inventory = load_inventory()
    if not inventory:
        await update.message.reply_text("Stock is currently empty — nothing recorded yet.")
        return
    lines = [f"{to_en(name)}: {info['amount']} {unit_to_en(info['unit'])}" for name, info in sorted(inventory.items())]
    await update.message.reply_text("Current stock:\n" + "\n".join(lines))


async def addstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.message.text or ""
    # Убираем саму команду "/addstock" из первой строки, если она там есть
    lines = [full_text.split("\n", 1)[0].replace("/addstock", "", 1).strip()] \
            + full_text.split("\n")[1:]
    # Каждая последующая строка может начинаться с "/addstock" (если
    # человек скопировал несколько готовых команд) — убираем это тоже
    lines = [ln.replace("/addstock", "", 1).strip() if ln.strip().startswith("/addstock") else ln.strip()
             for ln in lines]
    lines = [ln for ln in lines if ln]  # убираем пустые строки

    if not lines:
        await update.message.reply_text(
            "Usage: /addstock <product> <amount> <unit>\n"
            "Example: /addstock tomato 200 g\n"
            "You can also paste several lines at once, one product per line."
        )
        return

    results = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            results.append(f"Skipped (couldn't read): {line}")
            continue
        try:
            amount = float(parts[-2])
        except ValueError:
            results.append(f"Skipped (couldn't read amount): {line}")
            continue

        unit_en_raw = parts[-1].lower()
        name_en = " ".join(parts[:-2]).strip()

        EN_TO_RU_UNIT = {"g": "г", "kg": "кг", "ml": "мл", "l": "л", "pcs": "шт", "pc": "шт"}
        unit_ru = EN_TO_RU_UNIT.get(unit_en_raw, unit_en_raw)

        ru_key = None
        for ru_name, en_name in INGREDIENT_EN.items():
            if en_name.lower() == name_en.lower():
                ru_key = ru_name
                break

        inventory = load_inventory()
        key = ru_key if ru_key else name_en
        store_unit = unit_ru if ru_key else unit_en_raw
        existing = inventory.get(key, {"amount": 0, "unit": store_unit})
        existing["amount"] = round(existing.get("amount", 0) + amount, 2)
        existing["unit"] = store_unit
        inventory[key] = existing
        save_inventory(inventory)

        results.append(f"Added {amount} {unit_en_raw} of {name_en}. Total now: {existing['amount']} {unit_en_raw}.")

    await update.message.reply_text("\n".join(results))


async def removestock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip()
    inventory = load_inventory()
    # Ищем без учёта регистра
    match = next((k for k in inventory if k.lower() == name.lower()), None)
    if not match:
        await update.message.reply_text(f"Couldn't find '{name}' in stock.")
        return
    del inventory[match]
    save_inventory(inventory)
    await update.message.reply_text(f"Removed '{match}' from stock.")


async def updatestock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STOCK_APP_URL:
        await update.message.reply_text(
            "STOCK_APP_URL is not set — configure that environment variable first."
        )
        return
    await update.message.reply_text(
        "Tap the button and enter what's currently in the kitchen — only fill in "
        "what you actually counted, everything else stays as it was.",
        reply_markup=stock_keyboard(),
    )


async def restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shopping_list = load_json(LAST_SHOPPING_LIST_FILE, {})
    if not shopping_list:
        await update.message.reply_text("Последний список покупок пуст или его ещё не было.")
        return

    inventory = load_inventory()
    for name, item in shopping_list.items():
        if name in inventory:
            inventory[name]["amount"] += item["amount"]
            inventory[name]["unit"] = item["unit"]
        else:
            inventory[name] = {"amount": item["amount"], "unit": item["unit"]}
    save_inventory(inventory)
    save_json(LAST_SHOPPING_LIST_FILE, {})

    await update.message.reply_text("Готово, запасы пополнены по последнему списку покупок.")


WHATSTOBUY_TOP = 15


def whatstobuy_ranking(stock):
    """Like catalog/report_now.py: for every product that blocks a dish (at its
    base number of portions) -> how many dishes wait for ONLY this product,
    in how many dishes it is short, and the biggest shortfall in stock units.
    Returns (blocked dish count, [(key, only, any, shortfall)] best first)."""
    only, anyc, short = Counter(), Counter(), {}
    blocked = 0
    for dish in CATALOG.values():
        ok, missing = check_dish(dish, dish["base_portions"], stock)
        if ok:
            continue
        blocked += 1
        keys = [m[0] for m in missing]
        for key, need, have in missing:
            anyc[key] += 1
            short[key] = max(short.get(key, 0), need - have)
        if len(keys) == 1:
            only[keys[0]] += 1
    ranking = sorted(anyc, key=lambda k: (-only[k], -anyc[k], k))
    return blocked, [(k, only[k], anyc[k], short[k]) for k in ranking]


def fmt_amount(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


async def whatstobuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock, _ = inventory_as_english_stock()
    blocked, ranking = whatstobuy_ranking(stock)
    if not ranking:
        await update.message.reply_text(
            "Everything's in stock — every dish on the menu can be made right now."
        )
        return

    lines = [
        f"{i}. {key}: buy at least {fmt_amount(amount)} {stock_unit_for(key)}. "
        f"Unlocks {only} dish{'es' if only != 1 else ''} on its own, missing in {n}."
        for i, (key, only, n, amount) in enumerate(ranking[:WHATSTOBUY_TOP], 1)
    ]
    await update.message.reply_text(
        f"{blocked} of {len(CATALOG)} dishes can't be made right now. "
        "Buy these first to unlock the most dishes:\n\n" + "\n".join(lines)
    )


async def shoppinglist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
    if not weekly:
        await update.message.reply_text(
            "This week's shopping list is empty so far — nothing needed to buy yet."
        )
        return

    lines = [
        f"- {to_en(name)}: {item['amount']} {unit_to_en(item['unit'])}"
        for name, item in sorted(weekly.items())
    ]
    await update.message.reply_text(
        "This week's shopping list so far:\n" + "\n".join(lines)
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Here's how this bot works, start to finish:\n\n"
        "The menu is fixed: a set list of our dishes with tested recipes. "
        f"Around {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} the family gets a "
        "message to vote for tomorrow, one dish per category (breakfast: "
        "Main, Extra, Dessert; dinner: Soup, Salad, Main, Side, Dessert). "
        "Only the Main is required. Dishes we don't have the products for "
        "are greyed out. Anyone who hasn't voted yet gets one more reminder "
        f"around {NUDGE_HOUR:02d}:{NUDGE_MINUTE:02d}. Around "
        f"{COMPILE_HOUR:02d}:{COMPILE_MINUTE:02d}, you get this chat's "
        "message: the winning dishes, scaled to the right number of "
        "portions, with full step-by-step recipes — that's your "
        "instructions for the next day.\n\n"
        "A couple of things worth knowing:\n"
        "- Portions already include a small buffer for seconds — you don't "
        "need to add extra yourself.\n"
        "- If two kids in the family have an early separate breakfast, "
        "you'll see a short 'EARLY KIDS' BREAKFAST' section at the very "
        "top of the message — make just that one dish first, before the "
        "rest of the spread.\n"
        "- Serving style is buffet: each dish is one big shared platter or "
        "bowl, not individual plates.\n\n"
        "Commands:\n"
        "/stock — see what's currently in the kitchen\n"
        "/updatestock — open a form to enter what's on hand after a "
        "shopping trip (shows everything currently tracked, including "
        "anything you've added with /addstock)\n"
        "/addstock — add what you just bought, any product, any amount. "
        "Just say how much you bought — the bot adds it to what's already "
        "there, you don't need to know or calculate the old total. You "
        "can paste several lines at once, one product per line.\n"
        "Example: bought 200g of tomatoes? Just send:\n"
        "/addstock tomato 200 g\n"
        "Works for anything, even something not seen before — just type "
        "the name.\n"
        "/removestock <name> — remove a product entirely, in case "
        "something got added by mistake\n"
        "/whatstobuy — see which products to buy first to unlock the most "
        "dishes from the menu, with amounts\n"
        "/shoppinglist — see the current shopping list accumulated so far "
        "this week (a fuller version also arrives automatically once a "
        "week, grouped by store section)\n"
        "/rate — rate today's dishes (👍/👎 for taste, and whether the "
        "portion size was right) — this shows which dishes the family "
        "likes\n"
        "/previewcook — family head only: see tomorrow's cooking messages "
        "for the current votes, sent only to you. Nothing goes to the cook "
        "and stock is not changed\n"
        "/people — family head only: set how many people eat tomorrow "
        f"(usually {BREAKFAST_PEOPLE} at breakfast, {DINNER_PEOPLE} at dinner), "
        "for guests. Buttons, or /people 6 9 (breakfast 6, dinner 9), "
        "/people 6 9 3 (for 3 days), /people reset\n"
        "/whoami — show this chat's ID (only needed once, during setup)\n"
        "/help — show this message again"
    )
    await update.message.reply_text(text)


def served_tag(served):
    """Короткий отпечаток списка блюд — Telegram ограничивает callback_data
    64 байтами, а русское название блюда в UTF-8 легко их превышает, поэтому
    в кнопку кладём отпечаток списка + индекс блюда, а не само название."""
    return f"{zlib.crc32('|'.join(served).encode('utf-8')):08x}"


def normalize_served(entries):
    """Список блюд раньше хранился как список названий, теперь — как список
    {"name", "meal"}. Принимает оба формата, для старых строк meal = None."""
    result = []
    for entry in entries or []:
        if isinstance(entry, str):
            result.append({"name": entry, "meal": None})
        elif isinstance(entry, dict) and entry.get("name"):
            result.append({"name": entry["name"], "meal": entry.get("meal")})
    return result


def load_served(date):
    """Блюда, которые готовятся на дату date ("YYYY-MM-DD")."""
    return normalize_served(load_json(SERVED_BY_DATE_FILE, {}).get(date, []))


def save_served(date, served):
    by_date = load_json(SERVED_BY_DATE_FILE, {})
    by_date[date] = served
    keep = sorted(by_date)[-SERVED_KEEP_DAYS:]
    save_json(SERVED_BY_DATE_FILE, {d: by_date[d] for d in keep})


def served_names_tag(served):
    """Отпечаток считается только по названиям — так он совпадает для старого
    и нового формата списка блюд, и старые кнопки продолжают работать."""
    return served_tag([entry["name"] for entry in served])


def resolve_rated_dish(ref):
    """По "tag|index" из кнопки находит название блюда — в списках блюд по
    датам или в истории последних дней (карточку могли нажать через
    несколько дней). None — если не нашли."""
    parts = ref.split("|")
    if len(parts) != 2 or not parts[1].isdigit():
        return ref  # старая кнопка с названием блюда прямо в callback_data
    tag, idx = parts[0], int(parts[1])
    by_date = load_json(SERVED_BY_DATE_FILE, {})
    candidates = [normalize_served(by_date[d]) for d in sorted(by_date, reverse=True)]
    candidates += [normalize_served(entry.get("dishes", []))
                   for entry in reversed(load_json(RECENT_DISHES_FILE, []))]
    for served in candidates:
        if served and served_names_tag(served) == tag and idx < len(served):
            return served[idx]["name"]
    return None


def record_rating(dish_name, key):
    """key — up / down / portion_little / portion_right / portion_much."""
    prefs = load_json(FOOD_PREFERENCES_FILE, {"dislikes": ["яблоко"], "ratings": {}})
    ratings = prefs.setdefault("ratings", {})
    entry = ratings.setdefault(dish_name, {"up": 0, "down": 0})
    entry[key] = entry.get(key, 0) + 1
    save_json(FOOD_PREFERENCES_FILE, prefs)


def load_rated(date):
    """Кто уже прошёл оценку блюд за date — чтобы повторный /rate не задвоил голоса."""
    data = load_json(RATED_TODAY_FILE, {})
    if data.get("date") != date:
        return set()
    return set(data.get("users", []))


def mark_rated(user_id, date):
    users = load_rated(date)
    users.add(str(user_id))
    save_json(RATED_TODAY_FILE, {"date": date, "users": sorted(users)})


MEAL_HEADERS = {"breakfast": "🍳 Завтрак", "dinner": "🍽 Ужин"}
RATE_INTRO = "Как вам сегодняшняя еда? Оцените блюда, это займёт минуту."
RATE_DONE_TEXT = "✅ <b>Готово!</b> Спасибо, так меню станет вкуснее с каждым днём."
RATE_STALE_TEXT = "Эта карточка устарела. Напишите /rate."
RATE_ALREADY_TEXT = "Вы уже оценили сегодняшние блюда, спасибо! 🙌"


def build_taste_card(served, i, intro=None):
    """Шаг 1 для блюда i: текст карточки (HTML) и кнопки вкуса."""
    dish = served[i]
    counter = f"блюдо {i + 1} из {len(served)}"
    header = MEAL_HEADERS.get(dish["meal"])
    lines = []
    if intro:
        lines += [intro, ""]
    lines += [
        f"<b>{header}</b> · {counter}" if header else counter.capitalize(),
        "",
        f"<b>{html.escape(dish['name'])}</b>",
        "",
        "Как вам?",
    ]
    ref = f"{served_names_tag(served)}|{i}"
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Вкусно", callback_data=f"rt|{ref}|up"),
            InlineKeyboardButton("👎 Не очень", callback_data=f"rt|{ref}|down"),
        ],
        [InlineKeyboardButton("Пропустить →", callback_data=f"rt|{ref}|skip")],
    ])
    return "\n".join(lines), markup


def build_portion_card(served, i):
    """Шаг 2 для блюда i: текст карточки (HTML) и кнопки порции."""
    text = f"<b>{html.escape(served[i]['name'])}</b>\n\nКакой была порция?"
    ref = f"{served_names_tag(served)}|{i}"
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Маловато", callback_data=f"rp|{ref}|little"),
            InlineKeyboardButton("В самый раз", callback_data=f"rp|{ref}|right"),
            InlineKeyboardButton("Многовато", callback_data=f"rp|{ref}|much"),
        ],
        [InlineKeyboardButton("Пропустить →", callback_data=f"rp|{ref}|skip")],
    ])
    return text, markup


async def show_next_dish(query, served, i, date):
    """Перерисовывает карточку на шаг 1 блюда i, а если блюда кончились —
    на финальное «Готово» и запоминает, что человек оценку за date прошёл."""
    if i < len(served):
        text, markup = build_taste_card(served, i)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        mark_rated(query.from_user.id, date)
        await query.edit_message_text(RATE_DONE_TEXT, parse_mode="HTML")


def parse_rating_button(data, served):
    """Разбирает "rt|fp|i|v" / "rp|fp|i|v". Возвращает (i, v) или None,
    если карточка не относится к сегодняшнему списку блюд."""
    parts = data.split("|")
    if len(parts) != 4 or not parts[2].isdigit():
        return None
    _, tag, idx, value = parts
    i = int(idx)
    if not served or served_names_tag(served) != tag or i >= len(served):
        return None
    return i, value


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        served = load_served(today)
        if not served:
            await update.message.reply_text("Сегодня ещё нечего оценивать.")
            return
        if str(update.effective_user.id) in load_rated(today):
            await update.message.reply_text(RATE_ALREADY_TEXT)
            return
        text, markup = build_taste_card(served, 0)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error(f"/rate упал с ошибкой: {e}", exc_info=True)
        await update.message.reply_text("Something went wrong showing today's dishes — the error has been logged.")


async def send_rating_cards(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    served = load_served(today)
    if not served:
        return
    text, markup = build_taste_card(served, 0, intro=RATE_INTRO)
    already_rated = load_rated(today)
    family = load_family()
    for user_id in family:
        if str(user_id) in already_rated:
            continue
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить карточку оценки {user_id}: {e}")


async def rate_taste_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    served = load_served(today)
    parsed = parse_rating_button(query.data, served)
    if parsed is None:
        await query.edit_message_text(RATE_STALE_TEXT)
        return
    if str(query.from_user.id) in load_rated(today):
        await query.edit_message_text(RATE_ALREADY_TEXT)
        return
    i, value = parsed
    if value == "skip":
        await show_next_dish(query, served, i + 1, today)
        return
    if value not in ("up", "down"):
        return
    record_rating(served[i]["name"], value)
    text, markup = build_portion_card(served, i)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def rate_portion_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    served = load_served(today)
    parsed = parse_rating_button(query.data, served)
    if parsed is None:
        await query.edit_message_text(RATE_STALE_TEXT)
        return
    if str(query.from_user.id) in load_rated(today):
        await query.edit_message_text(RATE_ALREADY_TEXT)
        return
    i, value = parsed
    if value in ("little", "right", "much"):
        record_rating(served[i]["name"], f"portion_{value}")
    elif value != "skip":
        return
    await show_next_dish(query, served, i + 1, today)


async def rate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, direction, ref = query.data.split("|", 2)
    dish_name = resolve_rated_dish(ref)
    if dish_name is None:
        await query.edit_message_text("This rating card has expired — use /rate for today's dishes.")
        return
    record_rating(dish_name, direction)
    await query.edit_message_text(f"Thanks! Recorded your rating for {dish_name}.")


async def portion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, size, ref = query.data.split("|", 2)
    dish_name = resolve_rated_dish(ref)
    if dish_name is None:
        await query.edit_message_text("This rating card has expired — use /rate for today's dishes.")
        return
    record_rating(dish_name, f"portion_{size}")  # portion_little / portion_right / portion_much
    await query.edit_message_text(f"Thanks! Recorded portion feedback for {dish_name}.")


async def receive_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = json.loads(update.effective_message.web_app_data.data)

    if data.get("type") == "stock":
        stock_update = data.get("stock", {})
        if not stock_update:
            await update.message.reply_text("Nothing was filled in — stock unchanged.")
            return

        EN_TO_RU_UNIT = {"g": "г", "kg": "кг", "ml": "мл", "l": "л", "pcs": "шт"}
        for key, item in stock_update.items():
            unit_in = item.get("unit", "")
            if key in INGREDIENT_EN:
                # Известный продукт — храним в привычной русской единице
                item["unit"] = EN_TO_RU_UNIT.get(unit_in, unit_in)
            # иначе (новый, свободный ключ) — оставляем единицу как прислали

        inventory = load_inventory()
        for name, item in stock_update.items():
            inventory[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_inventory(inventory)
        await update.message.reply_text(
            f"Updated {len(stock_update)} item(s) in stock. Thanks!"
        )
        return

    def valid_pick(meal, course, pick):
        """Only a catalog dish from this category's pool counts; doneness and
        egg choices are not used in this version."""
        if not pick or not isinstance(pick, dict):
            return None
        dish = CATALOG.get(pick.get("id"))
        if not dish or not in_pool(dish, meal, course):
            return None
        return {"id": pick["id"]}

    breakfast_raw = data.get("breakfast") or {}
    breakfast_pick = {
        course: valid_pick("breakfast", course, breakfast_raw.get(course))
        for course in MEAL_ORDER["breakfast"]
    }

    dinner_raw = data.get("dinner") or {}
    dinner_pick = {
        course: valid_pick("dinner", course, dinner_raw.get(course))
        for course in MEAL_ORDER["dinner"]
    }

    vote_weight = (
        FAMILY_HEAD_VOTE_WEIGHT
        if FAMILY_HEAD_USERNAME and (user.username or "").lower() == FAMILY_HEAD_USERNAME
        else 1
    )

    responses = load_today_responses()
    responses[str(user.id)] = {
        "name": user.first_name,
        "breakfast": breakfast_pick,
        "dinner": dinner_pick,
        "notes": data.get("notes", ""),
        "vote_weight": vote_weight,
    }
    save_today_responses(responses)

    await update.message.reply_text(
        "Записал твой голос, спасибо! Меню на завтра решится по общим голосам вечером."
    )


# ---------- scheduled jobs ----------

async def send_evening_reminders(context: ContextTypes.DEFAULT_TYPE):
    family = load_family()
    for user_id in family:
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text="Что хочешь на завтрак и ужин завтра? Выбирай из меню:",
                reply_markup=menu_keyboard(),
            )
        except Exception as e:
            logger.warning("Не удалось отправить напоминание %s: %s", user_id, e)


async def nudge_non_voters(context: ContextTypes.DEFAULT_TYPE):
    family = load_family()
    responses = load_today_responses()
    voted_ids = set(responses.keys())
    for user_id, info in family.items():
        if user_id in voted_ids:
            continue
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text="⏰ Напоминание: ты ещё не проголосовал(а) за меню на завтра! Осталось немного времени.",
                reply_markup=menu_keyboard(),
            )
        except Exception as e:
            logger.warning(f"Не удалось напомнить {user_id}: {e}")

    # Отдельно от семьи: создателю и главе семьи — одна строка про число едоков.
    text = headcount_notice(load_headcount())
    for chat_id in dict.fromkeys((CREATOR_CHAT_ID, FAMILY_HEAD_CHAT_ID)):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Не удалось отправить число едоков {chat_id}: {e}")


def headcount_notice(hc):
    """«Завтра: завтрак 5 чел., ужин 7 чел. Изменить: /people», with
    «осталось N дн.» while a /people override is on."""
    b, _ = planned_people("breakfast", hc)
    d, _ = planned_people("dinner", hc)
    days = f", осталось {hc['days']} дн." if headcount_active(hc) else ""
    return f"Завтра: завтрак {b} чел., ужин {d} чел.{days} Изменить: /people"


def _tally_meal(picks_with_names, courses, meal, headcount):
    """Общая логика голосования для одного приёма пищи (завтрак или ужин).
    picks_with_names — список (имя, picks, вес_голоса). Вес влияет только на
    определение победителя, но не на количество порций.
    Едоков people = max(число из /people или по умолчанию, проголосовавшие),
    порций = people + PORTION_BUFFER_EXTRA.
    Возвращает {course: {"dish_id", "portions", "people", "guests", "voters"}}
    только для тех категорий, где хоть кто-то голосовал."""
    votes = {c: Counter() for c in courses}
    voters = {c: {} for c in courses}
    participants = 0

    for name, picks, weight in picks_with_names:
        picks = picks or {}
        if any(picks.get(c) for c in courses):
            participants += 1
        for c in courses:
            pick = picks.get(c)
            if pick:
                votes[c][pick["id"]] += weight
                voters[c].setdefault(pick["id"], []).append({"name": name})

    people, guests = meal_people(meal, participants, headcount)
    winners = {}
    for c in courses:
        if votes[c]:
            dish_id, _ = votes[c].most_common(1)[0]
            winners[c] = {
                "dish_id": dish_id,
                "portions": people + PORTION_BUFFER_EXTRA,
                "people": people,
                "guests": guests,
                "voters": voters[c][dish_id],
            }
    return winners


def compute_daily_menu(responses, headcount=None):
    """Считает голоса и определяет победителей по каждой под-категории
    завтрака и ужина. Возвращает {"breakfast": {...}, "dinner": {...}},
    где каждое значение — результат _tally_meal (может быть пустым словарём,
    если никто не голосовал). headcount — состояние /people (по умолчанию из
    data/headcount.json)."""
    headcount = load_headcount() if headcount is None else headcount
    breakfast = _tally_meal(
        [(r["name"], r.get("breakfast"), r.get("vote_weight", 1)) for r in responses.values()],
        MEAL_ORDER["breakfast"], "breakfast", headcount,
    )
    dinner = _tally_meal(
        [(r["name"], r.get("dinner"), r.get("vote_weight", 1)) for r in responses.values()],
        MEAL_ORDER["dinner"], "dinner", headcount,
    )
    return {"breakfast": breakfast, "dinner": dinner}


async def check_low_stock_and_alert(context: ContextTypes.DEFAULT_TYPE, inventory):
    """Warn the cook right away if something is down to its last couple of servings."""
    if not COOK_CHAT_ID:
        return

    low_items = []
    for name, info in inventory.items():
        typical = INGREDIENT_TYPICAL_USAGE.get(inventory_stock_name(name))
        if not typical or typical <= 0:
            continue
        servings_left = float(info.get("amount", 0)) * inventory_base_factor(info) / typical
        if servings_left < LOW_STOCK_SERVINGS:
            low_items.append(
                f"{to_en(name)} — about {round(servings_left, 1)} servings left "
                f"({info['amount']} {unit_to_en(info['unit'])})"
            )

    if low_items:
        text = "⚠️ Running low, will need to buy soon:\n" + "\n".join(low_items)
        await context.bot.send_message(chat_id=COOK_CHAT_ID, text=text)


# Weekday names don't depend on the server's locale this way.
WEEKDAYS_EN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

COOK_INTRO = """📋 TOMORROW'S MENU — {weekday}

HOW TO USE THIS
• Every dish has: what you need, numbered steps, and a ✅ line that tells you when it is ready.
• Do ONE step at a time. Do not skip steps.
• Times are minutes before serving. "T" = the moment the family sits down to eat.
• Serving style is buffet: each dish goes on ONE big platter or bowl in the middle of the table. Only dishes marked "plated separately" go on their own plate.
• Quantities already include a few extra portions for seconds.
• Measures: 1 tablespoon = 15 ml, 1 teaspoon = 5 ml.

🧰 YOU WILL NEED (check before you start)
• 2 large frying pans (so you can cook two dishes at once)
• 1 large pot (4 litres or bigger)
• Baking tray, foil, paper towels, cling film
• Tongs (not a fork), slotted spoon, spatula, grater, sharp knife, cutting board
• Kitchen thermometer and a timer
• Large platters and bowls, serving spoons

🌡 HOW TO USE THE KITCHEN THERMOMETER (steaks, fish, chicken)
1. Wipe the tip clean.
2. Push the tip in sideways, into the middle of the thickest part.
3. Do not touch the pan or a bone.
4. Wait 5 seconds until the number stops changing.
5. Read it, then wipe the tip clean.
No thermometer? Press the steak with your finger: medium feels firm but springy, not soft like raw meat.

🌡 TEMPERATURES TO REMEMBER
• Beef steak: take it off the heat at 55°C. It rises to about 58°C (medium) while resting.
• Fish (salmon, white fish): 60°C.
• Chicken, pork and any mince: 74°C."""

TG_LIMIT = 3900  # Telegram allows 4096 characters per message, keep a margin


def sanitize_plain(text):
    """Сообщение уходит без parse_mode: убираем markdown-символы на всякий случай."""
    lines = []
    for ln in text.splitlines():
        ln = ln.lstrip("#").lstrip() if ln.startswith("#") else ln
        lines.append(ln.replace("**", "").replace("__", "").replace("`", ""))
    return "\n".join(lines)


def split_big(block, limit=TG_LIMIT):
    """Режет слишком длинный кусок по пустым строкам (абзацам), не посреди фразы."""
    parts, units, cur = block.split("\n\n"), [], ""
    for p in parts:
        if cur and len(cur) + 2 + len(p) > limit:
            units.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        units.append(cur)
    return units


def pack_messages(pieces, limit=TG_LIMIT):
    """Складывает целые куски (вступление, секции, блюда) в сообщения до limit
    символов, не разрезая кусок между сообщениями, если он сам влезает."""
    units = []
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if len(p) <= limit:
            units.append(p)
            continue
        # A dish too long for one message (the steak sandwich) is cut between
        # paragraphs; each part starts with "(part i of n)" so the cook knows.
        parts = split_big(p, limit - len("(part 99 of 99)\n"))
        units.extend(f"(part {i} of {len(parts)})\n{part}" if len(parts) > 1 else part
                     for i, part in enumerate(parts, 1))
    msgs, cur = [], ""
    for u in units:
        if cur and len(cur) + 2 + len(u) > limit:
            msgs.append(cur)
            cur = u
        else:
            cur = (cur + "\n\n" + u) if cur else u
    if cur:
        msgs.append(cur)
    return msgs


LINE = "━━━━━━━━━━━━━━━━━━━━"
MEAL_HEADERS_EN = (("breakfast", "☀️ BREAKFAST"), ("dinner", "🌙 DINNER"))


def drop_unknown_dishes(menu):
    """Winners whose dish_id is not in the catalog (for example a vote saved
    before the switch to the fixed menu) are dropped. Returns (menu, dropped)."""
    clean, dropped = {}, []
    for meal, courses in menu.items():
        clean[meal] = {}
        for course, item in courses.items():
            if item["dish_id"] in CATALOG:
                clean[meal][course] = item
            else:
                dropped.append(f"{meal}/{course}: {item['dish_id']}")
    return clean, dropped


def build_cook_pieces(menu, weekday):
    """Deterministic cook message: every element is one whole block (intro,
    section header with countdown, one dish, final check). No AI."""
    pieces = [COOK_INTRO.format(weekday=weekday)]
    if menu["breakfast"].get("main"):
        dish_id = menu["breakfast"]["main"]["dish_id"]
        kids = KIDS_BREAKFAST_COUNT + KIDS_BREAKFAST_BUFFER
        pieces.append(f"{LINE}\n🌅 EARLY KIDS' BREAKFAST\n"
                      f"Make this FIRST. Just this one dish.\n{LINE}")
        pieces.append(render_dish(CATALOG[dish_id], kids))
    for meal, header in MEAL_HEADERS_EN:
        items = [(menu[meal][c]["dish_id"], menu[meal][c]["portions"])
                 for c in MEAL_ORDER[meal] if c in menu[meal]]
        if not items:
            continue
        first = next(menu[meal][c] for c in MEAL_ORDER[meal] if c in menu[meal])
        portions = first["portions"]
        people = first.get("people", portions - PORTION_BUFFER_EXTRA)
        guests = " (guests)" if first.get("guests") else ""
        pieces.append(f"{LINE}\n{header} ({people} people + {portions - people} extra = "
                      f"{portions} portions){guests}\n{LINE}\n\n"
                      "⏰ COUNTDOWN (T = the family sits down)\n"
                      + "\n".join(countdown_lines(items, CATALOG)))
        for dish_id, p in items:
            pieces.append(render_dish(CATALOG[dish_id], p))
        pieces.append("✅ FINAL CHECK (before you call the family)\n"
                      + "\n".join(f"• {CATALOG[d]['title']}" for d, _ in items)
                      + "\n• Serving spoons next to every dish")
    return [sanitize_plain(p) for p in pieces]


def cook_messages(menu):
    """The cook's Telegram messages for tomorrow — shared by
    compile_and_send_to_cook and /previewcook."""
    weekday = WEEKDAYS_EN[(datetime.now(TIMEZONE) + timedelta(days=1)).weekday()]
    return pack_messages(build_cook_pieces(menu, weekday))


def cooked_dishes(menu):
    """Everything the cook makes, in cooking order: the early kids' breakfast
    (the breakfast main for KIDS_BREAKFAST_COUNT + KIDS_BREAKFAST_BUFFER),
    then breakfast, then dinner. [(dish_id, portions)]"""
    items = []
    if menu["breakfast"].get("main"):
        items.append((menu["breakfast"]["main"]["dish_id"], KIDS_BREAKFAST_COUNT + KIDS_BREAKFAST_BUFFER))
    for meal in ("breakfast", "dinner"):
        for course in MEAL_ORDER[meal]:
            item = menu[meal].get(course)
            if item:
                items.append((item["dish_id"], item["portions"]))
    return items


def plan_stock_usage(menu, inventory):
    """Goes dish by dish through everything cooked (kids' breakfast included):
    check_dish against what is left, then subtract_for_dish. Returns
    (new inventory, shortfall) where shortfall is {stock_key: {"amount", "unit"}}
    in the catalog's stock units."""
    stock_before, _ = inventory_as_english_stock(inventory)
    stock = dict(stock_before)
    shortfall = {}
    for dish_id, portions in cooked_dishes(menu):
        dish = CATALOG[dish_id]
        _, missing = check_dish(dish, portions, stock)
        for key, need, have in missing:
            entry = shortfall.setdefault(key, {"amount": 0.0, "unit": stock_unit_for(key)})
            entry["amount"] = round(entry["amount"] + max(need - have, 0), 1)
        stock = subtract_for_dish(dish, portions, stock)
    return apply_stock_usage(inventory, stock_before, stock), shortfall


def compile_snapshot_file():
    return DATA_DIR / f"before_compile_{datetime.now(TIMEZONE).date()}.json"


def compile_headcount(snapshot=None):
    """The /people state a compile right now would use. A repeat compile on the
    same day (/forcecompile) uses the state from before the first one, unless
    /people was changed since — so it never uses up an extra guest day."""
    if snapshot is None:
        path = compile_snapshot_file()
        snapshot = load_json(path, None) if path.exists() else None
    headcount = load_headcount()
    if snapshot and "headcount_before" in snapshot and snapshot.get("headcount_after") == headcount:
        return snapshot["headcount_before"]
    return headcount


async def compile_and_send_to_cook(context: ContextTypes.DEFAULT_TYPE):
    if not COOK_CHAT_ID:
        logger.warning("COOK_CHAT_ID не задан — некому отправлять план")
        return

    responses = load_today_responses()
    if not responses:
        logger.info("Пока никто не проголосовал — пропускаю сборку")
        return

    snapshot_file = compile_snapshot_file()
    snapshot = load_json(snapshot_file, None) if snapshot_file.exists() else None
    headcount = compile_headcount(snapshot)

    menu, dropped = drop_unknown_dishes(compute_daily_menu(responses, headcount))
    if dropped:
        logger.warning(f"Победителей нет в каталоге, пропускаю: {dropped}")
        try:
            await context.bot.send_message(
                chat_id=CREATOR_CHAT_ID,
                text="В сборке повару пропущены блюда, которых нет в каталоге: " + ", ".join(dropped),
            )
        except Exception as e:
            logger.error(f"Не удалось предупредить создателя: {e}")
    if not menu["breakfast"] and not menu["dinner"]:
        logger.info("Голосов нет — пропускаю сборку")
        return

    # Семье — русские названия (title_ru): /rate, карточки оценки, история.
    served = []
    for meal in ("breakfast", "dinner"):
        for course in MEAL_ORDER[meal]:
            if course in menu[meal]:
                served.append({"name": CATALOG[menu[meal][course]["dish_id"]]["title_ru"], "meal": meal})
    # Сборка вечером собирает меню на завтра — /rate и карточки в 18:00
    # завтра будут читать именно этот ключ.
    tomorrow = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
    save_served(tomorrow, served)
    served_names = [entry["name"] for entry in served]

    recent = load_json(RECENT_DISHES_FILE, [])
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    # Повторная сборка в тот же день (/forcecompile) заменяет запись, а не дублирует
    recent = [entry for entry in recent if entry.get("date") != today_str]
    recent.append({"date": today_str, "dishes": served_names})
    recent = recent[-RECENT_DISHES_DAYS:]  # храним только последние N дней
    save_json(RECENT_DISHES_FILE, recent)

    # Повторная сборка за день (/forcecompile) не должна второй раз вычитать
    # запасы и дописывать недостачу — откатываемся к состоянию до первой.
    if snapshot:
        save_inventory(snapshot["inventory"])
        save_json(WEEKLY_SHORTFALL_FILE, snapshot["weekly_shortfall"])
        logger.info("Повторная сборка за сегодня — восстановил запасы до первой сборки")
    else:
        snapshot = {
            "inventory": load_inventory(),
            "weekly_shortfall": load_json(WEEKLY_SHORTFALL_FILE, {}),
        }
        save_json(snapshot_file, snapshot)

    updated_inventory, shortfall = plan_stock_usage(menu, load_inventory())
    save_inventory(updated_inventory)
    await check_low_stock_and_alert(context, updated_inventory)

    if shortfall:
        weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
        for name, item in shortfall.items():
            if name in weekly:
                weekly[name]["amount"] = round(weekly[name]["amount"] + item["amount"], 1)
                weekly[name]["unit"] = item["unit"]
            else:
                weekly[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_json(WEEKLY_SHORTFALL_FILE, weekly)

    messages = cook_messages(menu)
    logger.info(f"Сообщение повару: {len(messages)} частей, длины: {[len(m) for m in messages]}")
    sent = 0
    for m in messages:
        try:
            await context.bot.send_message(chat_id=COOK_CHAT_ID, text=m)
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить часть сообщения повару: {e}")
        await asyncio.sleep(0.5)

    # Гости разовые: после успешной сборки один день /people израсходован.
    if sent:
        after = consume_headcount_day(headcount)
        save_headcount(after)
        snapshot["headcount_before"], snapshot["headcount_after"] = headcount, after
        save_json(snapshot_file, snapshot)
        logger.info(f"Число едоков: было {headcount}, стало {after}")
    else:
        logger.error("Повару не ушло ни одной части — число едоков (/people) не трогаю")


async def send_weekly_shopping_list(context: ContextTypes.DEFAULT_TYPE):
    if not COOK_CHAT_ID:
        logger.warning("COOK_CHAT_ID не задан — некому отправлять список покупок")
        return

    weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
    if not weekly:
        await context.bot.send_message(
            chat_id=COOK_CHAT_ID,
            text="This week's shopping list: everything needed is already in stock, nothing to buy.",
        )
        return

    items_text = "\n".join(
        f"{to_en(name)}: {item['amount']} {unit_to_en(item['unit'])}"
        for name, item in weekly.items()
    )
    group_prompt = (
        "Сгруппируй этот список покупок по типичным отделам "
        "супермаркета (Meat & Fish, Dairy & Eggs, Produce, Pantry, "
        "Other — используй только те заголовки, для которых реально "
        "есть товары). Выведи только сгруппированный список, без "
        "предисловий и заключений, каждый отдел с заголовком, товары "
        "списком под ним:\n\n" + items_text
    )
    try:
        group_message = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": group_prompt}],
        )
        grouped_text = "".join(
            b.text for b in group_message.content if b.type == "text"
        ).strip()
    except Exception as e:
        logger.warning(f"Не удалось сгруппировать список покупок: {e}")
        grouped_text = items_text  # запасной вариант — обычный плоский список

    text = (
        "This week's shopping list:\n\n" + grouped_text +
        "\n\nOnce you've bought everything, send the bot /restock to update stock levels."
    )
    await context.bot.send_message(chat_id=COOK_CHAT_ID, text=text)

    # /restock adds back whatever was in the last sent list — now that's this weekly one
    save_json(LAST_SHOPPING_LIST_FILE, weekly)
    save_json(WEEKLY_SHORTFALL_FILE, {})


def menu_today_payload():
    """Voting options for the mini app: every category's pool from the catalog,
    Russian names for the family. generated_date stays so the mini app's
    freshness check passes as before."""
    result = {"breakfast": {}, "dinner": {},
              "generated_date": datetime.now(TIMEZONE).strftime("%Y-%m-%d")}
    for meal, courses in MEAL_ORDER.items():
        for course in courses:
            result[meal][course] = [
                {"id": dish_id, "name": dish["title_ru"], "description": dish.get("description_ru", "")}
                for dish_id, dish in pool(meal, course)
            ]
    return result


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/availability":
            body = json.dumps({"unavailable": compute_unavailable_dishes()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/menu-today":
            body = json.dumps(menu_today_payload(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/current-stock":
            inventory = load_inventory()
            all_names = set(inventory.keys())
            units_by_name = {k: v.get("unit", "") for k, v in inventory.items()}

            # Catalog products not tracked yet (under a Russian or English key)
            # get an empty row in the stock form, in the unit the catalog counts.
            tracked = {inventory_stock_name(k) for k in inventory}
            for dish in CATALOG.values():
                for key, _, _, staple in scaled_ingredients(dish, 1):
                    if staple or key in tracked or key in all_names:
                        continue
                    all_names.add(key)
                    units_by_name[key] = stock_unit_for(key)

            RU_TO_EN_UNIT = {"г": "g", "мл": "ml", "шт": "pcs", "кг": "kg", "л": "l"}
            items = []
            for key in sorted(all_names):
                display = to_en(key) if key in INGREDIENT_EN else key
                unit_raw = units_by_name.get(key, "")
                unit_display = RU_TO_EN_UNIT.get(unit_raw, unit_raw)
                current_amount = inventory.get(key, {}).get("amount", 0)
                items.append({
                    "key": key,
                    "display": display,
                    "unit": unit_display,
                    "current": current_amount,
                })

            body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # keep Render's request logs quiet


def start_health_server():
    """Bind to Render's assigned PORT so it treats this Web Service as up.

    Render's free tier only stays awake with regular inbound HTTP traffic —
    an external pinger (e.g. UptimeRobot) hitting this endpoint every few
    minutes keeps the process (and the evening job_queue) alive.
    """
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health check server слушает порт %s", port)


def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("addstock", addstock))
    app.add_handler(CommandHandler("removestock", removestock))
    app.add_handler(CommandHandler("updatestock", updatestock_cmd))
    app.add_handler(CommandHandler("restock", restock))
    app.add_handler(CommandHandler("whatstobuy", whatstobuy_cmd))
    app.add_handler(CommandHandler("shoppinglist", shoppinglist_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("regenmenu", regenmenu_cmd))
    app.add_handler(CommandHandler("forcecompile", forcecompile_cmd))
    app.add_handler(CommandHandler("previewcook", previewcook_cmd))
    app.add_handler(CommandHandler("people", people_cmd))
    app.add_handler(CallbackQueryHandler(people_cb, pattern=r"^hc\|"))
    app.add_handler(CallbackQueryHandler(rate_callback, pattern=r"^rate\|"))
    app.add_handler(CallbackQueryHandler(portion_callback, pattern=r"^portion\|"))
    app.add_handler(CallbackQueryHandler(rate_taste_cb, pattern=r"^rt\|"))
    app.add_handler(CallbackQueryHandler(rate_portion_cb, pattern=r"^rp\|"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_web_app_data))

    job_queue = app.job_queue
    job_queue.run_daily(
        send_evening_reminders, time=dtime(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TIMEZONE)
    )
    job_queue.run_daily(
        nudge_non_voters,
        time=dtime(hour=NUDGE_HOUR, minute=NUDGE_MINUTE, tzinfo=TIMEZONE),
    )
    job_queue.run_daily(
        send_rating_cards,
        time=dtime(hour=NUDGE_HOUR, minute=NUDGE_MINUTE, tzinfo=TIMEZONE),
    )
    job_queue.run_daily(
        compile_and_send_to_cook,
        time=dtime(hour=COMPILE_HOUR, minute=COMPILE_MINUTE, tzinfo=TIMEZONE),
    )
    job_queue.run_daily(
        send_weekly_shopping_list,
        time=dtime(hour=WEEKLY_SHOPPING_HOUR, minute=WEEKLY_SHOPPING_MINUTE, tzinfo=TIMEZONE),
        days=(WEEKLY_SHOPPING_DAY,),
    )

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
