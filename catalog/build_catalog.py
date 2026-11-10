# -*- coding: utf-8 -*-
"""Собирает dishes_fixed.json из текстовых файлов рецептов."""
import re, json, sys, os
from fractions import Fraction

_HERE = os.path.dirname(os.path.abspath(__file__))
_REL = os.path.join(_HERE, "..", "recipes_src")
SRC = os.environ.get("RECIPES_DIR") or (_REL if os.path.isdir(_REL) else "/mnt/user-data/outputs")
FILES = [
    "breakfast_recipes_batch1.txt", "breakfast_recipes_batch2.txt", "breakfast_recipes_batch3.txt",
    "main_recipes_batch1.txt", "main_recipes_batch2.txt", "main_recipes_batch3.txt",
    "soup_recipes_batch1.txt", "soup_recipes_batch2.txt",
    "side_recipes.txt", "salad_recipes.txt", "dessert_recipes.txt",
]

# --- категории по подстроке английского названия -------------------------------------------
CATEGORY_RULES = [
    ("steak sandwich", "breakfast_main"), ("beef and pepper breakfast skillet", "breakfast_main"),
    ("greek salmon", "breakfast_main"), ("cheeseburger cabbage", "breakfast_main"),
    ("pizza frittata", "breakfast_main"), ("loaded cauliflower breakfast bake", "breakfast_main"),
    ("sausage and broccoli", "breakfast_main"), ("pulled pork eggs benedict", "breakfast_main"),
    ("breakfast burritos", "breakfast_main"), ("cream cheese pancakes", "breakfast_main"),
    ("cauliflower \"fried rice\"", "breakfast_extra"), ("french creamy eggs", "breakfast_extra"),
    ("deviled eggs", "breakfast_extra"), ("frico fried egg", "breakfast_extra"),
    ("smoked salmon and cream cheese", "breakfast_extra"),
    ("shrimp and shirataki", "main"), ("chicken tetrazzini", "main"), ("stuffed peppers", "main"),
    ("beef stroganoff", "main"), ("parmesan-crusted chicken", "main"), ("chicken wings", "main"),
    ("cheese meatballs", "main"), ("cheese taco cups", "main"), ("bacon-wrapped meatloaf", "main"),
    ("stuffed cabbage rolls", "main"), ("pork chops", "main"), ("beef and mushroom stew", "main"),
    ("bacon chicken thighs", "main"), ("oxtail", "main"), ("garlic butter beef steak slices", "main"),
    ("shchi", "soup"), ("tomato lentil soup", "soup"), ("chicken soup", "soup"),
    ("meatball soup", "soup"), ("mushroom soup", "soup"), ("borscht", "soup"), ("pho", "soup"),
    ("mac and cheese", "side"), ("cauliflower tots", "side"), ("creamed brussels", "side"),
    ("garlic parmesan zucchini", "side"), ("cauliflower steaks", "side"), ("sesame peanut noodles", "side"),
    ("cobb salad", "salad"), ("greek salad", "salad"), ("caesar salad", "salad"), ("cheese salad", "salad"),
    ("meat salad", "salad"), ("beetroot salad", "salad"), ("prawn and avocado salad", "salad"),
    ("berries with sour cream", "dessert"), ("baked berry custard", "dessert"),
    ("flour-free crepes", "dessert"), ("peanut butter and honey balls", "dessert"), ("warm cherries", "dessert"),
]

