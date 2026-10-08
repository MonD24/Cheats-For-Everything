"""
Automatic feature discovery for Unity (Mono) games: read Assembly-CSharp.dll metadata
(no decompiler, no .NET SDK) and rank fields/properties that look like cheatable stats.

  python -m trainerkit.discover_unity "<game folder or exe>"  [--json out.json]
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import dnfile

# keyword -> (feature, weight). Matched against lowercased member names.
KEYWORDS = {
    r"^(cur|current)?(health|hp|hitpoints?)$|health(cur|current)?$|^hp(cur)?$": ("health", 10),
    r"max(health|hp)|(health|hp)max": ("max_health", 6),
    r"money|cash|coins?|gold|currency|wallet|funds": ("money", 10),
    r"special|^sp$|meter|rage|super|mana|energy|stamina": ("special_meter", 7),
    r"lives|lifecount|extralife": ("lives", 8),
    r"invinc|invuln|godmode|immortal|nodamage|cantakedamage|isdead": ("invincibility", 7),
    r"^(exp|xp|experience)$|experience|xp(cur|current)?$": ("experience", 7),
    r"^level$|playerlevel|charlevel": ("level", 5),
    r"damage(mult|multiplier|scale)|attack(power|damage)|strength|^atk$": ("damage", 6),
    r"(move|walk|run)speed|speedmult": ("speed", 5),
    r"timer|timeleft|countdown": ("timer", 4),
    r"ammo|bullets|clip": ("ammo", 8),
    r"cooldown": ("cooldown", 3),
}
CLASS_HINT = re.compile(r"player|hero|character|stats?|inventory|wallet|save|profile|combat|fighter", re.I)
NUMERIC = {"Int32": "int32", "Single": "float", "Int64": "int64", "Double": "double",
           "Boolean": "bool", "Int16": "int16", "UInt32": "uint32", "Byte": "byte"}


def find_assembly(target):
    p = Path(target)
    if p.is_file():
        p = p.parent
    hits = list(p.glob("*_Data/Managed/Assembly-CSharp.dll"))
    if not hits:
        raise FileNotFoundError("Assembly-CSharp.dll not found: not a Unity Mono game (IL2CPP needs a dumper)")
    return hits[0]


ELEMENT = {0x02: "bool", 0x04: "byte", 0x05: "byte", 0x06: "int16", 0x07: "int16", 0x08: "int32", 0x09: "uint32",
           0x0A: "int64", 0x0B: "int64", 0x0C: "float", 0x0D: "double"}


def field_type(sig_blob):
    """Return a short type name from a field signature, or None.
    The blob is 0x06 (FIELD), optional custom modifiers, then the ECMA-335 element type byte."""
    raw = getattr(sig_blob, "value", None)
    if isinstance(raw, (bytes, bytearray)) and raw[:1] == b"\x06":
        i = 1
        while i < len(raw) and raw[i] in (0x1F, 0x20):   # cmod_reqd / cmod_opt + compressed token
            i += 2
        return ELEMENT.get(raw[i]) if i < len(raw) else None
    try:
        t = sig_blob.type.element_type.name
    except Exception:
        try:
            t = str(sig_blob.type)
        except Exception:
            return None
    for k, v in NUMERIC.items():
        if k.lower() in t.lower() or {"I4": "int32", "R4": "float", "I8": "int64", "R8": "double",
                                       "BOOLEAN": "bool"}.get(t.upper()) == v:
            return v
    m = {"I4": "int32", "R4": "float", "I8": "int64", "R8": "double", "BOOLEAN": "bool", "I2": "int16",
         "U4": "uint32", "U1": "byte"}
    return m.get(t.upper().replace("ELEMENT_TYPE_", ""))


def scan(asm_path):
    pe = dnfile.dnPE(str(asm_path))
    md = pe.net.mdtables
    types = md.TypeDef.rows
    out = []
    for t in types:
        cls = str(t.TypeName)
        ns = str(t.TypeNamespace)
        members = []
        for f in (t.FieldList or []):
            row = f.row if hasattr(f, "row") else f
            try:
                sig = row.Signature
                vt = field_type(sig)
            except Exception:
                vt = None
            is_static = bool(row.Flags.fdStatic) if hasattr(row.Flags, "fdStatic") else False
            members.append(("field", str(row.Name), vt, is_static))
        for m in (t.MethodList or []):
            row = m.row if hasattr(m, "row") else m
            name = str(row.Name)
            if name.startswith("get_") or name.startswith("set_"):
                members.append(("prop", name[4:], None, False))
            elif re.search(r"damage|hurt|hit|die|kill|takedamage|addmoney|spend|heal", name, re.I):
                members.append(("method", name, None, False))
        for kind, name, vt, st in members:
            low = name.lower().lstrip("_m")
            for rx, (feat, w) in KEYWORDS.items():
                if re.search(rx, low):
                    score = w + (4 if CLASS_HINT.search(cls) else 0) + (2 if vt else 0) - (3 if kind == "method" else 0)
                    if "enemy" in cls.lower() or "boss" in cls.lower():
                        score -= 5
                    out.append({"feature": feat, "class": f"{ns}.{cls}".strip("."), "member": name,
                                "kind": kind, "type": vt, "static": st, "score": score})
                    break
    # dedupe props vs backing fields; best first per feature
    seen, best = set(), defaultdict(list)
    for c in sorted(out, key=lambda c: -c["score"]):
        key = (c["class"], c["member"].lower())
        if key in seen:
            continue
        seen.add(key)
        best[c["feature"]].append(c)
    return {f: v for f, v in sorted(best.items(), key=lambda kv: -kv[1][0]["score"])}


def main():
    args = sys.argv[1:]
    asm = find_assembly(args[0])
    res = scan(asm)
    if "--json" in args:
        Path(args[args.index("--json") + 1]).write_text(json.dumps(res, indent=1), encoding="utf-8")
    for feat, cands in res.items():
        print(f"\n== {feat}")
        for c in cands[:5]:
            print(f"  {c['score']:3d}  {c['class']}.{c['member']}  [{c['kind']}{' static' if c['static'] else ''}{' ' + c['type'] if c['type'] else ''}]")


if __name__ == "__main__":
    main()
