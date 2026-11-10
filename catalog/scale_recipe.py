# -*- coding: utf-8 -*-
"""Пересчёт и вывод рецептов из dishes_fixed.json на нужное число порций.
Никакого ИИ: только арифметика и шаблоны. Используется в compile_and_send_to_cook."""
import json, math, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
NUM = r"(\d+\s+\d/\d|\d/\d|\d+(?:[.,]\d+)?)"          # сначала смешанные дроби и дроби, потом числа
UNITS = r"(kg|g|ml|l|tablespoons?|teaspoons?|egg yolks?|eggs?|cloves?)"
COUNT_NOUNS = ("balls patties squares pieces slices wraps rolls sandwiches burritos leaves halves discs cups steaks "
               "chops thighs tots pancakes crepes meatballs cutlets peppers wings mandarins tostadas").split()
COUNT_RE = r"\b\d+\s+(?:[A-Za-z\-]+\s+){0,2}?(?:" + "|".join(COUNT_NOUNS) + r")\b"
MAX_SCALE = 1.5          # больше — готовим несколькими партиями одного рецепта

def load_catalog(path=None):
    return json.load(open(path or os.path.join(HERE, "dishes_fixed.json"), encoding="utf-8"))

def _val(tok):
    tok = tok.replace(",", ".").strip()
    total = 0.0
    for part in tok.split():
        if "/" in part:
            a, b = part.split("/"); total += float(a) / float(b)
        else:
            total += float(part)
    return total

def _frac(v):
    """0.25 -> '1/4', 1.5 -> '1 1/2'"""
    whole = int(v)
    rest = round((v - whole) * 4)
    if rest == 4:
        whole += 1; rest = 0
    names = {0: "", 1: "1/4", 2: "1/2", 3: "3/4"}
    if whole == 0:
        return names[rest] or "1/4"
    return f"{whole} {names[rest]}".strip()

def _round_amount(v, unit):
    u = unit.lower()
    if u in ("g", "ml"):
        if v < 20: return str(max(1, round(v)))
        if v < 100: return str(int(round(v / 5.0) * 5))
        return str(int(round(v / 10.0) * 10))
    if u in ("kg", "l"):
        return ("%.2f" % (round(v / 0.05) * 0.05)).rstrip("0").rstrip(".")
    if u.startswith(("teaspoon", "tablespoon")):
        return _frac(max(0.25, round(v * 4) / 4.0))
    return str(max(1, int(round(v))))                  # яйца, зубчики, желтки, штуки

_SING = {"tablespoons": "tablespoon", "teaspoons": "teaspoon", "eggs": "egg", "cloves": "clove",
         "egg yolks": "egg yolk", "yolks": "yolk"}
_PLUR = {v: k for k, v in _SING.items()}

def _plural(unit, num_str):
    """Единственное число при значении <= 1 (1/2 teaspoon, 1 egg), иначе множественное."""
    try:
        v = _val(num_str)
    except Exception:
        v = 2
    base = _SING.get(unit.lower(), unit.lower())
    if base in _PLUR:
        return base if v <= 1 else _PLUR[base]
    return unit

def scale_text(text, factor, base, portions):
    """Масштабирует количества в тексте. Не трогает размер порции (X g each/per/of X g), время, температуру, сантиметры."""
    if abs(factor - 1.0) < 1e-9 or not text:
        return text
    protected = []
    def protect(m):
        protected.append(m.group(0)); return f"\u27e6{len(protected)-1}\u27e7"
    t = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:g|ml)\s+(?:each|per)\b", protect, text)
    t = re.sub(r"\bof\s+\d+(?:[.,]\d+)?\s*(?:g|ml)\b", protect, t)
    # счётные существительные: N кратно base -> масштабируем как число порций
    def count_fix(m):
        n = int(m.group(1))
        if n >= base and n % base == 0:
            return f"{int(round(n * portions / base))}{m.group(2)}{m.group(3)}"
        return m.group(0)
    t = re.sub(r"\b(\d+)(\s+(?:[A-Za-z\-]+\s+){0,2}?)(" + "|".join(COUNT_NOUNS) + r")\b", count_fix, t)
    def unit_fix(m):
        v = _val(m.group(1)) * factor
        unit = m.group(2)
        s = _round_amount(v, unit)
        return f"{s} {_plural(unit, s)}"
    t = re.sub(NUM + r"\s*" + UNITS + r"\b", unit_fix, t, flags=re.I)
    return re.sub(r"\u27e6(\d+)\u27e7", lambda m: protected[int(m.group(1))], t)

