# -*- coding: utf-8 -*-
"""Проверка, хватает ли запасов на блюдо, и пересчёт единиц между рецептом и складом."""
try:
    from .scale_recipe import scaled_ingredients, load_catalog
except ImportError:
    from scale_recipe import scaled_ingredients, load_catalog

# Ключи запасов, которые складываются при проверке (разные виды одного продукта).
STOCK_GROUPS = {
    "mushrooms": ["mushrooms", "chopped mushrooms", "oyster mushrooms", "shiitake mushrooms", "shimeji mushrooms"],
    "berries": ["berries", "raspberries"],
    "raspberries": ["raspberries", "berries"],   # малину в рецепте может покрыть смесь ягод
    "bell pepper": ["bell pepper", "mixed peppers"],
    "smoked salmon": ["smoked salmon", "lightly salted salmon"],
}
# Как переводить единицу рецепта в единицу склада для продуктов, где они различаются.
# значение: (единица склада, коэффициент: сколько единиц склада в 1 единице рецепта)
CONV = {
    "lime":        {"pcs": ("g", 60), "tbsp": ("g", 30), "tsp": ("g", 10), "ml": ("g", 2)},
    "cauliflower": {"g": ("pcs", 1 / 800.0)},
    "cabbage":     {"pcs": ("g", 1500)},
    "celery":      {"g": ("pcs", 1 / 400.0)},
    "arrowroot":   {"tbsp": ("g", 8), "tsp": ("g", 3)},
    "vanilla extract": {"tsp": ("ml", 5)},
    "tamari sauce": {"tbsp": ("ml", 15), "tsp": ("ml", 5)},
    "honey":       {"tbsp": ("g", 20), "tsp": ("g", 7)},
    "mayonnaise":  {"tbsp": ("g", 15)},
    # Добавлено при переводе бота на каталог: в рецептах штуки/ложки, на складе граммы/мл.
    "bell pepper":       {"pcs": ("g", 150)},   # "Bell peppers: 8 (about 1200 g)"
    "red onion":         {"pcs": ("g", 100)},   # "Red onions: 2 small (about 200 g)"
    "pickled cucumbers": {"pcs": ("g", 50)},    # "Pickled cucumbers: 3 small", оценка
    "ginger":            {"pcs": ("g", 5)},     # "Fresh ginger: 5 cm piece" -> 1 cm ~ 5 g, оценка
    "sesame seeds":      {"tbsp": ("g", 9), "tsp": ("g", 3)},
}

def stock_amount(stock, key):
    keys = STOCK_GROUPS.get(key, [key])
    total, found = 0.0, False
    for k in keys:
        item = stock.get(k)
        if item is None:
            continue
        found = True
        total += float(item["amount"] if isinstance(item, dict) else item)
    return total if found else None

def to_stock_unit(key, unit, amount):
    conv = CONV.get(key, {}).get(unit)
    if conv:
        return amount * conv[1]
    return amount

# Допуск: продукта достаточно, если есть не меньше 90% нужного ИЛИ (для граммов
# и миллилитров при потребности до 100) нехватка не больше 5 г/мл. Продукта нет
# в запасах вообще или осталось 0 — его не хватает всегда, как бы мало ни было нужно.
ENOUGH_SHARE = 0.9
SMALL_NEED = 100
SMALL_GAP = 5


def stock_unit(key, unit):
    """Единица склада для единицы рецепта (после CONV)."""
    return CONV.get(key, {}).get(unit, (unit,))[0]


def is_enough(need, have, unit):
    """need и have — в единице склада unit (g, ml, pcs). have=None — продукта нет."""
    if have is None or have <= 0:
        return need <= 0
    if have + 1e-9 >= need * ENOUGH_SHARE:
        return True
    return unit in ("g", "ml") and need <= SMALL_NEED and need - have <= SMALL_GAP + 1e-9


def check_dish(dish, portions, stock):
    """-> (ok, [(stock_key, нужно, есть)]) для ингредиентов, которых не хватает
    (с допуском is_enough)."""
    missing = []
    for key, unit, amount, staple in scaled_ingredients(dish, portions):
        if staple:
            continue
        need = to_stock_unit(key, unit, amount)
        have = stock_amount(stock, key)
        if not is_enough(need, have, stock_unit(key, unit)):
            missing.append((key, round(need, 1), 0 if have is None else round(have, 1)))
    return (not missing), missing

def subtract_for_dish(dish, portions, stock):
    """Списывает запасы после готовки. Возвращает изменённый словарь (копию)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in stock.items()}
    for key, unit, amount, staple in scaled_ingredients(dish, portions):
        if staple:
            continue
        need = to_stock_unit(key, unit, amount)
        keys = [k for k in STOCK_GROUPS.get(key, [key]) if k in out]
        for k in keys:                        # списываем по порядку, пока не покроем
            cur = out[k]["amount"] if isinstance(out[k], dict) else out[k]
            take = min(cur, need); need -= take
            if isinstance(out[k], dict): out[k]["amount"] = cur - take
            else: out[k] = cur - take
            if need <= 1e-9: break
    return out
