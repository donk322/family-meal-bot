# -*- coding: utf-8 -*-
"""Список покупок для магазина: что купить и сколько, на английском, по уровням
срочности и разделам магазина. Детерминированно, без ИИ.
build_shopping_messages(stock) -> список сообщений для Telegram (<= 3900 символов)."""
import math
try:
    from .scale_recipe import load_catalog, scaled_ingredients
    from .stock_check import stock_amount, to_stock_unit, CONV
except ImportError:
    from scale_recipe import load_catalog, scaled_ingredients
    from stock_check import stock_amount, to_stock_unit, CONV

# key: (en, ru, раздел, уровень, целевой запас, фасовка, единица, (ярлык, размер одной упаковки) или None)
# Уровень 0 = срочно (основа почти всех блюд), 1 = тоже нужно (основа), 2 = только для отдельных блюд.
# Целевой запас: до какого количества докупаем (None = ровно столько, сколько не хватает для блюда).
ITEMS = {
 "chicken breast":  ("Chicken breast", "куриная грудка", "meat", 1, 2000, 500, "g", None),
 "chicken thigh":   ("Chicken thighs (bone-in, skin-on)", "куриные бёдра с костью и кожей", "meat", 1, 1400, 250, "g", None),
 "bacon":           ("Bacon", "бекон", "meat", 0, 900, 50, "g", None),
 "hard cheese":     ("Cheddar, yellow block (NOT parmesan)", "чеддер, жёлтый кусок (не пармезан)", "dairy", 0, 1000, 250, "g", None),
 "parmesan":        ("Parmesan", "пармезан", "dairy", 1, 400, 50, "g", None),
 "mozzarella":      ("Mozzarella", "моцарелла", "dairy", 1, 400, 100, "g", None),
 "sour cream":      ("Sour cream", "сметана", "dairy", 0, 1000, 250, "ml", None),
 "cream":           ("Cream", "сливки", "dairy", 0, 1000, 250, "ml", None),
 "butter":          ("Butter", "сливочное масло", "dairy", 1, 600, 250, "g", None),
 "cream cheese":    ("Cream cheese", "крем-сыр", "dairy", 1, 500, 250, "g", None),
 "feta":            ("Feta", "фета", "dairy", 1, 250, 100, "g", None),
 "cabbage":         ("White cabbage", "белокочанная капуста", "veg", 1, 3000, 1500, "g", ("head", 1500)),
 "avocado":         ("Avocados", "авокадо", "veg", 1, 1000, 250, "g", None),
 "garlic":          ("Garlic", "чеснок", "veg", 0, 120, 40, "g", ("head", 40)),
 "zucchini":        ("Zucchini", "цукини", "veg", 1, 1000, 250, "g", None),
 "spinach":         ("Baby spinach", "шпинат", "veg", 1, 250, 125, "g", None),
 "romaine lettuce": ("Romaine lettuce", "салат романо", "veg", 1, 900, 300, "g", ("head", 300)),
 "shirataki noodles": ("Shirataki noodles", "лапша ширатаки", "pantry", 1, 1400, 200, "g", ("pack", 200)),
 "mushroom stock cubes":   ("Mushroom stock cubes", "грибные бульонные кубики", "pantry", 0, 6, 6, "pcs", ("box", 6)),
 "vegetable stock cubes":  ("Vegetable stock cubes", "овощные бульонные кубики", "pantry", 1, 6, 6, "pcs", ("box", 6)),
 "chicken stock cubes":    ("Chicken stock cubes", "куриные бульонные кубики", "pantry", 1, 3, 6, "pcs", ("box", 6)),
 "pickled cucumbers": ("Pickled cucumbers", "солёные огурцы", "pantry", 1, 400, 350, "g", ("jar", 350)),
 "olives":          ("Pitted black olives", "чёрные маслины без косточек", "pantry", 1, 200, 200, "g", ("jar", 200)),
 # --- уровень 2 ---
 "sausages":        ("Sausages", "колбаски", "meat", 2, None, 100, "g", None),
 "pork mince":      ("Pork mince", "свиной фарш", "meat", 2, None, 250, "g", None),
 "pork shoulder":   ("Pork shoulder, boneless", "свиная лопатка без кости", "meat", 2, None, 500, "g", None),
 "pork chops":      ("Boneless pork chops", "свиные отбивные без кости", "meat", 2, None, 500, "g", None),
 "chicken wings":   ("Chicken wings", "куриные крылья", "meat", 2, None, 500, "g", None),
 "oxtail":          ("Oxtail", "бычьи хвосты", "meat", 2, None, 500, "g", None),
 "smoked salmon":   ("Smoked salmon, sliced", "копчёный лосось, нарезка", "meat", 2, None, 100, "g", None),
 "broccoli":        ("Broccoli", "брокколи", "veg", 2, None, 100, "g", None),
 "brussels sprouts": ("Brussels sprouts", "брюссельская капуста", "veg", 2, None, 250, "g", None),
 "peas in pods":    ("Sugar snap peas", "горох в стручках", "veg", 2, None, 150, "g", None),
 "raspberries":     ("Raspberries (or any berries)", "малина (или любые ягоды)", "veg", 2, None, 150, "g", None),
 "ginger":          ("Fresh ginger", "имбирь", "veg", 2, None, 30, "g", None),
 "bean sprouts":    ("Bean sprouts", "ростки фасоли", "veg", 2, None, 150, "g", None),
 "basil":           ("Fresh basil", "свежий базилик", "veg", 2, None, 1, "pcs", ("bunch", 1)),
 "jalapeno":        ("Jalapeño chili", "халапеньо", "veg", 2, None, 1, "pcs", None),
 "gluten-free english muffins": ("Gluten-free English muffins", "безглютеновые английские маффины", "pantry", 2, None, 4, "pcs", ("pack", 4)),
 "gluten-free flatbread": ("Gluten-free flatbreads", "безглютеновые лепёшки", "pantry", 2, None, 4, "pcs", ("pack", 4)),
 "almond flour":    ("Almond flour", "миндальная мука", "pantry", 2, None, 200, "g", None),
 "pork rinds":      ("Pork rinds", "свиные шкварки", "pantry", 2, None, 60, "g", None),
 "sesame oil":      ("Toasted sesame oil", "кунжутное масло", "pantry", 2, None, 250, "ml", ("bottle", 250)),
 "sesame seeds":    ("Sesame seeds", "кунжут", "pantry", 2, None, 50, "g", None),
 "cinnamon sticks": ("Cinnamon sticks", "корица палочками", "pantry", 2, None, 6, "pcs", None),
 "star anise":      ("Star anise", "бадьян", "pantry", 2, None, 4, "pcs", None),
 "cloves":          ("Whole cloves (spice)", "гвоздика (специя)", "pantry", 2, None, 12, "pcs", None),
 "cardamom pods":   ("Cardamom pods", "кардамон в коробочках", "pantry", 2, None, 5, "pcs", None),
 "fish sauce":      ("Fish sauce (gluten-free)", "рыбный соус без глютена", "pantry", 2, None, 250, "ml", ("bottle", 250)),
}
SECTIONS = [("meat", "🥩 MEAT & FISH"), ("dairy", "🥛 DAIRY"), ("veg", "🥬 VEGETABLES"), ("pantry", "🫙 PANTRY")]
TIERS = [(0, "🔴 URGENT — BUY TODAY"), (1, "🟠 ALSO NEEDED"), (2, "⚪ OPTIONAL")]

