import os
import json
import logging
from datetime import time as dtime, datetime
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
COOK_CHAT_ID = int(os.environ.get("COOK_CHAT_ID", "0") or 0)

REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "20"))
COMPILE_HOUR = int(os.environ.get("COMPILE_HOUR", "21"))
COMPILE_MINUTE = int(os.environ.get("COMPILE_MINUTE", "30"))
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Johannesburg"))

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

FAMILY_FILE = DATA_DIR / "family.json"
INVENTORY_FILE = DATA_DIR / "inventory.json"
LAST_SHOPPING_LIST_FILE = DATA_DIR / "last_shopping_list.json"
DISHES_FILE = BASE_DIR / "dishes.json"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

DISHES = json.loads(DISHES_FILE.read_text())


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


def today_responses_file():
    return DATA_DIR / f"responses_{datetime.now(TIMEZONE).date()}.json"


def load_today_responses():
    return load_json(today_responses_file(), {})


def save_today_responses(responses):
    save_json(today_responses_file(), responses)


def menu_keyboard():
    return ReplyKeyboardMarkup.from_button(
        KeyboardButton(text="Выбрать меню на завтра", web_app=WebAppInfo(url=MINI_APP_URL))
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

    breakfast_ids = [d for d in (data.get("breakfast") or []) if d in DISHES]
    dinner_ids = [d for d in (data.get("dinner") or []) if d in DISHES]

    responses = load_today_responses()
    responses[str(user.id)] = {
        "name": user.first_name,
        "breakfast": breakfast_ids,
        "dinner": dinner_ids,
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
        for dish_id in chosen_dishes:
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
    """Group selections by (meal_label, dish_id) -> list of person names.

    This lets the cook batch-cook one dish for several people at once
    instead of repeating the same recipe person by person.
    """
    groups = {"завтрак": {}, "ужин": {}}
    for r in responses.values():
        for meal_label, dish_ids in (("завтрак", r.get("breakfast", [])), ("ужин", r.get("dinner", []))):
            for dish_id in dish_ids:
                if dish_id not in DISHES:
                    continue
                groups[meal_label].setdefault(dish_id, []).append(r["name"])
    return groups


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
            lines.append(
                f"{dish['name']} — на {portions} {plural_portions(portions)} "
                f"({', '.join(people)}). Ингредиенты: {scaled_ingredients}. "
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
    save_json(LAST_SHOPPING_LIST_FILE, shopping_list)

    prompt = (
        "Ты помогаешь повару приготовить еду для семьи на 7 человек. Все блюда "
        "без глютена — это уже учтено в рецептах ниже, не меняй ингредиенты и "
        "их количество (они уже пересчитаны на нужное число порций).\n\n"
        f"Вот план на завтра, сгруппированный по блюду, а не по человеку — "
        f"чтобы повар мог готовить сразу партиями:\n{plan_summary}\n\n"
        "Отформатируй это в чистое, удобное для повара сообщение: отдельный "
        "блок на каждое блюдо с указанием, для кого оно и на сколько порций, "
        "ингредиентами (уже пересчитанными на нужное число порций) и шагами "
        "приготовления. Если в разделе «Комментарии по людям» есть аллергия "
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

    if shopping_list:
        lines = [f"{name}: {item['amount']} {item['unit']}" for name, item in shopping_list.items()]
        shopping_text = "Список покупок на завтра:\n" + "\n".join(lines) + (
            "\n\nКогда купите — отправьте боту команду /restock, чтобы обновить запасы."
        )
    else:
        shopping_text = "Всё нужное уже есть в запасах — докупать ничего не нужно."

    await context.bot.send_message(chat_id=COOK_CHAT_ID, text=shopping_text)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("addstock", addstock))
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

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
