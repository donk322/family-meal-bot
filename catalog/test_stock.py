# -*- coding: utf-8 -*-
"""Тест проверки и списания запасов (stock_check.py). Запуск: cd catalog && python test_stock.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stock_check import check_dish, subtract_for_dish, is_enough
from scale_recipe import load_catalog

errors = []

def expect(name, got, want):
    if got != want:
        errors.append(f"{name}: получили {got!r}, ожидали {want!r}")

def dish(*ings, base=4):
    """Блюдо из (stock_key, unit, amount[, staple]) на base порций."""
    return {"base_portions": base, "ingredients": [
        {"stock_key": k, "unit": u, "amount": a, "staple": (s[0] if s else False)} for k, u, a, *s in ings]}

# --- допуск: 90% или нехватка до 5 г/мл при потребности до 100 ---
cases = [
    ("ровно столько", 500, 500, "g", True),
    ("90% от 500 г", 500, 450, "g", True),
    ("89% от 500 г", 500, 445, "g", False),
    ("100 г, есть 95 (нехватка 5)", 100, 95, "g", True),
    ("100 г, есть 94 (нехватка 6, 94%)", 100, 94, "g", True),       # 94% >= 90%
    ("60 г, есть 55 (нехватка 5, 92%)", 60, 55, "g", True),
    ("30 г, есть 25 (нехватка 5, 83%)", 30, 25, "g", True),        # спасает правило 5 г
    ("30 г, есть 24 (нехватка 6, 80%)", 30, 24, "g", False),
    ("20 мл, есть 15 (нехватка 5)", 20, 15, "ml", True),
    ("120 г, есть 100 (нужно больше 100, 83%)", 120, 100, "g", False),
    ("8 г, есть 3 (нехватка 5)", 8, 3, "g", True),
    ("8 г, продукта нет вообще", 8, None, "g", False),
    ("3 г, есть 0 (позиция есть, но пустая)", 3, 0, "g", False),
    ("3 г, есть 1 (нехватка 2)", 3, 1, "g", True),
    ("10 яиц, есть 9 (90%)", 10, 9, "pcs", True),
    ("10 яиц, есть 5 (правило 5 г не для штук)", 10, 5, "pcs", False),
    ("4 шт, есть 0", 4, 0, "pcs", False),
]
for name, need, have, unit, want in cases:
    expect(f"is_enough: {name}", is_enough(need, have, unit), want)

# --- check_dish: недостача, отсутствие, staple, пересчёт порций ---
d = dish(("ground beef", "g", 800), ("sour cream", "ml", 40), ("salt", "tsp", 2, True), ("eggs", "pcs", 4))
expect("всего хватает", check_dish(d, 4, {"ground beef": 800, "sour cream": 40, "eggs": 4})[0], True)
expect("соль (staple) не проверяется", check_dish(d, 4, {"ground beef": 800, "sour cream": 40, "eggs": 4})[1], [])
ok, miss = check_dish(d, 8, {"ground beef": 1000, "sour cream": 80, "eggs": 8})
expect("на 8 порций фарша 1000 из 1600", (ok, miss), (False, [("ground beef", 1600.0, 1000.0)]))
ok, miss = check_dish(d, 4, {"ground beef": 800, "eggs": 4})
expect("сметаны нет вообще", miss, [("sour cream", 40.0, 0)])
expect("сметаны 36 из 40 (90%)", check_dish(d, 4, {"ground beef": 800, "sour cream": 36, "eggs": 4})[0], True)

# --- пересчёт единиц (CONV) и группы ---
p = dish(("bell pepper", "pcs", 4), base=4)                     # 4 шт = 600 г
expect("перец 4 шт против 600 г", check_dish(p, 4, {"bell pepper": 600})[0], True)
expect("перец 4 шт против 500 г (83%)", check_dish(p, 4, {"bell pepper": 500})[0], False)
expect("перец покрывает mixed peppers", check_dish(p, 4, {"mixed peppers": 600})[0], True)
r = dish(("raspberries", "g", 300))
expect("малину покрывают berries", check_dish(r, 4, {"berries": 300})[0], True)
m = dish(("mushrooms", "g", 500))
expect("грибы складываются по группе", check_dish(m, 4, {"mushrooms": 200, "shiitake mushrooms": 300})[0], True)

# --- subtract_for_dish ---
after = subtract_for_dish(d, 4, {"ground beef": 1000, "sour cream": 100, "eggs": 10, "salt": 50})
expect("списание фарша", after["ground beef"], 200)
expect("списание сметаны", after["sour cream"], 60)
expect("staple не списывается", after["salt"], 50)
after = subtract_for_dish(d, 4, {"ground beef": 790, "sour cream": 40, "eggs": 4})
expect("не уходит в минус при допуске", after["ground beef"], 0)
after = subtract_for_dish(m, 4, {"mushrooms": 200, "shiitake mushrooms": 400})
expect("группа списывается по порядку", (after["mushrooms"], after["shiitake mushrooms"]), (0, 100))

# --- весь каталог: на точном запасе всё доступно, без запасов — ничего ---
cat = load_catalog()
from scale_recipe import scaled_ingredients
from stock_check import to_stock_unit
for did, dd in cat.items():
    exact = {}
    for k, u, a, st in scaled_ingredients(dd, dd["base_portions"]):
        if not st:
            exact[k] = exact.get(k, 0) + to_stock_unit(k, u, a)
    if not check_dish(dd, dd["base_portions"], exact)[0]:
        errors.append(f"{did}: недоступно на точном запасе: {check_dish(dd, dd['base_portions'], exact)[1]}")
    if check_dish(dd, dd["base_portions"], {})[0]:
        errors.append(f"{did}: доступно без запасов")

print("Проверок допуска:", len(cases), "| блюд каталога:", len(cat))
print("Ошибок:", len(errors))
for e in errors:
    print("  ", e)
sys.exit(1 if errors else 0)