def _stock_unit(key, catalog):
    """Единица, в которой склад считает продукт (после CONV): g, ml или pcs.
    Нужна для продуктов, которых нет в ITEMS (яйца, молоко, хлеб...)."""
    counts = {}
    for d in catalog.values():
        for k, unit, _, staple in scaled_ingredients(d, 1):
            if k == key and not staple:
                u = CONV.get(k, {}).get(unit, (unit,))[0]
                counts[u] = counts.get(u, 0) + 1
    return max(counts, key=counts.get) if counts else "g"

def _ceil_to(x, pack):
    return int(math.ceil(x / float(pack) - 1e-9) * pack) if pack else x

def _fmt(amount, unit, label):
    if label:
        name, size = label
        n = int(math.ceil(amount / float(size) - 1e-9))
        plural = "es" if name == "bunch" else "s"
        each = " each" if n > 1 else ""
        extra = f" (about {int(size)} {unit}{each})" if unit in ("g", "ml") and size > 1 and name in ("head", "pack", "jar", "bottle") and unit != "pcs" else ""
        return f"{n} {name}{plural if n > 1 else ''}{extra}"
    if unit == "g":
        return f"{amount/1000:g} kg" if amount >= 1000 else f"{int(amount)} g"
    if unit == "ml":
        return f"{amount/1000:g} L" if amount >= 1000 else f"{int(amount)} ml"
    return f"{int(amount)} pcs"