def plan_scale(dish, portions):
    """-> (base, factor_on_one_batch, batches, portions_per_batch)"""
    base = dish["base_portions"]
    factor = portions / base
    batches = int(math.ceil(factor / MAX_SCALE)) if factor > MAX_SCALE else 1
    return base, factor / batches, batches, portions / batches

def scaled_ingredients(dish, portions):
    """Для запасов: [(stock_key, unit, amount_total, is_staple)] на ВСЕ порции."""
    f = portions / dish["base_portions"]
    out = []
    for ing in dish["ingredients"]:
        if ing.get("amount") is None or ing.get("multi"):
            continue
        out.append((ing["stock_key"], ing["unit"], ing["amount"] * f, ing.get("staple", False)))
    return out

def _scale_ingredient_line(ing, f, base, P):
    line = ing["text"]
    if abs(f - 1) < 1e-9:
        return line
    if ing.get("amount") is not None and not ing.get("multi") and ":" in line:
        nm, rest = line.split(":", 1)
        pa = re.match(r"\s*(about\s+)?" + NUM + r"\s*(kg|g|ml|l|tablespoons?|teaspoons?|slices?|cloves?|pieces?)?\b", rest, re.I)
        if pa:
            unit = pa.group(3) or ""
            s = _round_amount(_val(pa.group(2)) * f, unit or "pcs")
            amt = (f"{pa.group(1) or ''}{s} {_plural(unit, s)}" if unit else f"{pa.group(1) or ''}{s}").strip()
            return nm + ": " + (amt + scale_text(rest[pa.end():], f, base, P)).strip()
        return nm + ":" + scale_text(rest, f, base, P)
    return scale_text(line, f, base, P)

def render_dish(dish, portions):
    """Готовый текст блюда для повара на нужное число порций."""
    base, f, batches, per_batch = plan_scale(dish, portions)
    P = int(round(per_batch))
    out = [f"{dish['emoji']} {dish['title'].upper()} — serves {portions}"]
    if dish.get("description"):
        out.append(dish["description"])
    out.append(dish["time_line"])
    if batches > 1:
        out.append(f"NOTE: make this recipe {batches} times, one batch after the other. Each batch serves about {P} portions. The amounts below are for ONE batch.")
    elif abs(f - 1) > 1e-9:
        steps_text = " ".join(x["text"] for x in dish["steps"])
        extra = (f" Where the steps count pieces (slices, patties, rolls, squares), make {portions} in total."
                 if re.search(COUNT_RE, steps_text) else "")
        out.append(f"NOTE: this recipe is written for {base} portions and you need {portions}. The amounts below are already adjusted.{extra}")
    out += ["", "YOU NEED"]
    for ing in dish["ingredients"]:
        if ing.get("stock_only") or not ing.get("text"):
            continue
        out.append("• " + _scale_ingredient_line(ing, f, base, P))
    for e in dish.get("equipment", []):
        out.append("• " + e)
    cur = "__start__"
    for s in dish["steps"]:
        if s["section"] != cur:
            cur = s["section"]
            out += ["", cur] if cur else [""]
        out.append(f"{s['n']}. " + scale_text(s["text"], f, base, P))
    for r in dish.get("ready_when", []):
        out.append(scale_text(r, f, base, P))
    for w in dish.get("warnings", []):
        out.append(w)
    return "\n".join(out)

def countdown_lines(items, catalog):
    """items: [(dish_id, portions)] -> строки 'T minus X min: ...' по убыванию X."""
    rows = sorted(((catalog[d].get("start_offset_min") or 0, catalog[d]["title"]) for d, _ in items), key=lambda r: -r[0])
    return [f"T minus {m} min: {t}" for m, t in rows] + ["T minus 0: everything on the table"]
