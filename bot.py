import os
import re
import asyncio
import difflib
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
from anthropic import Anthropic, APIError

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

GENERATE_HOUR = int(os.environ.get("GENERATE_HOUR", "16"))
GENERATE_MINUTE = int(os.environ.get("GENERATE_MINUTE", "45"))

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
DISHES_FILE = BASE_DIR / "dishes.json"
TODAY_MENU_CACHE_FILE = DATA_DIR / "generated_dishes.json"
FOOD_PREFERENCES_FILE = DATA_DIR / "food_preferences.json"
SERVED_BY_DATE_FILE = DATA_DIR / "served_by_date.json"  # {"YYYY-MM-DD": [{"name", "meal"}, ...]}
SERVED_KEEP_DAYS = 7
RECENT_DISHES_FILE = DATA_DIR / "recent_dishes.json"
RATED_TODAY_FILE = DATA_DIR / "rated_today.json"  # {"date": "YYYY-MM-DD", "users": ["123", ...]}
RECENT_DISHES_DAYS = int(os.environ.get("RECENT_DISHES_DAYS", "5"))

client = Anthropic(api_key=ANTHROPIC_API_KEY)

DISHES = json.loads(DISHES_FILE.read_text())
DISHES_GENERATED_DATE = None  # строка "YYYY-MM-DD" или None, если ни разу не генерировалось в эту сессию
if TODAY_MENU_CACHE_FILE.exists():
    try:
        DISHES = json.loads(TODAY_MENU_CACHE_FILE.read_text())
        logger.info("Подхватил сгенерированное меню из кэша при старте")
        # Если кэш пережил перезапуск и записан сегодня — меню на сегодня уже есть
        cache_date = datetime.fromtimestamp(TODAY_MENU_CACHE_FILE.stat().st_mtime, TIMEZONE).strftime("%Y-%m-%d")
        if cache_date == datetime.now(TIMEZONE).strftime("%Y-%m-%d"):
            DISHES_GENERATED_DATE = cache_date
    except Exception:
        pass

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

FAMILY_FAVORITES_NOTE = (
    "\n\nСемья любит эти блюда и продукты. Старайся выбирать идеи "
    "отсюда, если хватает продуктов в запасах (можно не дословно, а "
    "близко по смыслу): бекон; куриные ножки, бёдра, крылышки, филе; "
    "котлеты из говядины, свинины или курицы; бургеры (без булки или "
    "на безглютеновом хлебе); жареные грибы; жареная цветная капуста; "
    "лапша ширатаки с овощами на сковороде; мясной салат (авокадо "
    "вместо картошки); сырный салат; стейк говяжий; тонко нарезанные "
    "кусочки говядины; свекольный салат с чесноком и майонезом; яйца "
    "варёные с майонезом; борщ; щи; сырный суп; суп с фрикадельками; "
    "фаршированные перцы; грибы с начинкой из крем-сыра и пармезана; "
    "сырники с крем-сыром; блинчики с крем-сыром; творожная "
    "запеканка; жареный халуми. Сложные блюда из этого списка "
    "(фаршированные перцы, грибы с начинкой, суп с фрикадельками) "
    "предлагай только в максимально упрощённом виде или пропусти.\n"
)

BREAKFAST_NOTE = (
    "\n\nПРАВИЛА ДЛЯ ЗАВТРАКА. Завтрак самый важный и самый сытный "
    "приём пищи в этой семье, они едят после тренировки. Поэтому: "
    "1) Все блюда завтрака высокобелковые и плотные: яйца, мясо, "
    "птица, рыба, сыр, субпродукты, в больших порциях. "
    "2) Никаких крахмалистых углеводов на завтрак: картофель, рис, "
    "макароны, лапша, гречка, любые крупы и каши. Хлеб допустим только "
    "как небольшая часть блюда, не как основа. "
    "3) Гарнир на завтрак овощной или белковый (жареные грибы, "
    "цветная капуста, овощи, авокадо, бекон), не крахмальный. "
    "4) Супа на завтрак нет. "
    "5) Десерт на завтрак может быть сладким лакомством, это нормально, "
    "но простым.\n"
)

BANANA_NOTE = (
    "\n\nБананы используй очень редко, а в десертах особенно: только "
    "если больше нечего предложить. Предпочитай ягоды, греческий "
    "йогурт, орехи, мёд.\n"
)

LATIN_TO_CYRILLIC = str.maketrans(
    "aceopxykmtbhACEOPXYKMTBH",
    "асеорхукмтвнАСЕОРХУКМТВН",
)


def fix_mixed_script(name):
    """Заменяет латинские буквы, похожие на русские, на русские — но только
    в словах, где уже есть кириллица («укроp» → «укроп»). Чисто латинские
    названия (повар мог добавить продукт по-английски) не трогает."""
    return " ".join(
        word.translate(LATIN_TO_CYRILLIC)
        if any(is_cyrillic(ch) for ch in word) else word
        for word in name.split(" ")
    )


def is_cyrillic(ch):
    return "а" <= ch.lower() <= "я" or ch.lower() == "ё"


def match_latin_as_wildcard(name, known):
    """Запасной вариант для опечаток вида «укроp», где латинская буква стоит
    не вместо похожей русской, а вместо другой (p вместо п): латинские буквы
    внутри русского слова считаются любой буквой. Возвращает название из
    known, только если подходит ровно одно."""
    if not any(is_cyrillic(ch) for ch in name):
        return None
    candidates = [
        k for k in known
        if len(k) == len(name)
        and all(a == b or (a.isascii() and a.isalpha()) for a, b in zip(name, k))
    ]
    return candidates[0] if len(candidates) == 1 else None


