"""Follow a value from the HUD back to where the game keeps it, by reading IL (no decompiler needed).

Many games only refresh a HUD label when the value changes through game code, so writing a field
shows nothing on screen even when it is the right field - or the visible number comes from a field
the name heuristics never ranked. The UI setter (SetMoneyText(float), UpdateAmmoLabel(int), ...) is
easy to find by name; its callers load the real value right before the call. This module lists, for
every call site of a method, the fields/properties read in the few instructions before it.

  python -m trainerkit.il_trace "<game folder>" SetMoneyText
"""
import re
import struct
import sys

import dnfile

from .discover_unity import find_assembly

# one-byte opcodes with their operand sizes (ECMA-335 III); 0xFE prefix = two-byte opcode
_OPERAND = {**{op: 0 for op in range(0x00, 0x100)},
            0x0E: 1, 0x0F: 1, 0x10: 1, 0x11: 1, 0x12: 1, 0x13: 1, 0x1F: 1, 0x20: 4, 0x21: 8, 0x22: 4, 0x23: 8,
            0x27: 4, 0x28: 4, 0x29: 4, 0x2B: 1, 0x2C: 1, 0x2D: 1, 0x2E: 1, 0x2F: 1, 0x30: 1, 0x31: 1, 0x32: 1,
            0x33: 1, 0x34: 1, 0x35: 1, 0x36: 1, 0x37: 1, 0x38: 4, 0x39: 4, 0x3A: 4, 0x3B: 4, 0x3C: 4, 0x3D: 4,
            0x3E: 4, 0x3F: 4, 0x40: 4, 0x41: 4, 0x42: 4, 0x43: 4, 0x44: 4, 0x6F: 4, 0x70: 4, 0x71: 4, 0x72: 4,
            0x73: 4, 0x74: 4, 0x75: 4, 0x79: 4, 0x7B: 4, 0x7C: 4, 0x7D: 4, 0x7E: 4, 0x7F: 4, 0x80: 4, 0x81: 4,
            0x8C: 4, 0x8D: 4, 0x8F: 4, 0xA3: 4, 0xA4: 4, 0xA5: 4, 0xC2: 4, 0xC6: 4, 0xD0: 4, 0xDD: 4, 0xDE: 1}
_OPERAND2 = {0x06: 4, 0x07: 4, 0x09: 2, 0x0A: 2, 0x0B: 2, 0x0C: 2, 0x0D: 2, 0x0E: 2, 0x12: 1, 0x15: 4, 0x16: 4, 0x19: 1, 0x1C: 4}
CALLS = {0x28, 0x6F, 0x73}          # call, callvirt, newobj
LOADS = {0x7B: "field", 0x7E: "static field", 0x7C: "field address"}


def instructions(code):
    i = 0
    while i < len(code):
        op = code[i]; start = i; i += 1
        if op == 0xFE:
            op2 = code[i]; i += 1
            i += _OPERAND2.get(op2, 0)
            yield start, 0xFE00 | op2, None
            continue
        if op == 0x45:                                  # switch: n targets
            n = struct.unpack_from("<I", code, i)[0]
            i += 4 + 4 * n
            yield start, op, None
            continue
        size = _OPERAND.get(op, 0)
        operand = int.from_bytes(code[i:i + size], "little") if size else None
        i += size
        yield start, op, operand