NAMES_RU = [
 ("steak sandwich", "Сэндвич со стейком, бри и яйцами", "Ломтики стейка, мягкий скрэмбл, карамелизованный лук и грибы на безглютеновом хлебе"),
 ("beef and pepper breakfast skillet", "Сковорода с говяжьим фаршем, перцем и сыром", "Фарш с болгарским перцем и яйцами под расплавленным сыром"),
 ("greek salmon", "Лосось по-гречески с фетой", "Запечённый лосось с помидорами, огурцом, фетой и маслинами"),
 ("cheeseburger cabbage", "Чизбургеры в капустных листьях", "Котлета с сыром, луком и огурцом в капустном листе, соус"),
 ("pizza frittata", "Фриттата «пицца»", "Запечённые яйца с томатным соусом, шпинатом, моцареллой и салями"),
 ("loaded cauliflower breakfast bake", "Запеканка из цветной капусты с беконом", "Цветная капуста, бекон, чеддер и яйца в духовке"),
 ("sausage and broccoli", "Яичная запеканка с колбасой и брокколи", "Колбаски, брокколи и сыр, залитые яйцами со сливками"),
 ("pulled pork eggs benedict", "Яйца бенедикт с рваной свининой", "Томлёная свинина, яйцо пашот и голландский соус на безглютеновых маффинах"),
 ("breakfast burritos", "Буррито с беконом, яйцом и сыром", "Скрэмбл, хрустящий бекон и сыр в безглютеновой лепёшке"),
 ("cream cheese pancakes", "Панкейки на крем-сыре без муки", "Тонкие панкейки из миндальной муки, крем-сыра и яиц"),
 ("cauliflower \"fried rice\"", "«Рис» из цветной капусты с яйцом", "Лёгкий гарнир: цветная капуста, яйцо, морковь и горошек"),
 ("french creamy eggs", "Яйца по-французски", "Очень нежный кремовый скрэмбл на сливочном масле"),
 ("deviled eggs", "Фаршированные яйца", "Варёные яйца с майонезной начинкой и паприкой, холодное блюдо"),
 ("frico fried egg", "Тостады с яйцом на сырной основе", "Хрустящий сырный диск с яйцом и свежей томатной сальсой"),
 ("smoked salmon and cream cheese", "Рулеты с копчёным лососем и крем-сыром", "Холодные рулеты с солёными огурчиками и красным луком"),
 ("shrimp and shirataki", "Креветки с лапшой ширатаки (постное)", "Обжарка с капустой и морковью, соус тамари, без мяса"),
 ("chicken tetrazzini", "Тетраццини с курицей на ширатаки", "Курица и лапша в сливочно-сырном соусе, запечённые"),
 ("stuffed peppers", "Фаршированные перцы с беконом", "Перец с говяжьим фаршем в томатном соусе под сыром"),
 ("beef stroganoff", "Бефстроганов из фарша с грибами", "Говядина и грибы в сметанном соусе"),
 ("parmesan-crusted chicken", "Курица в пармезановой корочке", "Котлеты из грудки в сыре с томатом и моцареллой"),
 ("chicken wings", "Куриные крылья в духовке", "Хрустящие крылья с острым майонезным соусом"),
 ("cheese meatballs", "Сырные фрикадельки в томатном соусе", "Говяжьи шарики с чеддером и пармезаном"),
 ("cheese taco cups", "Сырные тако-корзинки с говядиной", "Хрустящие сырные чашечки с пряным фаршем, авокадо и сметаной"),
 ("bacon-wrapped meatloaf", "Мясной рулет с беконом и сыром", "Запечённый говяжий рулет под хрустящим беконом"),
 ("stuffed cabbage rolls", "Голубцы в томатном соусе", "Говядина со свининой и цветной капустой вместо риса"),
 ("pork chops", "Свиные отбивные в грибном соусе", "Отбивные с шампиньонами, пармезаном и шпинатом"),
 ("beef and mushroom stew", "Говяжье рагу с грибами", "Томлёная говядина с овощами и грибами"),
 ("bacon chicken thighs", "Куриные бёдра с беконом в чесночном соусе", "Бёдра в сливочном соусе с грибами и пармезаном"),
 ("oxtail", "Тушёные бычьи хвосты", "Долго томлёное мясо на кости, очень мягкое"),
 ("garlic butter beef steak slices", "Стейк ломтиками с чесночным маслом", "Маринованные кусочки говядины, быстро обжаренные"),
 ("shchi (cabbage soup)", "Щи с говядиной", "Классические щи со свежей капустой на говяжьем бульоне"),
 ("lenten shchi", "Постные щи (среда)", "Щи с капустой и картошкой без мяса"),
 ("tomato lentil soup", "Томатный суп с чечевицей и грибами", "Густой суп из чечевицы, помидоров и грибов"),
 ("chicken soup", "Куриный суп с лапшой ширатаки", "Лёгкий куриный суп с овощами"),
 ("meatball soup", "Суп с фрикадельками", "Говяжьи фрикадельки, морковь и цветная капуста"),
 ("mushroom soup", "Грибной суп (пятница, постный)", "Прозрачный суп из грибов с луком и морковью"),
 ("borscht", "Борщ с говядиной", "Классический борщ со свёклой и говядиной"),
 ("pho", "Фо с говядиной и лапшой ширатаки", "Пряный бульон и тонкие ломтики говядины"),
 ("mac and cheese", "Цветная капуста в сырном соусе", "Кето мак-н-чиз: запечённая цветная капуста с сырной корочкой"),
 ("cauliflower tots", "Картошечки из цветной капусты", "Золотистые шарики из цветной капусты с сыром, острый соус"),
 ("creamed brussels", "Брюссельская капуста в сливках", "Запечённая в сливочном соусе с сыром"),
 ("garlic parmesan zucchini", "Цукини с чесноком и пармезаном", "Обжаренные кружки цукини"),
 ("cauliflower steaks", "Стейки из цветной капусты с сыром и беконом", "Запечённая цветная капуста, чеддер и бекон"),
 ("sesame peanut noodles", "Кунжутная лапша с арахисовой пастой", "Лапша ширатаки в ореховом соусе"),
 ("cobb salad", "Салат Кобб", "Курица, бекон, яйцо, авокадо и помидоры на листьях салата"),
 ("greek salad", "Греческий салат", "Огурцы, помидоры, красный лук, фета и маслины"),
 ("caesar salad", "Салат Цезарь с курицей", "Курица, романо, пармезан и безглютеновые сухарики"),
 ("cheese salad", "Сырный салат с яйцами и чесноком", "Тёртый сыр, яйца, чеснок и майонез"),
 ("meat salad", "Мясной салат с авокадо", "Курица, яйца, огурцы и авокадо вместо картошки"),
 ("beetroot salad", "Свекольный салат с чесноком", "Варёная свёкла с майонезом и чесноком"),
 ("prawn and avocado salad", "Салат с креветками и авокадо", "Креветки, авокадо, огурец и лайм"),
 ("berries with sour cream", "Ягоды со сметаной и мёдом", "Свежие ягоды, холодная сметана и мёд"),
 ("baked berry custard", "Запечённый ягодный крем", "Яичный крем с малиной, запечённый в духовке"),
 ("flour-free crepes", "Блинчики без муки с ягодами", "Тонкие блинчики на арроуруте с ягодами и сметаной"),
 ("peanut butter and honey balls", "Шарики из арахисовой пасты с мёдом", "Холодные шарики без выпечки"),
 ("warm cherries", "Тёплая вишня с ванилью", "Вишня с мёдом и ванилью, холодная сметана"),
]