def check_generated_ingredients(generated, inventory):
    """Сверяет названия ингредиентов сгенерированных блюд с запасами.
    Латинские буквы внутри русских слов сначала заменяются на русские,
    почти совпавшее название (опечатка) заменяется на название из запасов;
    блюдо с ингредиентом, которого нет ни в запасах, ни среди бытовых,
    выбрасывается. Несовпадение единицы с запасами только пишется в лог.
    Возвращает (оставшиеся блюда, {id выброшенного блюда: ненайденные названия})."""
    known = list(inventory) + sorted(PANTRY_STAPLES | {"вода"})
    checked, dropped = {}, {}
    for dish_id, dish in generated.items():
        unknown = []
        for ing in dish.get("ingredients", []):
            name = fix_mixed_script(ing["name"])
            match = difflib.get_close_matches(name, known, n=1, cutoff=0.85)
            if not match:
                wildcard = match_latin_as_wildcard(ing["name"], known)
                match = [wildcard] if wildcard else []
            if not match:
                unknown.append(ing["name"])
                continue
            if match[0] != ing["name"]:
                logger.info(f"{dish_id}: ингредиент «{ing['name']}» → «{match[0]}»")
                ing["name"] = match[0]
            stock_unit = inventory.get(ing["name"], {}).get("unit")
            if stock_unit and ing.get("unit") != stock_unit:
                logger.warning(
                    f"{dish_id}: единица у «{ing['name']}» — {ing.get('unit')}, "
                    f"а в запасах — {stock_unit}"
                )
        if unknown:
            logger.warning(f"Выбрасываю {dish_id} ({dish.get('name')}): нет в запасах {unknown}")
            dropped[dish_id] = unknown
            continue
        checked[dish_id] = dish
    return checked, dropped


# Почти пустые позиции не показываем модели при генерации меню: на 0.19 г
# ягод или 6 г огурца она строит блюда, которые не из чего приготовить.
# Соль, перец, масло и сухие специи в промпте и так «всегда есть».
# Английские единицы — у продуктов, которые /addstock добавил не из INGREDIENT_EN.
MENU_MIN_STOCK = {
    "г": 20, "мл": 20, "кг": 0.02, "л": 0.02, "шт": 0.5,
    "g": 20, "ml": 20, "kg": 0.02, "l": 0.02, "pcs": 0.5, "pc": 0.5,
}


def usable_stock(inventory):
    return {
        name: info for name, info in inventory.items()
        if info.get("amount", 0) > 0
        and info.get("amount", 0) >= MENU_MIN_STOCK.get(info.get("unit"), 0)
    }


# Hand-written dishes from the static catalog, shown to Claude as the target
# level of detail and simplicity (chicken with vegetables is left out on
# purpose: the cook doesn't make it well).
REFERENCE_DISH_IDS = [
    "breakfast_bacon_eggs",
    "breakfast_steak_eggs",
    "breakfast_chicken_thigh_pan",
    "dinner_chicken_patties_mash",
    "dinner_pork_patties",
    "dinner_starter_borscht",
]


def build_reference_text():
    try:
        static = json.loads(DISHES_FILE.read_text())
    except Exception:
        return ""
    reference = {k: static[k] for k in REFERENCE_DISH_IDS if k in static}
    if not reference:
        return ""
    return (
        "\n\nВот примеры рецептов нужного уровня. Бери с них пример "
        "по детализации и простоте шагов (не копируй сами блюда):\n"
        + json.dumps(reference, ensure_ascii=False, indent=1)
        + "\n"
    )


