import os
import json
import logging
import threading
from collections import Counter
from datetime import time as dtime, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

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
FAMILY_HEAD_VOTE_WEIGHT = int(os.environ.get("FAMILY_HEAD_VOTE_WEIGHT", "2"))
COMPILE_HOUR = int(os.environ.get("COMPILE_HOUR", "21"))
COMPILE_MINUTE = int(os.environ.get("COMPILE_MINUTE", "30"))
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Johannesburg"))

# Weekly shopping list — 0=Monday ... 6=Sunday
WEEKLY_SHOPPING_DAY = int(os.environ.get("WEEKLY_SHOPPING_DAY", "6"))
WEEKLY_SHOPPING_HOUR = int(os.environ.get("WEEKLY_SHOPPING_HOUR", "21"))
WEEKLY_SHOPPING_MINUTE = int(os.environ.get("WEEKLY_SHOPPING_MINUTE", "35"))

# Alert the cook right away if an ingredient has less than this many
# typical servings left, instead of waiting for the weekly list.
LOW_STOCK_SERVINGS = float(os.environ.get("LOW_STOCK_SERVINGS", "8"))

# Extra portions to cook on top of the number of people who voted, as a
# buffer for seconds.
PORTION_BUFFER_EXTRA = int(os.environ.get("PORTION_BUFFER_EXTRA", "1"))

# /whatstobuy reports shortfall for a full family-size batch, not one serving.
WHATSTOBUY_SERVINGS = int(os.environ.get("WHATSTOBUY_SERVINGS", "8"))

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

FAMILY_FILE = DATA_DIR / "family.json"
INVENTORY_FILE = DATA_DIR / "inventory.json"
LAST_SHOPPING_LIST_FILE = DATA_DIR / "last_shopping_list.json"
WEEKLY_SHORTFALL_FILE = DATA_DIR / "weekly_shortfall.json"
DISHES_FILE = BASE_DIR / "dishes.json"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

DISHES = json.loads(DISHES_FILE.read_text())

# "Бытовые" ингредиенты (соль, масло, специи, зелень для украшения), которые
# почти всегда есть под рукой — не учитываются при проверке доступности блюд.
PANTRY_STAPLES = {
    "соль", "чёрный перец", "растительное масло", "оливковое масло",
    "паприка", "зира", "орегано", "розмарин", "чили хлопья",
    "уксус", "красный винный уксус", "горчица", "свежая зелень",
}

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
    "лайм": "lime", "лимон": "lemon",
    "лук репчатый": "onion", "молоко": "milk", "морковь": "carrot",
    "мёд": "honey", "овощной бульон": "vegetable broth", "огурец": "cucumber",
    "огурцы солёные": "pickled cucumbers", "оливковое масло": "olive oil",
    "орегано": "oregano", "паприка": "paprika", "пармезан": "parmesan",
    "петрушка": "parsley", "помидор": "tomato",
    "растительное масло": "vegetable oil", "рис": "rice",
    "рис арборио": "arborio rice", "рис длиннозёрный": "long-grain rice",
    "розмарин": "rosemary", "свежая зелень": "fresh herbs",
    "свёкла": "beetroot", "слабосолёный лосось": "lightly salted salmon",
    "сливки": "cream", "сливочное масло": "butter", "сметана": "sour cream",
    "соль": "salt", "специи для тако": "taco seasoning",
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
    """Average amount_per_serving for each ingredient across every dish that uses it.

    Used to estimate "servings left" for a given stock level, so we can warn
    the cook before something actually runs out.
    """
    totals, counts = {}, {}
    for dish in DISHES.values():
        for ing in dish["ingredients"]:
            totals[ing["name"]] = totals.get(ing["name"], 0) + ing["amount_per_serving"]
            counts[ing["name"]] = counts.get(ing["name"], 0) + 1
    return {name: totals[name] / counts[name] for name in totals}


INGREDIENT_TYPICAL_USAGE = build_ingredient_typical_usage()


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