def compute_purchases(stock, catalog=None):
    """-> список словарей по продуктам, которые нужно купить, и словарь недостач по блюдам."""
    cat = catalog or load_catalog()
    short = {}            # key -> максимальная недостача для одного блюда
    dishes_short = {}     # key -> [блюда, которым не хватает]
    for did, d in cat.items():
        for key, unit, amt, staple in scaled_ingredients(d, d["base_portions"]):
            if staple: continue
            need = to_stock_unit(key, unit, amt)
            have = stock_amount(stock, key) or 0.0
            if need > have * 1.0 + 1e-9:
                short[key] = max(short.get(key, 0.0), need - have)
                dishes_short.setdefault(key, []).append(d["title_ru"])
    out = []
    for key, sh in short.items():
        info = ITEMS.get(key)
        have = stock_amount(stock, key) or 0.0
        if info:
            en, ru, section, tier, target, pack, unit, label = info
            want = sh if target is None else max(sh, target - have)
            amount = _ceil_to(want, pack)
        else:       # продукт без записи в таблице: показываем как есть, в уровне 2
            en, ru, section, tier, unit, label = key.capitalize(), "", "pantry", 2, _stock_unit(key, cat), None
            amount = math.ceil(sh)
        out.append({"key": key, "en": en, "ru": ru, "section": section, "tier": tier, "amount": amount,
                    "unit": unit, "label": label, "dishes": dishes_short.get(key, [])})
    return out

def _pack_messages(blocks, limit=3800):
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + 1 + len(b) > limit:
            msgs.append(cur); cur = b
        else:
            cur = (cur + "\n" + b) if cur else b
    if cur: msgs.append(cur)
    return msgs

def build_shopping_messages(stock, catalog=None):
    """Для покупателя: только что купить и сколько, на английском. Три уровня
    (срочно / тоже нужно / по желанию), внутри — разделы магазина, в конце —
    строки /addstock с рекомендованными количествами."""
    cat = catalog or load_catalog()
    purchases = compute_purchases(stock, cat)
    if not purchases:
        return ["🛒 SHOPPING LIST\nEverything for the menu is in stock. Nothing to buy."]
    blocks = ["🛒 SHOPPING LIST"]
    for tier, tier_title in TIERS:
        items = [p for p in purchases if p["tier"] == tier]
        if not items:
            continue
        blocks.append("\n" + tier_title)
        for sec, title in SECTIONS:
            rows = [p for p in items if p["section"] == sec]
            if not rows:
                continue
            blocks.append("\n" + title)
            for p in sorted(rows, key=lambda r: (-len(r["dishes"]), r["en"])):
                blocks.append(f"☐ {p['en']} — {_fmt(p['amount'], p['unit'], p['label'])}")
    msgs = _pack_messages(blocks)
    # блок /addstock с рекомендованными количествами
    lines = [f"/addstock {p['key']} {p['amount']} {p['unit']}"
             for p in sorted(purchases, key=lambda r: (r["tier"], r["key"]))]
    note = ("➕ AFTER SHOPPING: copy the lines below, DELETE the lines for things you did NOT buy "
            "(or change the amount), and send them to the bot. The amounts are the recommended ones.")
    chunks = [lines[i:i + 25] for i in range(0, len(lines), 25)]
    msgs.append(note)
    msgs += ["\n".join(c) for c in chunks]
    return msgs