async def generate_daily_menu_options(context: ContextTypes.DEFAULT_TYPE):
    """Раз в день, перед вечерним напоминанием, просит Claude придумать
    меню на завтра ИЗ ТОГО, ЧТО РЕАЛЬНО ЕСТЬ в запасах, и подменяет
    глобальный DISHES этими блюдами — весь остальной код (расчёт
    порций, вычитание из запасов, сборка сообщения повару) работает
    с этим точно так же, как раньше работал со статичным каталогом.
    Возвращает None, если меню готово, иначе причину сбоя для создателя."""
    global DISHES, INGREDIENT_TYPICAL_USAGE

    all_stock = load_inventory()
    inventory = usable_stock(all_stock)
    skipped = [f"{name} {info.get('amount')} {info.get('unit')}"
               for name, info in all_stock.items() if name not in inventory]
    if skipped:
        logger.info(f"Почти пустые позиции не передаю в генерацию меню: {', '.join(skipped)}")
    if not inventory:
        logger.warning("Запасы пусты — пропускаю генерацию меню, оставляю прошлый DISHES")
        return "пустые запасы"

    inventory_lines = "\n".join(
        f"{name}: {info['amount']} {info['unit']}" for name, info in inventory.items()
    )

    prefs = load_json(FOOD_PREFERENCES_FILE, {"dislikes": ["яблоко"], "ratings": {}})
    dislikes = prefs.get("dislikes", [])
    ratings = prefs.get("ratings", {})
    disliked_dishes = [name for name, r in ratings.items() if r.get("down", 0) > r.get("up", 0)]

    dislikes_note = (
        f"\n\nСемья не любит эти продукты — не используй их вообще, ни в "
        f"одном блюде: {', '.join(dislikes)}.\n" if dislikes else ""
    )
    bad_dishes_note = (
        f"\nЭти блюда семье не понравились раньше — не предлагай их снова "
        f"и не предлагай что-то очень похожее: {', '.join(disliked_dishes)}.\n"
        if disliked_dishes else ""
    )
    liked_dishes = [name for name, r in ratings.items()
                    if r.get("up", 0) > r.get("down", 0)]
    liked_note = (
        f"\nЭти блюда семье понравились. Делай что-то в таком же духе: "
        f"{', '.join(liked_dishes)}.\n" if liked_dishes else ""
    )

    too_much_dishes = [name for name, r in ratings.items()
                       if r.get("portion_much", 0) > r.get("portion_right", 0) + r.get("portion_little", 0)]
    too_little_dishes = [name for name, r in ratings.items()
                         if r.get("portion_little", 0) > r.get("portion_right", 0) + r.get("portion_much", 0)]

    portion_note = ""
    if too_much_dishes:
        portion_note += (
            f"\nЭтих блюд обычно готовится слишком много — если предложишь "
            f"что-то похожее, укажи чуть меньший amount_per_serving: "
            f"{', '.join(too_much_dishes)}.\n"
        )
    if too_little_dishes:
        portion_note += (
            f"\nЭтих блюд обычно не хватает — если предложишь что-то "
            f"похожее, укажи чуть больший amount_per_serving: "
            f"{', '.join(too_little_dishes)}.\n"
        )

    recent = load_json(RECENT_DISHES_FILE, [])
    recent_dish_names = [name for entry in recent for name in entry.get("dishes", [])]
    recent_note = (
        f"\n\nЭти блюда уже готовились в последние {RECENT_DISHES_DAYS} дней — "
        f"не повторяй их снова, придумай другие варианты: "
        f"{', '.join(recent_dish_names)}.\n" if recent_dish_names else ""
    )

    egg_variety_note = (
        "\n\nЕсли основное блюдо на завтрак связано с яйцами — обязательно "
        "меняй способ приготовления день ото дня: яичница, яйца-скрэмбл, "
        "варёные яйца, омлет, фриттата, запечённые в духовке яйца и "
        "похожие простые варианты. Не предлагай один и тот же способ (например, "
        "омлет) несколько дней подряд — смотри на список недавних блюд выше, "
        "чтобы понять, что уже было.\n"
    )

    prompt = (
        "Ты составляешь меню для семьи на 7 человек. Готовит человек без "
        "кулинарного опыта, который делает ровно то, что написано, и не "
        "понимает ничего «на глаз». Нужны простые, знакомые, вкусные блюда, "
        "а не ресторанные эксперименты. Качество важнее количества и "
        "оригинальности: предлагай только то, в чём уверен, что оно "
        "получится вкусным при простом исполнении.\n\n"
        "Ниже точный список продуктов, которые сейчас реально есть на "
        "кухне (в граммах/мл/штуках). Придумай меню на завтра без глютена "
        "(кроме их собственного безглютенового хлеба, если он есть в "
        "списке — других заменителей глютена не используй, если их нет "
        "в списке явно).\n\n"
        f"Продукты в наличии:\n{inventory_lines}\n"
        + FAMILY_FAVORITES_NOTE + build_reference_text()
        + BREAKFAST_NOTE + BANANA_NOTE
        + dislikes_note + bad_dishes_note + liked_note + portion_note
        + recent_note + egg_variety_note +
        "\nКатегории и сколько вариантов придумать в каждой:\n"
        "Завтрак: main (3 варианта), meat (2), cold_starter (1), "
        "hot_starter (1), side (1), sauce (1), dessert (1). Категории soup "
        "на завтрак НЕТ, блюда breakfast/soup не генерируй вообще.\n"
        "Ужин: starter (1), hot_starter (1), soup (1), main (3), side (2), "
        "sauce (1), dessert (1).\n\n"
        "Обязательные категории (main и meat на завтрак, main и side на "
        "ужин) пропускать нельзя. Для необязательных: если нет "
        "по-настоящему хорошего простого блюда из имеющихся продуктов, "
        "просто не включай ни одного блюда этой категории в ответ вместо "
        "слабого блюда.\n\n"
        "ПРАВИЛА ПРОСТОТЫ (обязательны):\n"
        "1. Только знакомые домашние блюда и техники: пожарить на "
        "сковороде, сварить, запечь в духовке, потушить, нарезать, "
        "перемешать.\n"
        "2. Нельзя: сырая говядина или рыба как готовое блюдо (карпаччо, "
        "тартар), яйца пашот, муссы и кремы, взбитые сливки, "
        "соусы-эмульсии, фламбе, карамелизация сахара, любые «шеф-приёмы».\n"
        "3. Из техники допустимы только: плита, сковорода, кастрюля, "
        "духовка, нож, доска, ложка, лопатка, тёрка, шумовка, дуршлаг, "
        "миска. Не требуй блендер, миксер, "
        "кухонный термометр, гриль-сковороду. Супы только непротёртые.\n"
        "4. Не больше 8 ингредиентов (соль, перец, масло, сухие специи не "
        "считаются) и не больше 10 шагов в рецепте. Один шаг — одно "
        "действие: не «нарежьте курицу, картофель и лук», а отдельный шаг "
        "на каждый продукт.\n"
        "5. Всегда точные количества, никаких «по вкусу». Меры только "
        "бытовые: столовая ложка (15 мл), чайная ложка (5 мл), минуты, "
        "цвет или признак готовности («до золотистой корочки», «пока не "
        "исчезнет розовый цвет внутри»). Соль, чёрный перец и специи тоже "
        "с количеством прямо в шаге: не «посолите и поперчите», а "
        "«добавьте 1/4 чайной ложки соли и щепотку чёрного перца».\n"
        "6. В каждом шаге: что делать, на каком огне, сколько минут, как "
        "понять, что готово.\n"
        "7. Всегда конкретное название продукта: какой именно сыр "
        "(пармезан или чеддер), какой именно лук, какая именно часть "
        "курицы. Не просто «сыр» или «зелень».\n"
        "8. Названия блюд простые и понятные, без витиеватых.\n"
        "9. Все количества в шагах рецепта (граммы, мл воды, ложки, штуки) "
        "пиши на ОДНУ порцию, так же как amount_per_serving в ingredients. "
        "На нужное число порций их пересчитают при отправке повару.\n\n"
        "СТРОГИЕ правила по продуктам:\n"
        "1. Каждое блюдо должно быть реально приготовимо ИЗ ТОГО, ЧТО ЕСТЬ "
        "В СПИСКЕ ВЫШЕ — не придумывай ингредиенты, которых там нет, "
        "кроме соли, чёрного перца, растительного/оливкового масла и "
        "обычных сухих специй (паприка, зира, орегано, чили хлопья, "
        "уксус) — это всегда считается, что есть под рукой, даже если "
        "не в списке.\n"
        "2. Названия ингредиентов в поле \"ingredients\" должны СОВПАДАТЬ "
        "БУКВАЛЬНО с названиями из списка продуктов выше — то же слово, "
        "тот же регистр, никаких синонимов.\n"
        "3. Единица (unit) ингредиента должна быть ТОЧНО той, что указана у "
        "этого продукта в списке выше: если лук, чеснок, перец или морковь "
        "в запасах в граммах, пиши их в граммах (например, 40 г лука), а не "
        "«0.3 шт». Воду в ingredients не включай.\n"
        "4. Не предлагай блюдо, для которого на одну порцию нужно больше "
        "конкретного продукта, чем реально есть в наличии.\n"
        "5. Все блюда без глютена.\n\n"
        "Верни ТОЛЬКО валидный JSON, без какого-либо текста до или после, "
        "в виде плоского словаря id -> блюдо, в точности такой структуры "
        "(это должно совпадать со схемой существующего dishes.json — "
        "meal_type, course, description, ingredients с unit и "
        "amount_per_serving на ОДНУ порцию, recipe_steps, serving_note):\n\n"
        "{\n"
        '  "b_main_1": {"name": "...", "meal_type": "breakfast", '
        '"course": "main", "description": "...", "ingredients": '
        '[{"name": "...", "unit": "г", "amount_per_serving": 100}], '
        '"recipe_steps": ["..."], "serving_note": "..."},\n'
        '  "b_meat_1": {"name": "...", "meal_type": "breakfast", '
        '"course": "meat", ...},\n'
        "  ... (аналогично для cold_starter, hot_starter, side, sauce, "
        "dessert на завтрак, у каждого блюда, включая main, course = "
        "точное название категории)\n"
        '  "d_starter_1": {"name": "...", "meal_type": "dinner", '
        '"course": "starter", ...},\n'
        "  ... (аналогично для hot_starter, soup, main, side, sauce, "
        "dessert на ужин)\n"
        "}\n\n"
        "id — короткий уникальный идентификатор латиницей вида "
        '"b_main_1", "d_soup_1" (b_ для завтрака, d_ для ужина, дальше '
        "название категории и номер по порядку)."
    )

    # Размышления выключены: они съедали лимит токенов, и JSON с меню
    # обрывался. Streaming оставлен — без него SDK не даёт ставить такой
    # max_tokens.
    text_parts = []
    try:
        with client.messages.stream(
            model="claude-sonnet-5",
            max_tokens=16000,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for chunk in stream.text_stream:
                text_parts.append(chunk)
            final_message = stream.get_final_message()
    except APIError as e:
        logger.error(f"Генерация меню: ошибка API: {e}. Оставляю прошлый DISHES.")
        return f"ошибка API: {e}"
    logger.info(
        f"Генерация меню: stop_reason={final_message.stop_reason}, "
        f"output_tokens={final_message.usage.output_tokens}"
    )
    text = "".join(text_parts).strip()
    if final_message.stop_reason != "end_turn":
        logger.error(
            f"Ответ с меню оборвался ({final_message.stop_reason}), "
            f"начало ответа: {text[:300]!r}. Оставляю прошлый DISHES."
        )
        return f"обрыв ({final_message.stop_reason})"

    # На случай, если ответ обёрнут в ```json ... ```
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        generated = json.loads(text)
        if not isinstance(generated, dict):
            raise ValueError(f"в ответе {type(generated).__name__}, а не словарь блюд")
    except ValueError as e:  # JSONDecodeError тоже ValueError
        logger.error(
            f"Не удалось распарсить сгенерированное меню: {e}. "
            f"Первые 300 символов ответа: {text[:300]!r}. Оставляю прошлый DISHES."
        )
        return "ошибка разбора JSON"

    total = len(generated)
    generated, dropped = check_generated_ingredients(generated, inventory)
    missing = Counter(name for names in dropped.values() for name in set(names))
    logger.info(
        f"Сверка с запасами: выброшено {len(dropped)} из {total} блюд"
        + (": " + ", ".join(f"«{name}» — {n}" for name, n in missing.most_common()) if missing else "")
    )
    if not generated:
        logger.error("После сверки с запасами не осталось ни одного блюда. Оставляю прошлый DISHES.")
        return f"сверка выбросила все блюда: не нашлись {', '.join(missing) or 'ингредиенты'}"

    global DISHES_GENERATED_DATE
    DISHES_GENERATED_DATE = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    DISHES = generated
    save_json(TODAY_MENU_CACHE_FILE, DISHES)
    INGREDIENT_TYPICAL_USAGE = build_ingredient_typical_usage()
    logger.info(f"Сгенерировано меню на сегодня: {len(DISHES)} блюд")
    return None


async def scheduled_menu_generation(context: ContextTypes.DEFAULT_TYPE):
    """Плановая генерация (и догоняющая при старте): о сбое пишем создателю,
    иначе его видно только по «Меню ещё не готово» в мини-аппе."""
    reason = await generate_daily_menu_options(context)
    if reason:
        try:
            await context.bot.send_message(
                chat_id=CREATOR_CHAT_ID,
                text=f"Не получилось сгенерировать меню: {reason}. Запустите /regenmenu",
            )
        except Exception as e:
            logger.error(f"Не удалось предупредить создателя: {e}")


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
        f"Каждый вечер в {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} буду присылать кнопку — меню каждый "
        "день новое, его придумывает ИИ прямо под то, что реально есть на "
        "кухне. Выбираешь по одному варианту в каждой категории (завтрак: "
        "Основное, Мясное, Холодные/Горячие закуски, Гарнир, Суп, Соус, "
        "Десерт; ужин — то же самое плюс Стартер) — какие-то категории "
        "обязательные, какие-то нет, форма подскажет.\n\n"
        f"Если кто-то забудет проголосовать — в {NUDGE_HOUR:02d}:{NUDGE_MINUTE:02d} придёт ещё одно "
        "личное напоминание, но только тем, кто ещё не проголосовал.\n\n"
        "После еды можно написать боту /rate — придут кнопки по каждому "
        "сегодняшнему блюду: понравилось или нет, и хватило ли порции. Это "
        "помогает ИИ в следующий раз готовить точнее под вкусы семьи.\n\n"
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
    allowed = bool(
        user.id in (CREATOR_CHAT_ID, FAMILY_HEAD_CHAT_ID)
        or (FAMILY_HEAD_USERNAME
            and (user.username or "").lstrip("@").lower() == FAMILY_HEAD_USERNAME)
    )
    logger.info(f"/{command} от id={user.id} username={user.username!r}, доступ={'да' if allowed else 'нет'}")
    if not allowed:
        await update.message.reply_text("Эта команда доступна только главе семьи.")
    return allowed


async def regenmenu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной перезапуск генерации меню — только для главы семьи. Нужен,
    когда деплой/рестарт после GENERATE_HOUR:GENERATE_MINUTE сбросил
    сегодняшнее меню и мини-апп показывает «Меню ещё не готово»."""
    if not await check_admin(update, "regenmenu"):
        return

    await update.message.reply_text("Генерирую меню на сегодня, это займёт пару минут…")
    reason = await generate_daily_menu_options(context)
    if not reason:
        await update.message.reply_text(f"Готово: {len(DISHES)} блюд, можно голосовать.")
        return
    text = f"Не получилось сгенерировать меню: {reason}. Подробности в логах Render."
    await update.message.reply_text(text)
    if update.effective_user.id != CREATOR_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=CREATOR_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"Не удалось предупредить создателя: {e}")


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
        "Every day around 16:45, the bot looks at what's actually in the "
        "kitchen and asks AI to come up with tonight's and tomorrow's menu "
        "options — only dishes that can really be made from what's in "
        "stock. Around 17:30 the family gets a message to vote on their "
        "favorites (breakfast: Main, Meat, Cold/Hot Appetizer, Side, Soup, "
        "Sauce, Dessert; dinner: the same categories, plus Starter). "
        "Anyone who hasn't voted yet gets one more reminder around 18:00. "
        "By around 18:30, you get this chat's message: the winning dishes, "
        "scaled to the right number of portions, with full step-by-step "
        "recipes — that's your instructions for the day.\n\n"
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
        "/whatstobuy — see what's missing to unlock more dishes from "
        "today's menu, with exact amounts\n"
        "/shoppinglist — see the current shopping list accumulated so far "
        "this week (a fuller version also arrives automatically once a "
        "week, grouped by store section)\n"
        "/rate — rate today's dishes (👍/👎 for taste, and whether the "
        "portion size was right) — this shapes what the AI suggests going "
        "forward\n"
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

    def valid_pick(pick):
        if not pick or not isinstance(pick, dict):
            return None
        if pick.get("id") not in DISHES:
            return None
        return pick

    breakfast_raw = data.get("breakfast") or {}
    breakfast_pick = {
        course: valid_pick(breakfast_raw.get(course))
        for course in ("main", "meat", "cold_starter", "hot_starter", "side", "soup", "sauce", "dessert")
    }

    dinner_raw = data.get("dinner") or {}
    dinner_pick = {
        course: valid_pick(dinner_raw.get(course))
        for course in ("starter", "hot_starter", "soup", "main", "side", "sauce", "dessert")
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
        ("main", "meat", "cold_starter", "hot_starter", "side", "soup", "sauce", "dessert"),
    )
    dinner = _tally_meal(
        [(r["name"], r.get("dinner"), r.get("vote_weight", 1)) for r in responses.values()],
        ("starter", "hot_starter", "soup", "main", "side", "sauce", "dessert"),
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


KITCHEN_FACTS = (
    "2 large frying pans, 1 large pot (4 litres), an oven, a baking tray, "
    "foil, paper towels, tongs, slotted spoon, spatula, grater, sharp "
    "knife, cutting board, a kitchen thermometer, a timer"
)

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

COOK_FORMAT_PROMPT = """You are writing tomorrow's cooking instructions for a home cook. The cook reads English, follows instructions literally, and has little cooking experience. Write everything in clear simple English. Translate every dish name, ingredient and step into English. Leave no Russian words.

THE OUTPUT IS PLAIN TEXT FOR TELEGRAM. Do NOT use markdown: no #, no **, no tables, no backticks. Use emoji and the ━ lines for structure, exactly as in the FORMAT below.

KITCHEN FACTS (assume these): {kitchen_facts}. Buffet style: each dish goes on one big shared platter or bowl. Quantities already include a small extra for seconds.

RULES FOR THE CONTENT
1. Keep every ingredient, quantity, time and temperature from the dish data. Never invent ingredients. You MAY: split a step with several actions into several steps so that every step is ONE action; add short cues (what it looks like, sounds like or smells like); add warnings; add prep steps; convert spoons. Quantities inside the recipe steps in the data are for ONE portion: rewrite them for the number of portions of that dish.
2. Be exact. Always give amounts (grams, ml, teaspoons, minutes, heat level out of 10). Never write "to taste", "a bit", "some", "a pinch" without a number. Salt and pepper always get an amount.
3. Name products exactly. "Hard cheese" is the yellow cheddar block, never parmesan. Write "hard cheese (yellow cheddar block, NOT parmesan)" the first time in each dish. Write "parmesan" only when the data says parmesan.
4. Meat and fish are judged with the thermometer, with a finger or visual check as backup: beef steak 55°C off the heat (rises to about 58°C, medium) unless the data asks for another doneness; fish 60°C; chicken, pork and any mince 74°C. If voters asked for a doneness, give the matching temperature (rare 50°C, medium-rare 52°C, medium 55°C, medium-well 60°C, well done 65°C, all off the heat).
5. If the portions do not fit in one pan, split into batches (for example 3 steaks, 5 eggs, 5 bacon slices, 270 g of mushrooms per batch) and write the batch plan: "Repeat steps X to Y for the next batch".
6. Eggs and anything that must be hot are cooked LAST. Cold dishes and long-simmering dishes are made FIRST. Prep such as slicing cheese or grating goes under GET READY, before the stove is on.
7. Never write that the instructions are simplified or that the cook is inexperienced. Just give clear complete steps.
8. If the family notes mention an allergy or a wish, add a ⚠️ line inside the dish it affects and say exactly how to change the dish.
9. If there is a section for an early kids' breakfast, put it FIRST. If a dish is for one person only (plated separately, no meat), put it in its own section and add which shared dishes that person can also eat and that they do NOT eat the meat dishes.
10. Keep each dish under about 3000 characters.
11. Never name people and never state anyone's gender (no he, she, his, her). The data may contain names: do not repeat them. For a dish for one person, write "the person who is fasting" (or "the person who ..." matching the reason). For a doneness wish, write how many portions need it, not whose they are.
12. COMPLETENESS: every dish in the data appears exactly once in its own section. If the same dish is in the early kids' breakfast and in the regular breakfast, write it out in full in BOTH sections. Before answering, count the dishes in the data and in your answer.
13. SALT: 1 teaspoon of fine salt = 6 g; convert grams with this. A steak gets at most 1/2 teaspoon of salt per steak. Give salt per steak (or per portion) and make sure the total in YOU NEED matches: an amount "per batch" times the number of batches. For soup give a start and a maximum: "Add 1 teaspoon, stir for 1 minute, taste. If still flat, add 1/2 teaspoon at a time, no more than 2 teaspoons in total."
14. NO VAGUE AMOUNTS: never write "a little", "a bit", "to taste", "as needed", "adjust". Oil always has an amount ("1 tablespoon (15 ml) of oil").
15. DONENESS: count the doneness requests per level; portions without a request are medium. Write one line such as "Steaks: 2 x medium-rare (52°C), 6 x medium (55°C), 1 x well done (65°C)" and group the batches by doneness ("Batch 1: 3 steaks, medium, take off at 55°C"). Describe batches in pieces, not grams. Never say who asked for what.
16. TIMES: the duration in a dish header matches its steps; "Start at T minus X" is at least the duration plus 5 minutes; the times in the countdown match the dish headers.
17. ORDER: sections go early kids' breakfast, regular breakfast, breakfast for one person, dinner, dinner for one person. The dinner for one person comes AFTER the last shared dinner dish and BEFORE the dinner final check.
18. SALTED WATER: whenever a step uses water for boiling, give the salt for it: "add 1 teaspoon of salt per litre of water".
19. IN ADVANCE: never write "in advance", "beforehand" or "ahead of time" without minutes. Write "take the steaks out of the fridge 30 minutes before cooking".
20. DURATION: the duration in a dish header = prep time + number of batches x time of one batch. Use these batch times: bacon 9 minutes, eggs 4 minutes, steaks 11 minutes, mushrooms 11 minutes. The dish starts in the countdown at its duration + 5 minutes ("⏱ 35 minutes" means "Start at T minus 40 min" or earlier).

FORMAT (follow exactly; replace the examples with today's real dishes)

Write one section header for each meal in the data, in the order from rule 17. Section headers say "(N people)" and never "serves"; only dish headers end with "— serves N":

━━━━━━━━━━━━━━━━━━━━
☀️ REGULAR BREAKFAST (7 people)
━━━━━━━━━━━━━━━━━━━━

Right under a regular meal header, write a COUNTDOWN with only today's dishes. T = the meal is served. Use minutes before T, say which pan each dish uses, put long and cold dishes first and eggs and steaks last, and make the times realistic:
⏰ COUNTDOWN (T = breakfast is served)
T minus 70 min: Fruit salad and cucumber salad, then into the fridge
T minus 55 min: Steaks with ham and cheese (pan 1)
T minus 12 min: Eggs LAST (pan 2), so they do not go cold
T minus 0: Everything on the table

Then every dish in this exact layout:

🍄 FRIED MUSHROOMS — serves 9
⏱ 40 minutes (pan 1). Start at T minus 50 min.

YOU NEED
• Mushrooms: 810 g
• Butter: 90 g
• Salt: 1 teaspoon
• Black pepper: 1/2 teaspoon

GET READY
1. Slice the mushrooms about 5 mm thick.
2. Divide them into 3 equal piles (about 270 g each).

COOK (3 batches)
3. Heat the large pan on medium-high heat for 2 minutes. Do not add oil.
4. Add 30 g of butter and let it melt.
5. Add the first pile in one layer. Do not crowd the pan.
6. Leave it for 3 minutes without stirring.
7. Stir. Cook 6 to 8 more minutes, stirring every minute.
✅ READY WHEN: all the water has gone from the pan and the mushrooms are golden brown.
8. Move the mushrooms to a warm bowl and cover with foil.
9. Repeat steps 4 to 8 with the 2nd and 3rd piles.
⚠️ Salt at the END only. Early salt makes mushrooms watery.

After the last dish of each meal, a final check:
✅ DINNER FINAL CHECK (before you call the family)
• one bullet per dish: where it goes and whether it is hot or cold
• Serving spoons next to every dish

Put the exact line ---SPLIT--- on its own line between sections: after the meal header with its countdown, between every two dishes, and before each final check. Never put ---SPLIT--- inside a dish.
"""

TG_LIMIT = 3900  # Telegram allows 4096 characters per message, keep a margin


def sanitize_plain(text):
    """На случай, если ИИ всё же написал markdown — сообщение уходит без parse_mode."""
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
        units.extend([p] if len(p) <= limit else split_big(p, limit))
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


# Dish headers in the cook message end with "— serves N"; section headers say "(N people)".
COOK_DISH_LINE = re.compile(r"[—–-]\s*serves \d+\s*$", re.MULTILINE)
VAGUE_PHRASES = ["to taste", "a little", "a bit of", "as needed", "adjust", "as much as"]
# Строка под заголовком блюда: «⏱ N minutes ... Start at T minus M min».
COOK_TIME_LINE = re.compile(r"⏱\s*(\d+)\s*min.*?Start at T minus (\d+)\s*min", re.IGNORECASE)


def check_cook_text(text):
    """Возвращает (число блюд в тексте, найденные расплывчатые фразы)."""
    lowered = text.lower()
    return len(COOK_DISH_LINE.findall(text)), [p for p in VAGUE_PHRASES if p in lowered]


def check_cook_timing(text):
    """Блюда, которые начинают меньше чем за длительность + 5 минут до подачи:
    список (название, длительность N, старт M) по строкам «⏱ N minutes ...
    Start at T minus M min» под заголовками блюд."""
    late, dish = [], None
    for line in text.splitlines():
        if COOK_DISH_LINE.search(line):
            dish = re.sub(r"^\W+", "", COOK_DISH_LINE.sub("", line)).strip()  # без эмодзи
            continue
        m = COOK_TIME_LINE.search(line)
        if m and dish:
            duration, start = int(m.group(1)), int(m.group(2))
            if start < duration + 5:
                late.append((dish, duration, start))
    return late


def describe_cook_problems(dish_count, expected_dishes, vague, late=()):
    """Перечень проблем черновика по-английски — для повторного запроса к ИИ."""
    problems = []
    for dish, duration, start in late:
        problems.append(
            f"- Dish {dish}: it takes {duration} minutes but starts only {start} "
            f"minutes before serving, start at least {duration + 5} minutes before"
        )
    if dish_count != expected_dishes:
        problems.append(
            f"- The message has {dish_count} dishes (lines ending with \"— serves N\"), "
            f"but the data has {expected_dishes}. Every dish must appear exactly once in "
            "its section; a dish that is both in the early kids' breakfast and in the "
            "regular breakfast is written out in full in both."
        )
    if vague:
        problems.append(
            "- Vague phrases found: " + ", ".join(f"\"{p}\"" for p in vague)
            + ". Replace each one with an exact amount or a clear rule."
        )
    return "\n".join(problems)


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

    served = []
    for meal in ("breakfast", "dinner"):
        for course_data in menu[meal].values():
            served.append({"name": DISHES[course_data["dish_id"]]["name"], "meal": meal})
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
        voters_note = f" (голосовали: {format_voters(voters)})" if voters else ""
        return (
            f"{dish['name']} — на {portions} {plural_portions(portions)}"
            f"{voters_note}. "
            f"Ингредиенты: {scaled}. Приготовление: {steps} Подача: {dish['serving_note']}"
        )

    kids_section = ""
    if menu["breakfast"].get("main"):
        main_dish_id = menu["breakfast"]["main"]["dish_id"]
        kids_portions = KIDS_BREAKFAST_COUNT + KIDS_BREAKFAST_BUFFER
        kids_section = (
            "## EARLY KIDS' BREAKFAST (prepare first, before the regular breakfast)\n\n"
            + format_dish_block(main_dish_id, kids_portions, [])
            + "\n\nServe this early, separately from the rest of the family's breakfast — "
            "just this one dish, not the full spread."
        )

    sections = []
    if kids_section:
        sections.append(kids_section)
    breakfast_labels = {
        "main": "Основное", "meat": "Мясное блюдо",
        "cold_starter": "Холодная закуска", "hot_starter": "Горячая закуска",
        "side": "Гарнир", "soup": "Суп", "sauce": "Соус", "dessert": "Десерт",
    }
    if menu["breakfast"]:
        lines = ["## Завтрак"]
        for course in ("main", "meat", "cold_starter", "hot_starter", "side", "soup", "sauce", "dessert"):
            if course in menu["breakfast"]:
                item = menu["breakfast"][course]
                lines.append(f"### {breakfast_labels[course]}")
                lines.append(format_dish_block(item["dish_id"], item["portions"], item["voters"]))
        sections.append("\n\n".join(lines))

    dinner_labels = {
        "starter": "Холодная закуска", "hot_starter": "Горячая закуска",
        "soup": "Суп", "main": "Основное", "side": "Гарнир",
        "sauce": "Соус", "dessert": "Десерт",
    }
    if menu["dinner"]:
        lines = ["## Ужин"]
        for course in ("starter", "hot_starter", "soup", "main", "side", "sauce", "dessert"):
            if course in menu["dinner"]:
                item = menu["dinner"][course]
                lines.append(f"### {dinner_labels[course]}")
                lines.append(format_dish_block(item["dish_id"], item["portions"], item["voters"]))
        sections.append("\n\n".join(lines))

    notes_lines = [f"{r['name']}: {r['notes']}" for r in responses.values() if r.get("notes")]
    if notes_lines:
        sections.append("Комментарии по людям:\n" + "\n".join(notes_lines))

    plan_summary = "\n\n---\n\n".join(sections)

    # Повторная сборка за день (/forcecompile) не должна второй раз вычитать
    # запасы и дописывать недостачу — откатываемся к состоянию до первой.
    snapshot_file = DATA_DIR / f"before_compile_{datetime.now(TIMEZONE).date()}.json"
    if snapshot_file.exists():
        snapshot = load_json(snapshot_file, {})
        save_inventory(snapshot["inventory"])
        save_json(WEEKLY_SHORTFALL_FILE, snapshot["weekly_shortfall"])
        logger.info("Повторная сборка за сегодня — восстановил запасы до первой сборки")
    else:
        save_json(snapshot_file, {
            "inventory": load_inventory(),
            "weekly_shortfall": load_json(WEEKLY_SHORTFALL_FILE, {}),
        })

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

    weekday = WEEKDAYS_EN[(datetime.now(TIMEZONE) + timedelta(days=1)).weekday()]
    intro_text = COOK_INTRO.format(weekday=weekday)

    prompt = (
        COOK_FORMAT_PROMPT.format(kitchen_facts=KITCHEN_FACTS)
        + "\nВот меню на завтра по итогам голосования (ингредиенты уже "
        "пересчитаны на указанное число порций, количества внутри шагов "
        "даны на одну порцию):\n"
        + plan_summary
        + "\n"
    )

    def ask_cook_text(messages, attempt):
        # Размышления выключены: с ними модель тратила на планирование ~33k
        # токенов из лимита и обрывала текст повару на середине.
        text_parts = []
        with client.messages.stream(
            model="claude-sonnet-5",
            max_tokens=16000,
            thinking={"type": "disabled"},
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                text_parts.append(chunk)
            final_message = stream.get_final_message()
        text = "".join(text_parts)
        logger.info(
            f"Compile-сообщение повару ({attempt}): stop_reason={final_message.stop_reason}, "
            f"output_tokens={final_message.usage.output_tokens}, "
            f"длина={len(text)} символов"
        )
        return text, final_message.stop_reason

    async def warn_creator(text):
        try:
            await context.bot.send_message(chat_id=CREATOR_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"Не удалось предупредить создателя: {e}")

    async def cut_off(text, stop_reason):
        """Оборванный или пустой текст повару не шлём — создатель перезапустит сборку."""
        if stop_reason == "end_turn" and text.strip():
            return False
        reason = stop_reason if stop_reason != "end_turn" else "пустой ответ"
        logger.error(f"Сообщение повару оборвалось ({reason}) — повару ничего не отправлено")
        await warn_creator(
            f"Сообщение повару оборвалось ({reason}), повару ничего не "
            "отправлено. Запустите /forcecompile"
        )
        return True

    # Столько блюд, сколько в plan_summary: завтрак + ужин (блюда на одного
    # уже среди них) + отдельный блок школьников.
    expected_dishes = len(menu["breakfast"]) + len(menu["dinner"]) + (1 if kids_section else 0)

    first_request = [{"role": "user", "content": prompt}]
    recipe_text, stop_reason = ask_cook_text(first_request, "черновик")
    if await cut_off(recipe_text, stop_reason):
        return

    # Проверяем только текст ИИ — фиксированное вступление COOK_INTRO сюда не входит.
    dish_count, vague = check_cook_text(recipe_text)
    late = check_cook_timing(recipe_text)
    if dish_count == expected_dishes and not vague and not late:
        logger.info(f"Повтора не было: блюд {dish_count} из {expected_dishes}, "
                    "расплывчатых фраз и поздних стартов нет")
    else:
        problems = describe_cook_problems(dish_count, expected_dishes, vague, late)
        logger.warning(f"Повторный запрос к ИИ, причины:\n{problems}")
        recipe_text, stop_reason = ask_cook_text(first_request + [
            {"role": "assistant", "content": recipe_text},
            {"role": "user", "content": (
                f"Your draft has these problems:\n{problems}\n\n"
                "Write the COMPLETE message again with every problem fixed. "
                "Same format, same ---SPLIT--- rules."
            )},
        ], "повтор")
        if await cut_off(recipe_text, stop_reason):
            return
        dish_count, vague = check_cook_text(recipe_text)
        late = check_cook_timing(recipe_text)
        if dish_count != expected_dishes:
            logger.error(f"После повтора блюд всё ещё {dish_count} из {expected_dishes} — повару ничего не отправлено")
            await warn_creator(
                f"В сообщении повару не хватает блюд (в ответе {dish_count} из "
                f"{expected_dishes}). Повару ничего не отправлено. Запустите /forcecompile"
            )
            return
        if vague:
            logger.warning(f"После повтора остались расплывчатые фразы {vague} — отправляю как есть")
        if late:
            logger.warning(f"После повтора остались поздние старты {late} — отправляю как есть")
        if not vague and not late:
            logger.info(f"После повтора всё сошлось: блюд {dish_count} из {expected_dishes}")

    # ИИ ставит ---SPLIT--- между секциями и блюдами; режем только по ним,
    # чтобы блюдо не обрывалось на середине. Без разделителей split_big
    # порежет текст по пустым строкам.
    body = sanitize_plain(recipe_text)
    pieces = [intro_text] + body.split("---SPLIT---")
    messages = pack_messages(pieces)
    logger.info(f"Сообщение повару: {len(messages)} частей, длины: {[len(m) for m in messages]}")
    for m in messages:
        try:
            await context.bot.send_message(chat_id=COOK_CHAT_ID, text=m)
        except Exception as e:
            logger.error(f"Не удалось отправить часть сообщения повару: {e}")
        await asyncio.sleep(0.5)


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
            result = {"breakfast": {}, "dinner": {}, "generated_date": DISHES_GENERATED_DATE}
            for dish_id, dish in DISHES.items():
                meal = dish.get("meal_type")
                if meal not in result:
                    continue
                course = dish.get("course") or ("main" if meal == "breakfast" else None)
                if not course:
                    continue
                result[meal].setdefault(course, []).append({
                    "id": dish_id,
                    "name": dish.get("name", ""),
                    "description": dish.get("description", ""),
                    "customize": dish.get("customize", []),
                })
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/current-stock":
            inventory = load_inventory()
            all_names = set(inventory.keys())
            units_by_name = {k: v.get("unit", "") for k, v in inventory.items()}

            for dish in DISHES.values():
                for ing in dish.get("ingredients", []):
                    if ing["name"] not in all_names:
                        all_names.add(ing["name"])
                        units_by_name[ing["name"]] = ing["unit"]

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
    app.add_handler(CallbackQueryHandler(rate_callback, pattern=r"^rate\|"))
    app.add_handler(CallbackQueryHandler(portion_callback, pattern=r"^portion\|"))
    app.add_handler(CallbackQueryHandler(rate_taste_cb, pattern=r"^rt\|"))
    app.add_handler(CallbackQueryHandler(rate_portion_cb, pattern=r"^rp\|"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_web_app_data))

    job_queue = app.job_queue
    job_queue.run_daily(
        scheduled_menu_generation,
        time=dtime(hour=GENERATE_HOUR, minute=GENERATE_MINUTE, tzinfo=TIMEZONE),
    )
    # Деплой/рестарт после времени генерации сбрасывает сегодняшнее меню —
    # догоняем сразу, а не ждём завтрашнего запуска. До этого времени
    # ничего не делаем: обычная ежедневная задача сработает сама.
    now = datetime.now(TIMEZONE)
    generate_time_today = now.replace(hour=GENERATE_HOUR, minute=GENERATE_MINUTE, second=0, microsecond=0)
    if DISHES_GENERATED_DATE != now.strftime("%Y-%m-%d") and now >= generate_time_today:
        logger.info("Меню на сегодня ещё не сгенерировано — запускаю генерацию при старте")
        job_queue.run_once(scheduled_menu_generation, when=0)
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