def compute_unavailable_dishes():
    """Список id блюд, которые нельзя приготовить — не хватает ингредиента
    даже на одну порцию. Мелкие "бытовые" ингредиенты (соль, масло, специи,
    зелень для украшения) не блокируют блюдо — почти всегда есть под рукой."""
    inventory = load_inventory()
    unavailable = []
    for dish_id, dish in DISHES.items():
        for ing in dish["ingredients"]:
            if ing["name"] in PANTRY_STAPLES:
                continue
            have = inventory.get(ing["name"], {}).get("amount", 0)
            if have < ing["amount_per_serving"]:
                unavailable.append(dish_id)
                break
    return unavailable


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
        f"Каждый вечер в {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} буду присылать кнопку с меню, "
        "чтобы выбрать, что хочешь на завтрак и ужин. Можешь нажать её и "
        "сейчас, чтобы попробовать."
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
    # Usage: /addstock мука 500 г
    parts = context.args
    if len(parts) < 3:
        await update.message.reply_text(
            "Формат: /addstock название количество единица\n"
            "Например: /addstock мука 500 г"
        )
        return

    unit = parts[-1]
    try:
        amount = float(parts[-2])
    except ValueError:
        await update.message.reply_text("Количество должно быть числом. Пример: /addstock мука 500 г")
        return
    name = " ".join(parts[:-2])

    inventory = load_inventory()
    if name in inventory:
        inventory[name]["amount"] += amount
        inventory[name]["unit"] = unit
    else:
        inventory[name] = {"amount": amount, "unit": unit}
    save_inventory(inventory)

    await update.message.reply_text(f"Добавлено: {name} — теперь {inventory[name]['amount']} {unit}")


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


async def whatstobuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inventory = load_inventory()
    missing = {}
    for dish in DISHES.values():
        for ing in dish["ingredients"]:
            if ing["name"] in PANTRY_STAPLES:
                continue
            needed_for_batch = ing["amount_per_serving"] * WHATSTOBUY_SERVINGS
            have = inventory.get(ing["name"], {}).get("amount", 0)
            shortfall = needed_for_batch - have
            if shortfall > 0:
                existing = missing.get(ing["name"])
                if not existing or existing["amount"] < shortfall:
                    missing[ing["name"]] = {"amount": round(shortfall, 1), "unit": ing["unit"]}

    if not missing:
        await update.message.reply_text(
            "Everything's in stock — any dish on the menu can be made right now."
        )
        return

    lines = [
        f"- {to_en(name)}: {item['amount']} {unit_to_en(item['unit'])}"
        for name, item in sorted(missing.items())
    ]
    await update.message.reply_text(
        "To unlock more dishes from the menu, buy:\n" + "\n".join(lines)
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Here's what this bot does and the commands you might need:\n\n"
        "Every evening the family votes on tomorrow's menu. Once voting "
        "closes, you'll get a message here with the winning dishes, scaled "
        "to the right number of portions, with full step-by-step recipes.\n\n"
        "Commands:\n"
        "/stock — see what's currently in the kitchen\n"
        "/updatestock — quickly enter what's in the kitchen after a shopping trip\n"
        "/whatstobuy — see what's missing to unlock more dishes from the menu\n"
        "/whoami — show this chat's ID (only needed once, during setup)\n"
        "/help — show this message again"
    )
    await update.message.reply_text(text)