BREAKFAST_DESSERTS = ("berries with sour cream", "baked berry custard", "warm cherries")

STAPLES = {"salt", "black pepper", "vegetable oil", "olive oil", "paprika", "dried oregano",
           "chili flakes", "vinegar", "mustard", "water", "bay leaves", "bay leaf", "black peppercorns"}

# --- имя ингредиента в рецепте -> ключ в запасах бота --------------------------------------
NAME_MAP = {
    "lean ground beef": "ground beef", "ground beef": "ground beef", "beef steak": "beef steak",
    "beef (stewing pieces)": "beef", "beef (stewing pieces)": "beef", "flank steak (or any lean beef steak)": "beef steak",
    "beef": "beef", "chicken breast": "chicken breast", "chicken thighs with skin": "chicken thigh",
    "chicken thighs (bone-in, with skin)": "chicken thigh", "chicken wings (split into pieces)": "chicken wings",
    "pork shoulder (boneless)": "pork shoulder", "boneless pork chops": "pork chops", "pork mince": "pork mince",
    "oxtail (cut into pieces of 5 cm)": "oxtail", "sausages": "sausages", "sausages (the pre-cooked kind)": "sausages",
    "salami": "salami", "bacon": "bacon", "eggs": "eggs", "egg": "eggs", "egg yolks": "eggs",
    "hard cheese (yellow cheddar block, not parmesan)": "hard cheese", "parmesan": "parmesan",
    "mozzarella": "mozzarella", "feta": "feta", "brie": "brie", "cream cheese": "cream cheese",
    "cream": "cream", "cold cream": "cream", "sour cream": "sour cream", "milk": "milk", "butter": "butter",
    "mayonnaise": "mayonnaise", "napoletana tomato sauce": "napoletana tomato sauce", "tomato paste": "tomato paste",
    "tomatoes": "tomato", "cucumbers": "cucumber", "cucumber": "cucumber", "pickled cucumbers": "pickled cucumbers",
    "bell peppers": "bell pepper", "bell pepper": "bell pepper", "red bell pepper": "bell pepper",
    "onion": "onion", "onions": "onion", "red onion": "red onion", "red onions": "red onion",
    "garlic": "garlic", "carrots": "carrot", "carrot": "carrot", "cauliflower": "cauliflower", "cabbage": "cabbage",
    "mushrooms": "mushrooms", "mushrooms (any mix)": "mushrooms", "brown mushrooms": "mushrooms",
    "beetroot": "beetroot", "zucchini": "zucchini", "celery": "celery", "potatoes": "potato",
    "avocados": "avocado", "avocado": "avocado", "limes": "lime", "lime": "lime", "lime juice": "lime",
    "lime zest": "lime", "spinach": "spinach", "baby spinach": "spinach", "broccoli": "broccoli",
    "romaine lettuce": "romaine lettuce", "brussels sprouts": "brussels sprouts", "cherries": "cherries",
    "berries": "berries", "berries (raspberries, strawberries, blueberries or a mix)": "berries",
    "raspberries": "raspberries", "honey": "honey", "vanilla extract": "vanilla extract", "arrowroot": "arrowroot",
    "peanut butter": "peanut butter", "peanut butter (smooth, natural)": "peanut butter",
    "peanut butter (smooth, natural)": "peanut butter",
    "tamari sauce": "tamari sauce", "coconut aminos": "coconut aminos", "shirataki noodles": "shirataki noodles",
    "shirataki noodles (fettuccine or angel hair)": "shirataki noodles",
    "prawns": "prawns", "salmon fillet": "salmon fillet", "smoked salmon": "smoked salmon",
    "smoked salmon, sliced": "smoked salmon", "lightly salted salmon": "lightly salted salmon",
    "gluten-free bread": "gluten-free bread", "gluten-free flatbreads (large, soft)": "gluten-free flatbread",
    "gluten-free english muffins": "gluten-free english muffins", "brown lentils": "brown lentils",
    "chicken stock cube": "chicken stock cubes", "mushroom stock cube": "mushroom stock cubes",
    "mushroom stock cubes": "mushroom stock cubes", "vegetable stock cubes": "vegetable stock cubes",
    "beef stock cube": "mushroom stock cubes", "beef stock cube (or mushroom stock cube)": "mushroom stock cubes",
    "dill": "dill", "basil": "basil", "olives": "olives", "pitted black olives": "olives",
    "almonds": "almonds", "almond flour": "almond flour", "pork rinds (crushed)": "pork rinds",
    "pork rinds (crushed finely)": "pork rinds", "sesame seeds": "sesame seeds", "toasted sesame oil": "sesame oil",
    "fresh ginger": "ginger", "fish sauce": "fish sauce", "bean sprouts": "bean sprouts", "jalapeño chili": "jalapeno",
    "cinnamon sticks": "cinnamon sticks", "star anise": "star anise", "cloves": "cloves", "cardamom pods": "cardamom pods",
    "cauliflower steaks": "cauliflower", "ginger": "ginger",
}