class Assembly:
    def __init__(self, path):
        self.pe = dnfile.dnPE(str(path))
        self.md = self.pe.net.mdtables
        self.owner = {}
        for t in self.md.TypeDef.rows:
            cls = f"{t.TypeNamespace}.{t.TypeName}".strip(".")
            for m in (t.MethodList or []):
                self.owner[id(m.row)] = cls
            for f in (t.FieldList or []):
                self.owner[id(f.row)] = cls

    def body(self, row):
        rva = row.Rva
        if not rva:
            return b""
        data = self.pe.get_data(rva, 12)
        if data[0] & 0x3 == 0x2:                     # tiny header
            return self.pe.get_data(rva + 1, data[0] >> 2)
        size = struct.unpack_from("<I", data, 4)[0]
        hdr = (struct.unpack_from("<H", data, 0)[0] >> 12) * 4
        return self.pe.get_data(rva + hdr, size)

    def name(self, token):
        table, idx = token >> 24, (token & 0xFFFFFF) - 1
        try:
            if table == 0x06:
                r = self.md.MethodDef.rows[idx]; return f"{self.owner.get(id(r), '?')}.{r.Name}"
            if table == 0x04:
                r = self.md.Field.rows[idx]; return f"{self.owner.get(id(r), '?')}.{r.Name}"
            if table == 0x0A:
                r = self.md.MemberRef.rows[idx]
                parent = r.Class.row
                pname = getattr(parent, "TypeName", None) or getattr(parent, "Name", "?")
                return f"{pname}.{r.Name}"
        except Exception:
            return None
        return None

    def callers(self, method_name, window=12):
        """[(caller, [values read before the call])] for every call to a method with this name."""
        out = []
        for t in self.md.TypeDef.rows:
            cls = f"{t.TypeNamespace}.{t.TypeName}".strip(".")
            for m in (t.MethodList or []):
                try:
                    code = self.body(m.row)
                except Exception:
                    continue
                recent = []
                for _, op, operand in instructions(code):
                    if op in LOADS or (op in CALLS and operand):
                        target = self.name(operand) if operand else None
                        if op in CALLS and target and target.split(".")[-1] == method_name:
                            out.append((f"{cls}.{m.row.Name}", list(recent[-window:])))
                        elif target:
                            recent.append(target)
        return out


def feeds(target, method_name):
    """Fields/properties that flow into calls of `method_name` (most frequent first)."""
    asm = Assembly(find_assembly(target))
    hits = asm.callers(method_name)
    counts = {}
    for _, reads in hits:
        for r in reads:
            if re.search(r"\.(get_)?\w+$", r) and not re.search(r"\.(op_|ToString|Format|Concat|get_Instance)", r):
                counts[r] = counts.get(r, 0) + 1
    return {"call_sites": [c for c, _ in hits], "reads": sorted(counts, key=lambda k: -counts[k])}


if __name__ == "__main__":
    import json
    print(json.dumps(feeds(sys.argv[1], sys.argv[2]), indent=1))


UI_SETTER = re.compile(r"(?i)^(set|update|refresh|show|display|redraw)_?\w*(money|cash|coins?|gold|currency|bucks|wallet|"
                       r"funds|gems?|souls?)\w*$")
UI_CLASS = re.compile(r"(?i)(^|[._])ui|hud|view|display|panel|widget|label|counter|screen")
UI_NAME = re.compile(r"(?i)(text|label|display|counter|ui|hud|amount)$")


def hud_sources(target, setter=UI_SETTER):
    """Leaf names of fields/properties whose values flow into UI setters matching `setter`
    (e.g. {'Money'} for SetMoneyText(float) fed by PlayerFullInventory.Money)."""
    asm = Assembly(find_assembly(target))
    names = {str(m.row.Name) for t in asm.md.TypeDef.rows for m in (t.MethodList or [])
             if setter.match(str(m.row.Name)) and not str(m.row.Name).startswith("set_")
             and (UI_CLASS.search(f"{t.TypeNamespace}.{t.TypeName}") or UI_NAME.search(str(m.row.Name)))}
    leafs, sites = {}, []
    for name in names:
        for caller, reads in asm.callers(name):
            sites.append(f"{caller} -> {name}")
            for r in reads:
                leaf = r.split(".")[-1]
                leaf = leaf[4:] if leaf.startswith(("get_", "set_")) else leaf
                if leaf in names or re.search(r"(?i)^(instance|Instance|op_|ToString|Format|Concat|SetActive|transform|gameObject)", leaf):
                    continue
                leafs[leaf] = leafs.get(leaf, 0) + 1
    return {"setters": sorted(names), "call_sites": sites, "fields": sorted(leafs, key=lambda k: -leafs[k])}
