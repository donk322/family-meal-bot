# -*- coding: utf-8 -*-
from collections import Counter, defaultdict
from scale_recipe import load_catalog
from stock_check import check_dish

STOCK = {
 "almonds":35,"arrowroot":150,"bbq seasoning":50,"brie":250,"brown lentils":400,"buckwheat gluten-free bread":2550,"cabbage":46,
 "cauliflower":3.4,"celery":2,"cherries":2000,"chicken stock cubes":14.5,"chopped mushrooms":120,"clemengold mandarins":1300,
 "coconut aminos":240,"garlic herb stock cubes":4,"gluten-free flatbread":4,"kentucky coating":190,"kiwi":500,"mayonnaise":1030,
 "mixed peppers":700,"mushroom stock cubes":30,"napoletana tomato sauce":700,"ostrich mince":500,"oyster mushrooms":130,
 "peanut butter":500,"peas in pods":240,"pineapple":700,"prawns":2560,"raspberries":2400,"red onion":1000,"salami":1600,
 "shiitake mushrooms":200,"shimeji mushrooms":300,"shirataki noodles":600,"tamari sauce":470,"vanilla extract":50,
 "vegetable stock cubes":18.4,"avocado":610,"banana":1200,"gluten-free bread":18.5,"bacon":180,"bell pepper":1270,"ham":0,
 "beef":240,"beef steak":1062,"ground beef":2000,"potato":3000,"sausages":5770,"chicken thigh":992,"chicken breast":516,
 "lime":535,"onion":1940,"milk":3000,"carrot":4000,"honey":305,"cucumber":1800,"pickled cucumbers":510,"paprika":12.5,
 "parmesan":63,"tomato":1550,"pork mince":480,"beetroot":2050,"lightly salted salmon":100,"cream":250,"butter":132,
 "sour cream":500,"salt":435,"hard cheese":0,"tomato paste":675,"dill":10,"salmon fillet":1602,"zucchini":340,"garlic":14,
 "black pepper":230.5,"mushrooms":570,"berries":2500,"eggs":87,"apple":3960,"white fish fillet":1200,
}
cat = load_catalog()
ok, blocked = [], {}
for did, d in cat.items():
    good, miss = check_dish(d, d["base_portions"], STOCK)
    (ok.append(d) if good else blocked.__setitem__(did, miss))
print(f"Можно готовить сейчас (на базовое число порций): {len(ok)} из {len(cat)}")
bycat = defaultdict(list)
for d in ok: bycat[d["category"]].append(d["title"])
for c, v in bycat.items():
    print(f"\n[{c}] {len(v)}"); [print("  -", t) for t in v]
print("\n\nНЕ ХВАТАЕТ ДЛЯ БЛЮД:")
only = Counter(); anyc = Counter()
for did, miss in blocked.items():
    keys = [m[0] for m in miss]
    for k in keys: anyc[k] += 1
    if len(keys) == 1: only[keys[0]] += 1
for did, miss in sorted(blocked.items(), key=lambda x: len(x[1])):
    print(f"- {cat[did]['title']}: " + ", ".join(f"{k} (нужно {n}, есть {h})" for k, n, h in miss))
print("\n\nЧто докупить, чтобы открыть больше всего блюд (одно блюдо ждёт только это / всего блюд, где этого не хватает):")
for k, n in anyc.most_common(25):
    print(f"  {k}: открывает само по себе {only.get(k,0)}, участвует в {n}")