async def receive_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = json.loads(update.effective_message.web_app_data.data)

    if data.get("type") == "stock":
        stock_update = data.get("stock", {})
        if not stock_update:
            await update.message.reply_text("Nothing was filled in — stock unchanged.")
            return
        inventory = load_inventory()
        for name, item in stock_update.items():
            inventory[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_inventory(inventory)
        await update.message.reply_text(
            f"Updated {len(stock_update)} item(s) in stock. Thanks!"
        )
        return

    def valid_pick(pick):
        if not pick or not isinstance(pick, dict):
            return None
        if pick.get("id") not in DISHES:
            return None
        return pick

    breakfast_raw = data.get("breakfast") or {}
    breakfast_pick = {
        course: valid_pick(breakfast_raw.get(course))
        for course in ("main", "dessert")
    }

    dinner_raw = data.get("dinner") or {}
    dinner_pick = {
        course: valid_pick(dinner_raw.get(course))
        for course in ("starter", "main", "side", "dessert")
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


def apply_inventory(needed):
    """Subtract what's in stock from what's needed.

    Returns (shopping_list, updated_inventory) — shopping_list holds only the
    shortfall (what still needs buying), updated_inventory is what's left in
    stock after today's planned cooking uses it up.
    """
    inventory = load_inventory()
    shopping_list = {}

    for name, need in needed.items():
        have = inventory.get(name, {"amount": 0.0, "unit": need["unit"]})
        available = have["amount"]
        shortfall = max(need["amount"] - available, 0.0)
        leftover = max(available - need["amount"], 0.0)

        if shortfall > 0:
            shopping_list[name] = {"amount": round(shortfall, 1), "unit": need["unit"]}

        inventory[name] = {"amount": round(leftover, 1), "unit": need["unit"]}

    return shopping_list, inventory


def plural_portions(n):
    if n % 10 == 1 and n % 100 != 11:
        return "порция"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "порции"
    return "порций"


def _tally_meal(picks_with_names, courses):
    """Общая логика голосования для одного приёма пищи (завтрак или ужин).
    picks_with_names — список (имя, picks, вес_голоса). Вес влияет только на
    определение победителя, но не на количество порций (participants).
    Возвращает {course: {"dish_id", "portions", "voters"}} только для тех
    категорий, где хоть кто-то голосовал."""
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
                voters[c].setdefault(pick["id"], []).append(
                    {"name": name, "egg": pick.get("egg"), "meat": pick.get("meat")}
                )

    winners = {}
    for c in courses:
        if votes[c]:
            dish_id, _ = votes[c].most_common(1)[0]
            winners[c] = {
                "dish_id": dish_id,
                "portions": participants + PORTION_BUFFER_EXTRA,
                "voters": voters[c][dish_id],
            }
    return winners


def compute_daily_menu(responses):
    """Считает голоса и определяет победителей по каждой под-категории
    завтрака и ужина. Возвращает {"breakfast": {...}, "dinner": {...}},
    где каждое значение — результат _tally_meal (может быть пустым словарём,
    если никто не голосовал)."""
    breakfast = _tally_meal(
        [(r["name"], r.get("breakfast"), r.get("vote_weight", 1)) for r in responses.values()],
        ("main", "meat", "dessert"),
    )
    dinner = _tally_meal(
        [(r["name"], r.get("dinner"), r.get("vote_weight", 1)) for r in responses.values()],
        ("starter", "main", "side", "dessert"),
    )
    return {"breakfast": breakfast, "dinner": dinner}


def aggregate_ingredients_from_menu(menu):
    needed = {}
    def add_dish(dish_id, portions):
        for ing in DISHES[dish_id]["ingredients"]:
            key = ing["name"]
            if key not in needed:
                needed[key] = {"amount": 0.0, "unit": ing["unit"]}
            needed[key]["amount"] += ing["amount_per_serving"] * portions

    for item in menu["breakfast"].values():
        add_dish(item["dish_id"], item["portions"])
    for item in menu["dinner"].values():
        add_dish(item["dish_id"], item["portions"])
    return needed


async def check_low_stock_and_alert(context: ContextTypes.DEFAULT_TYPE, inventory):
    """Warn the cook right away if something is down to its last couple of servings."""
    if not COOK_CHAT_ID:
        return

    low_items = []
    for name, info in inventory.items():
        typical = INGREDIENT_TYPICAL_USAGE.get(name)
        if not typical or typical <= 0:
            continue
        servings_left = info["amount"] / typical
        if servings_left < LOW_STOCK_SERVINGS:
            low_items.append(
                f"{to_en(name)} — about {round(servings_left, 1)} servings left "
                f"({info['amount']} {unit_to_en(info['unit'])})"
            )

    if low_items:
        text = "⚠️ Running low, will need to buy soon:\n" + "\n".join(low_items)
        await context.bot.send_message(chat_id=COOK_CHAT_ID, text=text)


async def compile_and_send_to_cook(context: ContextTypes.DEFAULT_TYPE):
    if not COOK_CHAT_ID:
        logger.warning("COOK_CHAT_ID не задан — некому отправлять план")
        return

    responses = load_today_responses()
    if not responses:
        logger.info("Пока никто не проголосовал — пропускаю сборку")
        return

    menu = compute_daily_menu(responses)
    if not menu["breakfast"] and not menu["dinner"]:
        logger.info("Голосов нет — пропускаю сборку")
        return

    def format_voters(voters):
        labels = []
        for v in voters:
            extra = []
            if v.get("egg"):
                extra.append(f"яйцо: {v['egg']}")
            if v.get("meat"):
                extra.append(f"мясо: {v['meat']}")
            labels.append(f"{v['name']} ({', '.join(extra)})" if extra else v["name"])
        return ", ".join(labels)

    def format_dish_block(dish_id, portions, voters):
        dish = DISHES[dish_id]
        scaled = ", ".join(
            f"{ing['name']} {round(ing['amount_per_serving'] * portions, 1)} {ing['unit']}"
            for ing in dish["ingredients"]
        )
        steps = " ".join(f"{i+1}) {s}" for i, s in enumerate(dish["recipe_steps"]))
        return (
            f"{dish['name']} — на {portions} {plural_portions(portions)} "
            f"(голосовали: {format_voters(voters)}). "
            f"Ингредиенты: {scaled}. Приготовление: {steps} Подача: {dish['serving_note']}"
        )

    sections = []
    breakfast_labels = {"main": "Основное", "meat": "Мясное блюдо", "dessert": "Десерт"}
    if menu["breakfast"]:
        lines = ["## Завтрак"]
        for course in ("main", "meat", "dessert"):
            if course in menu["breakfast"]:
                item = menu["breakfast"][course]
                lines.append(f"### {breakfast_labels[course]}")
                lines.append(format_dish_block(item["dish_id"], item["portions"], item["voters"]))
        sections.append("\n\n".join(lines))

    dinner_labels = {"starter": "Стартер", "main": "Основное", "side": "Гарнир", "dessert": "Десерт"}
    if menu["dinner"]:
        lines = ["## Ужин"]
        for course in ("starter", "main", "side", "dessert"):
            if course in menu["dinner"]:
                item = menu["dinner"][course]
                lines.append(f"### {dinner_labels[course]}")
                lines.append(format_dish_block(item["dish_id"], item["portions"], item["voters"]))
        sections.append("\n\n".join(lines))

    notes_lines = [f"{r['name']}: {r['notes']}" for r in responses.values() if r.get("notes")]
    if notes_lines:
        sections.append("Комментарии по людям:\n" + "\n".join(notes_lines))

    plan_summary = "\n\n---\n\n".join(sections)

    needed = aggregate_ingredients_from_menu(menu)
    shopping_list, updated_inventory = apply_inventory(needed)
    save_inventory(updated_inventory)
    await check_low_stock_and_alert(context, updated_inventory)

    if shopping_list:
        weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
        for name, item in shopping_list.items():
            if name in weekly:
                weekly[name]["amount"] = round(weekly[name]["amount"] + item["amount"], 1)
                weekly[name]["unit"] = item["unit"]
            else:
                weekly[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_json(WEEKLY_SHORTFALL_FILE, weekly)

    prompt = (
        "Write your entire response in clear, simple English — the cook reading "
        "this speaks English, not Russian. Translate every dish name, ingredient "
        "name, and instruction into English; do not leave any Russian words in "
        "your output.\n\n"
        "The cook is not very experienced, so translate the recipe steps in full "
        "detail — every single step, with exact times, temperatures, and doneness "
        "cues. Do not summarize, shorten, or merge steps together. If a step "
        "mentions a specific technique, keep the explanation of how to do it.\n\n"
        "Ты помогаешь повару приготовить еду для семьи на 7 человек по итогам "
        "голосования — только победившие блюда, а не всё, что кто-то предлагал. "
        "Все блюда без глютена — уже учтено в рецептах ниже, не меняй "
        "ингредиенты и их количество (уже пересчитаны на нужное число "
        "порций).\n\n"
        "Повар не очень опытный — сохраняй ВСЕ шаги приготовления дословно и "
        "по порядку, ничего не сокращай и не объединяй в более общие фразы. "
        "Рецепты специально написаны подробно (точное время, температура, "
        "признаки готовности) — это важно сохранить, а не пересказать короче. "
        "Твоя задача — только красиво оформить и сгруппировать, не редактируя "
        "содержание рецептов.\n\n"
        "Представь, что объясняешь это дословно человеку, который вообще никогда "
        "не готовил — как ребёнку, который первый раз стоит у плиты. Не пропускай "
        "ни одной мелочи: сколько именно чего добавить, когда именно помешать, "
        "как понять, что готово. Если в рецепте написано добавить какую-то "
        "специю, зелень или приправу — обязательно укажи именно её, а не общее "
        "«добавьте специи по вкусу». Ни в коем случае не пиши в самом сообщении "
        "ничего о том, что инструкция упрощена или что повар неопытный — просто "
        "дай чёткие, полные шаги, как есть, без всяких пояснений об уровне "
        "сложности.\n\n"
        "Это шведский стол: каждое блюдо готовится одной большой порцией и "
        "подаётся на общей посуде (одна большая тарелка/миска/поднос), а не "
        "раскладывается по отдельным персональным тарелкам — люди накладывают "
        "себе сами. Учти это в формулировке подачи. Если ингредиенты "
        "пересчитаны на много порций (например, больше 6) и блюдо жарится на "
        "сковороде (яичница, омлет, скрэмбл, оладьи) — напиши повару "
        "предупреждение, что может понадобиться сковорода побольше или "
        "готовка в несколько заходов, потому что всё сразу может не "
        "поместиться.\n\n"
        f"Вот меню на завтра по итогам голосования:\n{plan_summary}\n\n"
        "Отформатируй это в чистое сообщение для повара: раздел «Завтрак» с "
        "подразделами Основное/Десерт, раздел «Ужин» с подразделами "
        "Стартер/Основное/Гарнир/Десерт (пропускай подраздел, если в него "
        "никто не голосовал). Рядом с именами в скобках указана прожарка "
        "яйца/мяса — обязательно вынеси её отдельной пометкой внутри блюда, "
        "чтобы повар знал, что часть порций нужно снять с огня раньше или "
        "позже. Если в «Комментариях по людям» есть аллергия или пожелание — "
        "явно укажи в подходящем блюде, что одну порцию нужно отделить и "
        "видоизменить, и как именно."
    )

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    recipe_text = "".join(
        block.text for block in message.content if block.type == "text"
    )
    chunks = [recipe_text[i : i + 3500] for i in range(0, len(recipe_text), 3500)]
    for chunk in chunks:
        await context.bot.send_message(chat_id=COOK_CHAT_ID, text=chunk)


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

    lines = [f"{to_en(name)}: {item['amount']} {unit_to_en(item['unit'])}" for name, item in weekly.items()]
    text = (
        "This week's shopping list:\n" + "\n".join(lines) +
        "\n\nOnce you've bought everything, send the bot /restock to update stock levels."
    )
    await context.bot.send_message(chat_id=COOK_CHAT_ID, text=text)

    # /restock adds back whatever was in the last sent list — now that's this weekly one
    save_json(LAST_SHOPPING_LIST_FILE, weekly)
    save_json(WEEKLY_SHORTFALL_FILE, {})


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/availability":
            body = json.dumps({"unavailable": compute_unavailable_dishes()}).encode("utf-8")
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
    app.add_handler(CommandHandler("updatestock", updatestock_cmd))
    app.add_handler(CommandHandler("restock", restock))
    app.add_handler(CommandHandler("whatstobuy", whatstobuy_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_web_app_data))

    job_queue = app.job_queue
    job_queue.run_daily(
        send_evening_reminders, time=dtime(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TIMEZONE)
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