def norm(s):
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def strip_emoji(s):
    return re.sub(r"^[^\w\"“(]+", "", s).strip()

UNIT_ALIASES = {
    "g": "g", "kg": "kg", "ml": "ml", "l": "l",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "slice": "pcs", "slices": "pcs", "clove": "pcs", "cloves": "pcs", "piece": "pcs", "pieces": "pcs",
}

NUM = r"(\d+\s+\d/\d|\d/\d|\d+(?:[.,]\d+)?)"

def to_float(tok):
    tok = tok.replace(",", ".").strip()
    total = Fraction(0)
    for part in tok.split():
        total += Fraction(part) if "/" in part else Fraction(part)
    return float(total)

def parse_amount(text):
    """Возвращает (amount, unit, rest) из начала строки вида '1 1/2 teaspoons (note)'."""
    t = text.strip()
    t = re.sub(r"^about\s+", "", t, flags=re.I)
    m = re.match(NUM + r"\s*(kg|g|ml|l|tablespoons?|teaspoons?|slices?|cloves?|pieces?)?\b", t, re.I)
    if not m:
        return None
    amount = to_float(m.group(1))
    unit = UNIT_ALIASES.get((m.group(2) or "").lower(), "pcs")
    return amount, unit, t[m.end():].strip()

def parse_dish(block):
    lines = [l.rstrip() for l in block.split("\n")]
    # заголовок
    title_line = None
    for i, l in enumerate(lines):
        if l.startswith("━"):
            continue
        if l.strip():
            title_line = l.strip(); idx = i; break
    m = re.match(r"(.+?)\s+—\s+serves\s+(\d+)", title_line)
    if not m:
        raise ValueError("нет 'serves N' в заголовке: " + title_line)
    head, serves = m.group(1), int(m.group(2))
    emoji = re.match(r"^([^\w\"“(]+)", head)
    emoji = emoji.group(1).strip() if emoji else ""
    name = strip_emoji(head)
    desc, time_line, start_off, minutes = [], None, None, None
    body = lines[idx + 1:]
    j = 0
    while j < len(body) and not body[j].startswith("⏱"):
        if body[j].strip() and not body[j].startswith("━"):
            desc.append(body[j].strip())
        j += 1
    if j < len(body):
        time_line = body[j].strip()
        mm = re.search(r"Start at T minus (\d+) min", time_line)
        start_off = int(mm.group(1)) if mm else None
        hm = re.search(r"(\d+)\s+hours?(?:\s+(\d+)\s+minutes)?", time_line)
        mn = re.search(r"(\d+)\s+minutes", time_line)
        if hm:
            minutes = int(hm.group(1)) * 60 + int(hm.group(2) or 0)
        elif mn:
            minutes = int(mn.group(1))
        j += 1
    ingredients, equipment = [], []
    steps, sections, ready, warns = [], [], [], []
    mode, cur_section = None, None
    while j < len(body):
        l = body[j].strip(); j += 1
        if not l or l.startswith("━"):
            continue
        if l == "YOU NEED":
            mode = "need"; continue
        if mode == "need" and l.startswith("•"):
            item = l[1:].strip()
            if ":" in item:
                nm, rest = item.split(":", 1)
                pa = parse_amount(rest) if not re.match(r"^(For the dip|Dressing|For the)", nm) else None
                if pa:
                    amount, unit, note = pa
                    ingredients.append({"text": item, "name": nm.strip(), "amount": amount, "unit": unit, "note": note})
                else:
                    ingredients.append({"text": item, "name": nm.strip(), "amount": None, "unit": None, "note": rest.strip(), "multi": True})
                    for part in re.split(r",\s+(?![^()]*\))", rest.strip()):
                        mm = re.match(r"^(.*?)\s+" + NUM + r"\s*(kg|g|ml|l|tablespoons?|teaspoons?)?\b", part.strip(), re.I)
                        if mm:
                            ingredients.append({"text": None, "name": mm.group(1).strip(), "amount": to_float(mm.group(2)),
                                                "unit": UNIT_ALIASES.get((mm.group(3) or "").lower(), "pcs"), "note": "", "stock_only": True})
            else:
                equipment.append(item)
            continue
        if re.match(r"^\d+\.\s", l):
            mode = "steps"
            steps.append({"n": int(l.split(".")[0]), "text": l.split(".", 1)[1].strip(), "section": cur_section})
            continue
        if l.startswith("✅"):
            ready.append(l); continue
        if l.startswith("⚠️"):
            warns.append(l); continue
        if mode in ("steps", "need") and l.isupper() or re.match(r"^[A-Z][A-Z0-9 ,'’()\-/]+( \(.*\))?$", l):
            cur_section = l; sections.append(l); mode = "steps"; continue
        if mode == "need" and not l.startswith("•"):
            # строка-пояснение внутри блока (ожидаем редко)
            desc.append(l); continue
        desc.append(l)
    return {"title": name, "emoji": emoji, "base_portions": serves, "description": " ".join(desc),
            "time_line": time_line, "duration_min": minutes, "start_offset_min": start_off,
            "ingredients": ingredients, "equipment": equipment, "steps": steps,
            "ready_when": ready, "warnings": warns}

