# -*- coding: utf-8 -*-
"""Тест списка блюд для голосования и сообщения о нехватке для выбранного меню.
Запуск из корня: python test_menu_today.py

Данные бота (data/) перенаправлены во временную папку, Telegram подменён
фейком, настоящий BOT_TOKEN не нужен."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("MINI_APP_URL", "https://example.invalid")

import bot  # noqa: E402  (main() только под __main__)
from catalog.scale_recipe import scaled_ingredients  # noqa: E402
from catalog.stock_check import check_dish, to_stock_unit  # noqa: E402

real_data = bot.DATA_DIR
tmp = Path(tempfile.mkdtemp(prefix="menu_today_test_"))
for name, val in list(vars(bot).items()):
    if isinstance(val, Path) and val.parent == real_data:
        setattr(bot, name, tmp / val.name)
bot.DATA_DIR = tmp
bot.COOK_CHAT_ID = 111

errors = []


def expect(name, got, want):
    if got != want:
        errors.append(f"{name}: получили {got!r}, ожидали {want!r}")


def check(name, cond):
    if not cond:
        errors.append(name)


def needs(dish, portions):
    """{stock_key: нужно в единице склада} без staple."""
    out = {}
    for key, unit, amount, staple in scaled_ingredients(dish, portions):
        if not staple:
            out[key] = out.get(key, 0) + to_stock_unit(key, unit, amount)
    return out


def set_stock(stock):
    """stock {stock_key: amount в g/ml/pcs} -> data/inventory.json с английскими ключами."""
    bot.save_inventory({k: {"amount": round(v, 2), "unit": bot.stock_unit_for(k)} for k, v in stock.items()})


def set_people(b=None, d=None):
    bot.save_headcount({"breakfast": b, "dinner": d, "days": 1} if b or d else dict(bot.HEADCOUNT_EMPTY))


def payload_ids(payload):
    return {(meal, course, d["id"]) for meal in ("breakfast", "dinner")
            for course, dishes in payload[meal].items() for d in dishes}


# --- тестовый сток: каждого продукта столько, сколько нужно самому
# «прожорливому» блюду на 9 порций; сыра и лосося нет совсем ---
ALL_KEYS = {}
for dish in bot.CATALOG.values():
    for key, amount in needs(dish, 9).items():
        ALL_KEYS[key] = max(ALL_KEYS.get(key, 0), amount)
REMOVED = {"hard cheese", "salmon fillet"}
TEST_STOCK = {k: v for k, v in ALL_KEYS.items() if k not in REMOVED}
set_stock(TEST_STOCK)
set_people()

# 1. В /menu-today только блюда, на которые хватает на запланированные порции
p57 = bot.menu_today_payload()
stock_now, _ = bot.inventory_as_english_stock()
for meal, course, dish_id in payload_ids(p57):
    portions = bot.planned_portions(meal, bot.load_headcount())
    check(f"{dish_id} в списке, но не хватает на {portions} порций",
          check_dish(bot.CATALOG[dish_id], portions, stock_now)[0])
    check(f"{dish_id} в списке, но в нём {REMOVED}", not (needs(bot.CATALOG[dish_id], 1).keys() & REMOVED))
expect("завтрак планируется на 5+2", bot.planned_portions("breakfast", bot.load_headcount()), 7)
expect("ужин планируется на 7+2", bot.planned_portions("dinner", bot.load_headcount()), 9)
full = sum(len(bot.pool(m, c)) for m, cs in bot.MEAL_ORDER.items() for c in cs)
check("на тестовом стоке часть блюд должна пропасть", 0 < len(payload_ids(p57)) < full)
with_removed = [d for d, dish in bot.CATALOG.items() if needs(dish, 1).keys() & REMOVED]
check("есть блюда с сыром/лососем", bool(with_removed))
check("блюда с сыром/лососем не в списке",
      not any(d in with_removed for _, _, d in payload_ids(p57)))
# Десерт завтрака считается на порции завтрака, ужина — на порции ужина:
# при /people 1 30 завтрак 3 порции, ужин 32 — тот же десерт есть утром и нет вечером.
set_people(1, 30)
p_dessert = bot.menu_today_payload()
b_desserts = {d["id"] for d in p_dessert["breakfast"]["dessert"]}
d_desserts = {d["id"] for d in p_dessert["dinner"]["dessert"]}
check("десерт на завтрак (3 порции) есть", bool(b_desserts))
check("десерт ужина на 32 порции пропал", not (b_desserts & d_desserts))
set_people()

# 2. /people 9 11 — список короче, чем при 5/7
set_people(9, 11)
p911 = bot.menu_today_payload()
check(f"при 9/11 список короче: {len(payload_ids(p911))} против {len(payload_ids(p57))}",
      len(payload_ids(p911)) < len(payload_ids(p57)))
check("при 9/11 только подмножество блюд из 5/7", payload_ids(p911) <= payload_ids(p57))
set_people()

# 3. Запрос по HTTP считается по актуальным запасам, /availability больше нет
server = HTTPServer(("127.0.0.1", 0), bot._HealthHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}"


def get(path):
    with urllib.request.urlopen(url + path) as r:
        return r.read().decode("utf-8")


expect("HTTP /menu-today = menu_today_payload", payload_ids(json.loads(get("/menu-today"))), payload_ids(p57))
set_stock({})
empty = json.loads(get("/menu-today"))
expect("пустой склад: все категории пустые, без падения",
       {m: {c: len(v) for c, v in empty[m].items()} for m in ("breakfast", "dinner")},
       {m: {c: 0 for c in cs} for m, cs in bot.MEAL_ORDER.items()})
expect("/availability убран", get("/availability"), "ok")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


def run(job):
    ctx = FakeContext()
    asyncio.run(job(ctx))
    return ctx.bot.sent


# 4. Пустая основная категория: сообщение в 16:30, а не падение
sent = run(bot.alert_empty_main)
expect("пустой склад: предупреждение создателю и главе семьи",
       sorted(c for c, _ in sent), sorted({bot.CREATOR_CHAT_ID, bot.FAMILY_HEAD_CHAT_ID}))
check("текст про завтрак и ужин", all("Нет доступных основных блюд на завтрак и ужин" in t for _, t in sent))
set_stock(TEST_STOCK)
expect("на тестовом стоке основные есть — предупреждения нет", run(bot.alert_empty_main), [])

# 5. Нехватка для выбранного меню приходит только при реальной недостаче
SHORT_PREFIX = "Для выбранного меню не хватает:"


def compile_with(votes, stock):
    for f in tmp.glob("before_compile_*.json"):
        f.unlink()
    set_stock(stock)
    set_people()
    bot.save_json(bot.WEEKLY_SHORTFALL_FILE, {})
    bot.save_today_responses({"1": {"name": "Тест", "breakfast": votes.get("breakfast", {}),
                                    "dinner": votes.get("dinner", {}), "vote_weight": 1}})
    sent = run(bot.compile_and_send_to_cook)
    alerts = [(c, t) for c, t in sent if t.startswith(SHORT_PREFIX)]
    cook = "\n".join(t for c, t in sent if c == bot.COOK_CHAT_ID)
    return alerts, cook


# Два блюда ужина на один продукт: каждому по отдельности хватает, вместе нет.
dinner_9 = {c: bot.available_pool("dinner", c, TEST_STOCK, bot.load_headcount()) for c in bot.MEAL_ORDER["dinner"]}
pair = None
for c1 in bot.MEAL_ORDER["dinner"]:
    for c2 in bot.MEAL_ORDER["dinner"]:
        if c1 >= c2 or pair:
            continue
        for id1, d1 in dinner_9[c1]:
            for id2, d2 in dinner_9[c2]:
                shared = (needs(d1, 9).keys() & needs(d2, 9).keys()) - {"eggs"}
                if shared and not pair:
                    pair = (c1, id1, c2, id2, sorted(shared)[0])
check("нашлась пара блюд ужина с общим продуктом", pair is not None)
c1, id1, c2, id2, shared_key = pair
n1, n2 = needs(bot.CATALOG[id1], 9)[shared_key], needs(bot.CATALOG[id2], 9)[shared_key]
plenty = {k: v * 10 for k, v in ALL_KEYS.items()}
votes = {"dinner": {c1: {"id": id1}, c2: {"id": id2}}}

alerts, cook = compile_with(votes, plenty)
expect("всего с запасом — сообщения о нехватке нет", alerts, [])

tight = dict(plenty, **{shared_key: max(n1, n2)})
set_stock(tight)
check("по отдельности оба блюда в списке",
      {id1, id2} <= {d for _, _, d in payload_ids(bot.menu_today_payload())})
alerts, cook = compile_with(votes, tight)
expect("общий продукт — нехватка создателю и главе семьи",
       sorted(c for c, _ in alerts), sorted({bot.CREATOR_CHAT_ID, bot.FAMILY_HEAD_CHAT_ID}))
ru = bot.stock_name_ru(shared_key)
check(f"в сообщении есть «{ru} — …»: {alerts[:1]}",
      alerts and f"{ru} — " in alerts[0][1] and alerts[0][1].endswith("Купите до готовки. /whatstobuy"))
check("недостача ушла в недельный список", shared_key in bot.load_json(bot.WEEKLY_SHORTFALL_FILE, {}))
check("повару ушли оба блюда", bot.CATALOG[id1]["title"] in cook and bot.CATALOG[id2]["title"] in cook)
check("в сообщении повару нет русского текста о нехватке", SHORT_PREFIX not in cook)

# 6. Голос за блюдо, которое пропало из списка после голосования, считается
gone_id, gone_dish = next((d, dish) for d, dish in bot.pool("dinner", "main")
                          if needs(dish, 1).keys() & REMOVED)
set_stock(TEST_STOCK)
check("блюдо с сыром/лососем не в списке", gone_id not in {d for _, _, d in payload_ids(bot.menu_today_payload())})
alerts, cook = compile_with({"dinner": {"main": {"id": gone_id}}}, TEST_STOCK)
check("пропавшее блюдо всё равно ушло повару", gone_dish["title"] in cook)
check("и по нему пришла нехватка", bool(alerts))

server.shutdown()
shutil.rmtree(tmp, ignore_errors=True)

if errors:
    print("ОШИБКИ:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print(f"OK: 5/7 — {len(payload_ids(p57))} блюд из {full}, 9/11 — {len(payload_ids(p911))}; "
      f"пара на общий продукт: {id1} + {id2} ({shared_key})")
