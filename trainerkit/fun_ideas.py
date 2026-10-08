"""Find unusual / fun cheat ideas in a Unity (Mono) game, for the agent to pitch to the user.
Three sources, all game-agnostic:
  1. developer debug/cheat flags left in the code (often whole hidden modes),
  2. gameplay levers worth bending (gravity, knockback, size, drop chance, enemy count, ...),
  3. Unity engine tricks that work in every Unity game (slow-mo, turbo, low gravity, camera zoom).
"""
import re

import dnfile

from .discover_unity import find_assembly

# member-name regex -> (category, pitch template, suggested mode/value)
LEVERS = [
    (r"debug|cheat|godmode|noclip|devmode|^dev|unlockall|allunlocked|freecam|showfps|skip(cutscene|intro)",
     "dev_flag", "Скрытый флаг разработчиков «{m}» в {c} — может включать целый режим", ("freeze", "true")),
    (r"gravity", "physics", "Своя гравитация героя ({c}.{m}): прыжки на Луне или кирпичом вниз", ("freeze", "@x0.3")),
    (r"jump(force|height|power|velocity|speed)", "movement", "Супер-прыжок ({c}.{m})", ("multiply", "3")),
    (r"knockback|launch(force|power)|pushforce|impulse", "physics", "Враги улетают за экран от удара ({c}.{m})", ("multiply", "8")),
    (r"(local)?scale|size(mult|multiplier)?$|bighead|headsize", "visual", "Великан / лилипут ({c}.{m})", ("multiply", "2")),
    (r"drop(chance|rate|pct|percent)|luck|lootchance|critchance|crit(rate|pct)", "luck", "Всё время везёт: {c}.{m} на максимум", ("freeze", "1")),
    (r"(enemy|spawn)(count|max|limit|amount|wave)|maxenemies", "chaos", "Орда врагов: {c}.{m} ×5 — хаос и много опыта", ("multiply", "5")),
    (r"cooldown|recharge|reload(time|speed)", "combat", "Без перезарядки: {c}.{m} = 0", ("freeze", "0")),
    (r"attackspeed|animspeed|combospeed|firerate", "combat", "Пулемётные удары ({c}.{m} ×3)", ("multiply", "3")),
    (r"friendlyfire|canhitallies|teamkill", "chaos", "Дружеский огонь ({c}.{m})", ("freeze", "true")),
    (r"(price|cost)(mult|multiplier|pct)?$|discount", "economy", "Всё в магазине бесплатно ({c}.{m} = 0)", ("freeze", "0")),
    (r"airjump|doublejump|maxjumps|jumpcount", "movement", "Бесконечные прыжки в воздухе ({c}.{m})", ("freeze", "99")),
    (r"dash(distance|speed|count)|dodge(distance|count)", "movement", "Рывок на пол-экрана ({c}.{m} ×4)", ("multiply", "4")),
    (r"stun|freeze(time|duration)|hitstop|hitpause", "combat", "Враги надолго в стане ({c}.{m} ×10)", ("multiply", "10")),
    (r"difficulty|aggression|ailevel|aggro", "chaos", "Подкрутить ИИ/сложность ({c}.{m})", ("freeze", "0")),
    (r"timescale|gamespeed|slowmo", "time", "Своё время игры ({c}.{m})", ("freeze", "0.5")),
    (r"invisible|stealth|detect(ion)?range|visionrange|sightrange", "stealth", "Невидимка: враги не видят ({c}.{m} = 0)", ("freeze", "0")),
    (r"fov|zoom|orthographicsize|camdistance|cameradistance", "camera", "Отдалить камеру ({c}.{m})", ("multiply", "1.6")),
    (r"character|playable|skin|costume|outfit", "visual", "Подмена персонажа/скина ({c}.{m}) — проверить, можно ли играть за другого", ("set", "?")),
]

# work in ANY Unity game, no game code needed (static members of UnityEngine)
ENGINE = [
    {"name": "Slow-Mo x0.5", "class": "UnityEngine.Time", "member": "timeScale", "mode": "freeze", "value": "0.5",
     "pitch": "Замедление времени — бой как в «Матрице»"},
    {"name": "Turbo x2", "class": "UnityEngine.Time", "member": "timeScale", "mode": "freeze", "value": "2",
     "pitch": "Вся игра в два раза быстрее (спидран-режим)"},
    {"name": "Moon Gravity (2D)", "class": "UnityEngine.Physics2D", "member": "gravity", "mode": "freeze", "value": "0,-3",
     "pitch": "Лунная гравитация для 2D-физики (если игра её использует)"},
    {"name": "Moon Gravity (3D)", "class": "UnityEngine.Physics", "member": "gravity", "mode": "freeze", "value": "0,-3,0",
     "pitch": "Лунная гравитация для 3D-физики (если игра её использует)"},
]

CLASS_BONUS = re.compile(r"player|hero|character|combat|weapon|enemy|spawn|camera|game(manager|settings|tweaks)|debug|cheat|difficulty|shop|loot", re.I)


def scan(target):
    pe = dnfile.dnPE(str(find_assembly(target)))
    found, seen = [], set()
    for t in pe.net.mdtables.TypeDef.rows:
        cls = f"{t.TypeNamespace}.{t.TypeName}".strip(".")
        if cls.startswith("<") or "Editor" in cls:
            continue
        members = [str(f.row.Name) for f in (t.FieldList or [])] + \
                  [str(m.row.Name)[4:] for m in (t.MethodList or []) if str(m.row.Name).startswith("set_")]
        for mem in members:
            low = mem.lower().lstrip("_").replace("<", "").split(">")[0]
            if low.startswith("m") and len(low) > 2 and mem.startswith("m") and mem[1:2].isupper():
                low = low[1:]
            for rx, cat, pitch, (mode, value) in LEVERS:
                if re.search(rx, low):
                    key = (cls, low)
                    if key in seen:
                        break
                    seen.add(key)
                    score = (6 if cat == "dev_flag" else 3) + (3 if CLASS_BONUS.search(cls) else 0) \
                        - (4 if re.search(r"ui|menu|audio|sound|vfx|particle|tween|anim(ator|ation)", cls, re.I) else 0) \
                        - (6 if re.search(r"vfx|sfx|fx|key|hash|data|propert|log|draw|gizmo|path|name|id$|sprite|description", low) else 0) \
                        + (3 if cat == "dev_flag" and re.search(r"invinc|god|unlock|ignore|infinite|zoom|skip|free|noclip|all", low) else 0)
                    found.append({"category": cat, "class": cls, "member": mem.replace("<", "").split(">")[0],
                                  "pitch": pitch.format(c=cls.split(".")[-1], m=mem), "mode": mode,
                                  "value": value, "score": score})
                    break
    found = [f for f in found if f["score"] > 0]
    found.sort(key=lambda x: -x["score"])
    return {"engine": ENGINE, "game": found}