def slug(title):
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60]

def split_blocks(text):
    parts = re.split(r"(?m)^━{5,}\n(?=[^\n━]+\n━{5,})", text)
    blocks = []
    for p in parts:
        p = p.strip()
        if not p or "serves" not in p.split("\n")[0]:
            continue
        # убираем завершающую линию ━ после заголовка
        p = re.sub(r"\n━{5,}\n", "\n", p, count=1)
        blocks.append(p)
    return blocks

def build():
    dishes, problems = {}, []
    for fn in FILES:
        path = os.path.join(SRC, fn)
        if not os.path.exists(path):
            problems.append("нет файла " + fn); continue
        text = open(path, encoding="utf-8").read()
        for b in split_blocks(text):
            try:
                d = parse_dish(b)
            except Exception as e:
                problems.append(f"{fn}: {e}"); continue
            did = slug(d["title"])
            cat = next((c for key, c in CATEGORY_RULES if key in d["title"].lower()), None)
            if not cat:
                problems.append("нет категории: " + d["title"]); cat = "unknown"
            d["id"] = did; d["category"] = cat; d["source_file"] = fn
            ru = next(((n, ds) for key, n, ds in NAMES_RU if key in d["title"].lower()), None)
            if not ru: problems.append("нет русского названия: " + d["title"]); ru = (d["title"], "")
            d["title_ru"], d["description_ru"] = ru
            d["breakfast_ok"] = cat in ("breakfast_main", "breakfast_extra") or any(k in d["title"].lower() for k in BREAKFAST_DESSERTS)
            # структурированные ингредиенты для запасов
            for ing in d["ingredients"]:
                key = norm(re.sub(r"\s*\(.*?\)", "", ing["name"]))
                full = norm(ing["name"])
                canon = NAME_MAP.get(full) or NAME_MAP.get(key)
                ing["stock_key"] = canon or key
                ing["mapped"] = bool(canon) or key in STAPLES
                ing["staple"] = (canon or key) in STAPLES
            dishes[did] = d
    return dishes, problems

if __name__ == "__main__":
    dishes, problems = build()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dishes_fixed.json")
    json.dump(dishes, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from collections import Counter
    print("Блюд:", len(dishes)); print(Counter(d["category"] for d in dishes.values()))
    print("Проблемы разбора:", problems or "нет")
    bad = []
    for d in dishes.values():
        if not d["ingredients"]: bad.append(d["id"] + ": нет ингредиентов")
        if len(d["steps"]) < 5: bad.append(d["id"] + ": мало шагов")
        if d["start_offset_min"] is None: bad.append(d["id"] + ": нет Start at T minus")
        if not d["ready_when"]: bad.append(d["id"] + ": нет READY WHEN")
        ns = [s["n"] for s in d["steps"]]
        if ns != list(range(1, len(ns) + 1)): bad.append(d["id"] + ": сбита нумерация шагов " + str(ns[:8]))
    print("Структурные замечания:", bad or "нет")
    unm = sorted({ing["stock_key"] for d in dishes.values() for ing in d["ingredients"] if not ing["mapped"] and not ing.get("multi")})
    print("Ингредиенты без явного соответствия (используются как есть):", unm)
