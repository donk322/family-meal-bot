# -*- coding: utf-8 -*-
import re, sys
from scale_recipe import load_catalog, render_dish, scaled_ingredients

cat = load_catalog()
errors = []
for did, d in cat.items():
    for P in range(3, 17):
        try:
            txt = render_dish(d, P)
        except Exception as e:
            errors.append(f"{did} P={P}: упало {e!r}"); continue
        if re.search(r"\b0(\.0+)? ?(g|ml|kg|tablespoons?|teaspoons?|eggs?|cloves?)\b", txt):
            errors.append(f"{did} P={P}: нулевое количество")
        if re.search(r"nan|None|\u27e6", txt):
            errors.append(f"{did} P={P}: мусор в тексте")
        for line in txt.split("\n"):
            if line.startswith("• Eggs:") and not re.match(r"• Eggs: \d+( |$|\()", line):
                errors.append(f"{did} P={P}: дробные яйца: {line}")
        for key, unit, amt, st in scaled_ingredients(d, P):
            if amt <= 0: errors.append(f"{did} P={P}: нулевой запас {key}")
print("Проверено блюд:", len(cat), "× порций 3..16")
print("Ошибок:", len(errors))
for e in errors[:40]: print("  ", e)

def show(did, P, lines=14):
    print("\n" + "=" * 70); print(f"{did} на {P} порций")
    t = render_dish(cat[did], P).split("\n")
    print("\n".join(t[:lines]))

if "--show" in sys.argv:
    show("beef_and_pepper_breakfast_skillet_with_cheese_and_smoky_mayo", 6, 4)
    show("beef_and_pepper_breakfast_skillet_with_cheese_and_smoky_mayo", 7, 22)
    show("steak_sandwich_with_brie_soft_eggs_onions_and_mushrooms", 9, 40)
