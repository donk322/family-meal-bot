import os
import json
import logging
import threading
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
COMPILE_HOUR = int(os.environ.get("COMPILE_HOUR", "21"))
COMPILE_MINUTE = int(os.environ.get("COMPILE_MINUTE", "30"))
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Johannesburg"))

# Weekly shopping list — 0=Monday ... 6=Sunday
WEEKLY_SHOPPING_DAY = int(os.environ.get("WEEKLY_SHOPPING_DAY", "6"))
WEEKLY_SHOPPING_HOUR = int(os.environ.get("WEEKLY_SHOPPING_HOUR", "21"))
WEEKLY_SHOPPING_MINUTE = int(os.environ.get("WEEKLY_SHOPPING_MINUTE", "35"))

# Alert the cook right away if an ingredient has less than this many
# typical servings left, instead of waiting for the weekly list.
LOW_STOCK_SERVINGS = float(os.environ.get("LOW_STOCK_SERVINGS", "2"))

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
    PANTRY_STAPLES = {
        "соль", "чёрный перец", "растительное масло", "оливковое масло",
        "паприка", "зира", "орегано", "розмарин", "чили хлопья",
        "уксус", "красный винный уксус", "горчица", "свежая зелень",
    }
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
        KeyboardButton(text="📦 Обновить запасы", web_app=WebAppInfo(url=STOCK_APP_URL))
    )


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    family = load_family()
    family[str(user.id)] = {"name": user.first_name}
    save_family(family)

    await update.message.reply_text(
        f"Привет, {user.first_name}! Записал тебя в список.\n\n"
        f"Каждый вечер в {REMINDER_HOUR}:00 буду присылать кнопку с меню, чтобы "
        "выбрать, что хочешь на завтрак и ужин. Можешь нажать её и сейчас, "
        "чтобы попробовать.",
        reply_markup=menu_keyboard(),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id этого чата: {update.effective_chat.id}")


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inventory = load_inventory()
    if not inventory:
        await update.message.reply_text("Запасы пока пустые — ничего не отмечено.")
        return
    lines = [f"{name}: {info['amount']} {info['unit']}" for name, info in sorted(inventory.items())]
    await update.message.reply_text("Текущие запасы:\n" + "\n".join(lines))


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
            "STOCK_APP_URL не настроен — сначала задай эту переменную окружения."
        )
        return
    await update.message.reply_text(
        "Жми кнопку и впиши, сколько чего сейчас есть дома — заполняй только то, "
        "что реально пересчитал, остальное не тронется.",
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


async def receive_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = json.loads(update.effective_message.web_app_data.data)

    if data.get("type") == "stock":
        stock_update = data.get("stock", {})
        if not stock_update:
            await update.message.reply_text("Ничего не заполнено — запасы не изменены.")
            return
        inventory = load_inventory()
        for name, item in stock_update.items():
            inventory[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_inventory(inventory)
        await update.message.reply_text(
            f"Обновил {len(stock_update)} позиций в запасах. Спасибо!"
        )
        return

    def parse_picks(raw):
        # Each item is {"id": dish_id, "egg": "...", "meat": "..."} — egg/meat optional
        picks = []
        for item in raw or []:
            dish_id = item.get("id")
            if dish_id in DISHES:
                picks.append(item)
        return picks

    breakfast_picks = parse_picks(data.get("breakfast"))
    dinner_picks = parse_picks(data.get("dinner"))

    responses = load_today_responses()
    responses[str(user.id)] = {
        "name": user.first_name,
        "breakfast": breakfast_picks,
        "dinner": dinner_picks,
        "notes": data.get("notes", ""),
    }
    save_today_responses(responses)

    await update.message.reply_text("Записал, спасибо! Повар получит это вечером.")


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


def aggregate_ingredients(responses):
    """Sum ingredient amounts needed across every person's chosen dishes."""
    needed = {}
    for r in responses.values():
        chosen_dishes = list(r.get("breakfast", [])) + list(r.get("dinner", []))
        for pick in chosen_dishes:
            dish_id = pick.get("id")
            if not dish_id or dish_id not in DISHES:
                continue
            for ing in DISHES[dish_id]["ingredients"]:
                key = ing["name"]
                if key not in needed:
                    needed[key] = {"amount": 0.0, "unit": ing["unit"]}
                needed[key]["amount"] += ing["amount_per_serving"]
    return needed


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


def build_dish_groups(responses):
    """Group selections by (meal_label, dish_id) -> list of {name, egg, meat}.

    This lets the cook batch-cook one dish for several people at once
    instead of repeating the same recipe person by person, while still
    keeping track of who wants their egg or meat done differently.
    """
    groups = {"завтрак": {}, "ужин": {}}
    for r in responses.values():
        for meal_label, picks in (("завтрак", r.get("breakfast", [])), ("ужин", r.get("dinner", []))):
            for pick in picks:
                dish_id = pick.get("id")
                if dish_id not in DISHES:
                    continue
                groups[meal_label].setdefault(dish_id, []).append({
                    "name": r["name"],
                    "egg": pick.get("egg"),
                    "meat": pick.get("meat"),
                })
    return groups


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
                f"{name} — осталось на {round(servings_left, 1)} порц. ({info['amount']} {info['unit']})"
            )

    if low_items:
        text = "⚠️ Скоро понадобится купить:\n" + "\n".join(low_items)
        await context.bot.send_message(chat_id=COOK_CHAT_ID, text=text)


async def compile_and_send_to_cook(context: ContextTypes.DEFAULT_TYPE):
    if not COOK_CHAT_ID:
        logger.warning("COOK_CHAT_ID не задан — некому отправлять план")
        return

    responses = load_today_responses()
    if not responses:
        logger.info("Пока никто не ответил — пропускаю сборку")
        return

    # Group by dish so the cook can batch-cook for several people at once
    # instead of repeating the same recipe person by person.
    groups = build_dish_groups(responses)
    meal_sections = []
    for meal_label in ("завтрак", "ужин"):
        dish_ids = groups[meal_label]
        if not dish_ids:
            continue
        lines = [f"## {meal_label.capitalize()}"]
        for dish_id, people in dish_ids.items():
            dish = DISHES[dish_id]
            portions = len(people)
            scaled_ingredients = ", ".join(
                f"{ing['name']} {round(ing['amount_per_serving'] * portions, 1)} {ing['unit']}"
                for ing in dish["ingredients"]
            )
            steps = " ".join(f"{i+1}) {s}" for i, s in enumerate(dish["recipe_steps"]))

            name_labels = []
            for p in people:
                extras = []
                if p.get("egg"):
                    extras.append(f"яйцо: {p['egg']}")
                if p.get("meat"):
                    extras.append(f"мясо: {p['meat']}")
                name_labels.append(f"{p['name']} ({', '.join(extras)})" if extras else p["name"])

            lines.append(
                f"{dish['name']} — на {portions} {plural_portions(portions)} "
                f"({', '.join(name_labels)}). Ингредиенты: {scaled_ingredients}. "
                f"Приготовление: {steps} Подача: {dish['serving_note']}"
            )
        meal_sections.append("\n\n".join(lines))

    notes_lines = [f"{r['name']}: {r['notes']}" for r in responses.values() if r.get("notes")]
    if notes_lines:
        meal_sections.append("Комментарии по людям:\n" + "\n".join(notes_lines))

    plan_summary = "\n\n---\n\n".join(meal_sections)

    needed = aggregate_ingredients(responses)
    shopping_list, updated_inventory = apply_inventory(needed)
    save_inventory(updated_inventory)
    await check_low_stock_and_alert(context, updated_inventory)

    prompt = (
        "Ты помогаешь повару приготовить еду для семьи на 7 человек. Все блюда "
        "без глютена — это уже учтено в рецептах ниже, не меняй ингредиенты и "
        "их количество (они уже пересчитаны на нужное число порций).\n\n"
        f"Вот план на завтра, сгруппированный по блюду, а не по человеку — "
        f"чтобы повар мог готовить сразу партиями:\n{plan_summary}\n\n"
        "Отформатируй это в чистое, удобное для повара сообщение: отдельный "
        "блок на каждое блюдо с указанием, для кого оно и на сколько порций, "
        "ингредиентами (уже пересчитанными на нужное число порций) и шагами "
        "приготовления. Рядом с именами некоторых людей в скобках указано, "
        "как приготовить их яйцо или мясо (например «Аня (яйцо: жидкий "
        "желток)») — обязательно вынеси это отдельной пометкой внутри блока "
        "блюда, чтобы повар знал, что в общей партии часть порций нужно снять "
        "с огня раньше или позже остальных, и явно укажи, кому какая степень "
        "готовности нужна. Если в разделе «Комментарии по людям» есть аллергия "
        "или пожелание, которое касается конкретного человека внутри общей "
        "партии — явно укажи в этом блоке, что одну порцию нужно отделить и "
        "видоизменить, и как именно. Не меняй количества и ингредиенты там, "
        "где комментариев нет."
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

    # Don't send today's shortfall as its own message — fold it into the
    # running weekly total instead, sent once a week (see send_weekly_shopping_list).
    if shopping_list:
        weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
        for name, item in shopping_list.items():
            if name in weekly:
                weekly[name]["amount"] = round(weekly[name]["amount"] + item["amount"], 1)
                weekly[name]["unit"] = item["unit"]
            else:
                weekly[name] = {"amount": item["amount"], "unit": item["unit"]}
        save_json(WEEKLY_SHORTFALL_FILE, weekly)


async def send_weekly_shopping_list(context: ContextTypes.DEFAULT_TYPE):
    if not COOK_CHAT_ID:
        logger.warning("COOK_CHAT_ID не задан — некому отправлять список покупок")
        return

    weekly = load_json(WEEKLY_SHORTFALL_FILE, {})
    if not weekly:
        await context.bot.send_message(
            chat_id=COOK_CHAT_ID,
            text="Список покупок на неделю: всё нужное уже было в запасах, докупать нечего.",
        )
        return

    lines = [f"{name}: {item['amount']} {item['unit']}" for name, item in weekly.items()]
    text = (
        "Список покупок на неделю:\n" + "\n".join(lines) +
        "\n\nКогда купите — отправьте боту команду /restock, чтобы обновить запасы."
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
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_web_app_data))

    job_queue = app.job_queue
    job_queue.run_daily(
        send_evening_reminders, time=dtime(hour=REMINDER_HOUR, minute=0, tzinfo=TIMEZONE)
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
