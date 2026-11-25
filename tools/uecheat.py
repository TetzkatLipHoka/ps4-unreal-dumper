"""Stage-1 cheat generator: UE4 dump -> TLH trainer file for the cheat-menu PRX (no PRX changes, no names at runtime).
    python uecheat.py ../dumps/CUSA09175/dump.bin out.TLH                      # default set (God, Player time, Walk speed)
    python uecheat.py <dump.bin> <out.TLH> "Name=Path=Value" ...              # Path relative to the player pawn
      "God=bCanBeDamaged=0"  "Player time=CustomTimeDilation=2"  "Walk speed=CharacterMovement.MaxWalkSpeed=1200"
      "Anim speed=Mesh.AnimScriptInstance:BendPlayerAnimInstance.LocationBasedSpeedScale=2"   (Prop:Class = downcast)
      "Inf Hp=HealthComponent.HealthSet.Damage.CurrentValue=0"   (struct members inline: HealthSet, Damage)
      "Hp=AbilitySystemComponent.SpawnedAttributes[3]:ATR_Health.Health.CurrentValue=500"   (TArray<UObject*> element)
      "Items=Controller:GameplayPC_C.PlayerInventoryComponent.Inventory[15].Amount=99"     (TArray<struct> element)
      "Hp=World.OwningGameInstance:BP_MossGameInstance_C.SharedHero:BP_HeroCharacter_C.CurrentHealth=3"
                          (World.-rooted: from GWorld, for characters the controlled pawn does not reach; --find/--addr
                           search from the pawn and from World; "GWorld." works too)
      downcasts left out are added from the dump's objects ("PlayerController.Pawn.VehicleState" -> "Pawn:VBP_Car_C")
      "Hp=GameData:RmoPlayerData.@0xd8:i=999"   (@offset:type = unreflected C++ field, as --addr prints it; i/f/d/b)
      "Slot=Inventory[$0].Amount=99"   ([$n] = index from an editable trainer cell, default n: one cheat for every slot)
      "Range=InteractionDetector.InteractionRanges{Medium}=500"   (TMap / TSet element by key: number, enum member, FName,
                          {$n} = key from a cell; a number value ends the path, struct / object values go on with .Member)
    python uecheat.py <dump.bin> <out.TLH> "Name=call:Path.Function(args)"   # the game's own function, once per switch-on
      through ProcessEvent from the hook (game thread). Path from the pawn (Pawn. or none), World. / GWorld., PC. (the local
      PlayerController), or a class name for static functions (called on its default object). Args: numbers, true/false,
      enum members, null, World / Pawn / PC / an object path, a class name for UClass parameters, $n = editable cell.
      "Slow motion=call:GameplayStatics.SetGlobalTimeDilation(World, $0.3)"  "Heal=call:HealthComponent.Heal($100)"
    python uecheat.py <dump.bin> <out.TLH> "Name=patch:Class.Function=noop|true|false|<n>"   # TLH Assembly entry at the
      function's C++ body (return forced / call skipped), toggled by the menu, no hook; every caller is affected (enemies too).
      Refused when the body is inlined into its exec thunk.
    python uecheat.py <dump.bin> --find Damage [depth] [--types int,float]   # list ready-to-use paths whose name contains the
      word (default depth 4); heal* / *hp / max*hp = a pattern over one name. Walks the dump's live player pawn (real component
      classes, downcasts added), prints path, type and dump value
    python uecheat.py <dump.bin> --methods [word]   # functions with the word (default: damage, heal, unlock, give ...),
      on objects the pawn / World reach first, with ready call: and patch: specs
    python uecheat.py <dump.bin> --addr 0x15a4a0d274 [0x...] [depth]   # path(s) to an address found with a scanner
      (same game session as the dump; default depth 6)
    python uecheat.py <dump.bin> --fields BP_Components_PlayerAttributesComponent.FloatAttributes[3]
      every member of the object/struct at that path with its dump value ("" = the pawn itself)
    python uecheat.py <dump.bin> <out.TLH> "Name=button=World.AuthorityGameMode.weak:0xbfc.ToggleCheatMenu"
      button = call a native UFunction's C++ implementation once each time the cheat is switched on. Object path from
      World (GWorld) or Pawn; hops: property name, +0xNN (raw pointer), weak:0xNN (TWeakObjectPtr {Index, Serial}).
One StartUP injection on UObject::ProcessEvent (runs thousands of times per frame on the game thread) walks
GWorld -> World.OwningGameInstance -> GameInstance.LocalPlayers[0] -> Player.PlayerController -> Controller.Pawn and
writes every enabled cheat's value; the cheats themselves are plain Value cells (flag byte + editable number).
Offsets come from the dump's SDK; GWorld/GObjects/ProcessEvent from offsets.json (base-relative, stable per binary).
Bool cheats set/clear the bit while on and leave the last state when switched off (no original to restore).
"""
import itertools, json, os, re, struct, sys
import ueobj
from ueobj import CF

CELL = dict(FloatProperty=("Single", 4, "edx"), DoubleProperty=("Double", 8, "rdx"), IntProperty=("Integer", 4, "edx"), UInt32Property=("Cardinal", 4, "edx"),
            ByteProperty=("Byte", 1, "dl"), Int8Property=("Byte", 1, "dl"))
DEFAULT = ["God=bCanBeDamaged=0", "Player time=CustomTimeDilation=1", "Walk speed=CharacterMovement.MaxWalkSpeed=600"]
FMT = dict(FloatProperty="<f", DoubleProperty="<d", IntProperty="<i", UInt32Property="<I", ByteProperty="<B", Int8Property="<b")
CALLER_SAVED = ["rsi", "rdi", "r8", "r9", "r10", "r11"]      # rax/rcx/rdx are saved by the cave prologue


def member(e, cls, prop):
    c = cls
    while c:
        p = e.member(c, prop)
        if p:
            return p
        c = e.super(c)
    sys.exit(f"{e.name(cls)} has no property {prop}")


def has_member(e, cls, prop):
    while cls:
        if e.member(cls, prop):
            return True
        cls = e.super(cls)


def auto_cast(e, obj, segs):
    """Add ":Class" downcasts the path leaves out, taken from the objects in the dump: a member missing on the declared
    class but present on the instance's real class (paths from other tools: "Pawn.VehicleState" -> "Pawn:VBP_Car_C...")."""
    segs, addr, cls = list(segs), obj, e.cls(obj) if obj else None
    for i in range(1, len(segs) + 1):
        if not cls or i == len(segs) or segs[i - 1][:1] in "@+" or segs[i - 1].startswith("weak:"):
            break
        tok = segs[i - 1]
        name, _, cast = tok.partition(":")
        name, _, idx = name.rstrip("]").partition("[")
        c = cls
        while c and not e.member(c, name):
            c = e.super(c)
        p = c and e.member(c, name)
        if not p:
            break
        kind, a = e.ffield_class_name(p), addr + e.prop_offset(p) if addr else None
        if kind == "StructProperty":
            cls = e.ptr(p + e.O["StructProperty_Struct"])
        elif idx and kind == "ArrayProperty":
            inner = e.ptr(p + e.O["ArrayProperty_Inner"])
            data = a and e.ptr(a)
            if e.ffield_class_name(inner) == "StructProperty":
                cls = e.ptr(inner + e.O["StructProperty_Struct"])
                a = data and data + int(idx.lstrip("$") or 0, 0) * e.prop_size(inner)
            else:
                cls, a = e.ptr(inner + e.O["ObjectProperty_Class"]), data and e.ptr(data + 8 * int(idx.lstrip("$") or 0, 0))
                a = ("obj", a)
        elif kind in ("ObjectProperty", "WeakObjectProperty"):
            cls, a = e.ptr(p + e.O["ObjectProperty_Class"]), ("obj", a and e.ptr(a))
        else:
            break
        if isinstance(a, tuple):                                  # object hop: declared class, cast if the next member needs it
            a = a[1]
            if cast:
                cls = cls_of(e, cast)
            elif a and e.cls(a) and not has_member(e, cls, segs[i].partition(":")[0].partition("[")[0]):
                cls = e.cls(a)
                segs[i - 1] = f"{tok}:{e.name(cls)}"
        addr = a
    return segs


def func_in(e, cls, name):
    c = cls
    while c:
        f = e.member(c, name, CF["Function"])
        if f:
            return f
        c = e.super(c)


def cls_of(e, name):
    return e.find(name, CF["Class"]) or sys.exit(f"class {name} not found")


RAW = dict(i="IntProperty", f="FloatProperty", d="DoubleProperty", b="ByteProperty")
INTS = ("IntProperty", "UInt32Property", "Int64Property", "UInt64Property", "Int16Property", "UInt16Property", "Int8Property",
        "ByteProperty", "EnumProperty")
_label = itertools.count(1)


def split_path(path):
    """Path segments: dots inside [] / {} do not split ("Stats{Health.Max}" stays one segment)."""
    out, depth, cur = [], 0, ""
    for ch in path:
        depth += ch in "[{"; depth -= ch in "]}"
        if ch == "." and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    return out + [cur]


def cell(cells, default, vt="Integer", size=4):
    """An editable trainer cell (index, key or call argument): its label; main() turns cells into GetMem + Value entries.
    vt None = hidden memory (GetMem only, starts at 0): caches a name lookup for the rest of the session."""
    if cells is None:
        sys.exit("$n cells and name keys are only possible in cheats (not in --fields / --find)")
    cells.append((f"Cell{len(cells) + 1}", default, vt, size))
    return cells[-1][0]


def cached(cells, size, lookup, reg, fail):
    """asm: `lookup` (lines that leave the result in reg, or jump to fail) only while the hidden cell is still 0; the name routine walks
    the whole pool and the hook runs thousands of times per frame."""
    c, L = cell(cells, 0, None, size), f"h{next(_label)}"
    return [f"mov {reg}, {'dword' if size == 4 else 'qword'} ptr [{c}]", f"test {reg}, {reg}", f"jnz {L}"] + lookup + \
           [f"mov {'dword' if size == 4 else 'qword'} ptr [{c}], {reg}", f"{L}:"]


def prop_align(e, p):
    k, size = e.ffield_class_name(p), e.prop_size(p) or 8
    if k == "StructProperty":
        return e.i32(e.ptr(p + e.O["StructProperty_Struct"]) + e.O["UStruct_MinAlignment"]) or 8
    if k in ("NameProperty", "WeakObjectProperty"):
        return 4
    return size if size in (1, 2, 4, 8) else 8


def enum_value(e, p, text):
    """Number of an enum member for an enum / byte property ("Wood", "EItem::Wood") or a plain number."""
    try:
        return int(text, 0)
    except ValueError:
        pass
    k = e.ffield_class_name(p)
    en = e.ptr(p + e.O["EnumProperty_Base"] + 8) if k == "EnumProperty" else e.ptr(p + e.O["ByteProperty_Enum"]) if k == "ByteProperty" else None
    if en:
        arr, num = e.u64(en + e.O["UEnum_Names"]), e.i32(en + e.O["UEnum_Names"] + 8) or 0
        for i in range(min(num, 4096)):
            n = e.fname(arr + i * e.enum_stride) or ""
            if n.lower() == text.lower() or n.lower().endswith("::" + text.lower()):
                return e.val(arr + i * e.enum_stride + (0x10 if e.case_preserving else 8), "<q") if not e.enum_names_only else i
    sys.exit(f"{text}: not a number{' and not a member of ' + e.name(en) if en else ''}")


_names = {}


def name_index(e, text):
    """FName (index, number) of a string in the dump's name pool: "Wood" -> (idx, 0), "Wood_3" -> (idx of Wood, 4)."""
    if not _names:
        p = e.pool
        for b in range(p.current_block + 1):
            blk = e.d.u64(p.addr + 0x10 + b * 8)
            data = e.d.read(blk, p.cursor if b == p.current_block else (1 << p.block_bits) * p.stride) or b""
            i = 0
            while i + 2 <= len(data):
                hd = int.from_bytes(data[i:i + 2], "little")
                n = (hd >> 6) * (2 if hd & 1 else 1)
                if not n:
                    break
                s = data[i + 2:i + 2 + n].decode("utf-16-le" if hd & 1 else "latin-1", "replace")
                _names.setdefault(s.lower(), (b << p.block_bits) | (i // p.stride))
                i += (2 + n + p.stride - 1) & -p.stride
    if text.lower() in _names:
        return _names[text.lower()], 0
    m = re.fullmatch(r"(.*)_(\d+)", text)
    if m and m[1].lower() in _names and not m[2].startswith("0"):
        return _names[m[1].lower()], int(m[2]) + 1
    sys.exit(f"{text}: no such name in this dump")


def map_parts(e, cls, name):
    """TMap / TSet member `name`: (property, key prop, value prop, value offset in the pair, element stride). TSet: value = key.
    Memory (measured, RuneScape): TSparseArray {Data, Num, Max; TBitArray {Inline[4], Secondary*, NumBits, MaxBits}; FirstFree, NumFree},
    element = pair + {HashNextId, HashIndex}, rounded to the pair's alignment."""
    p = member(e, cls or sys.exit(f"{name}: class unknown after a raw hop"), name)
    kind = e.ffield_class_name(p)
    if kind == "MapProperty":
        kp, vp = e.ptr(p + e.O["MapProperty_Base"]), e.ptr(p + e.O["MapProperty_Base"] + 8)
    elif kind == "SetProperty":
        kp = vp = e.ptr(p + e.O["SetProperty_ElementProp"])
    else:
        sys.exit(f"{name} is {kind}, {{key}} needs a TMap or TSet")
    up = lambda n, a: -(-n // a) * a
    ka = prop_align(e, kp)
    voff = 0 if vp == kp else up(e.prop_size(kp), prop_align(e, vp))
    pa = max(ka, prop_align(e, vp))
    return p, kp, vp, voff, up(up(voff + e.prop_size(vp), pa) + 8, max(pa, 4))


def map_hop(e, cls, name, key, cast, reg, fail, cells):
    """{key} hop: scan the allocated elements for the key (int / enum member / FName / $n cell), reg -> the value (object pointer
    loaded; struct or number: its address). Clobbers rdx, r8-r11."""
    O = e.O
    p, kp, vp, voff, stride = map_parts(e, cls, name)
    kk, ks, L = e.ffield_class_name(kp), e.prop_size(kp), f"m{next(_label)}"
    code, num = [], None
    if key.startswith("$"):
        code.append(f"mov r8d, dword ptr [{cell(cells, int(key[1:] or 0, 0))}]")
    elif kk == "NameProperty":
        idx, num = name_index(e, key)
        import ueitems_tlh
        hdr, h = ueitems_tlh.name_ref(e, idx)
        code += cached(cells, 4, ["push rax", "push rcx", "push rsi", "push rdi", f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", "call ucname",
                                 "mov r8d, r11d", "pop rdi", "pop rsi", "pop rcx", "pop rax", "cmp r8d, -1", f"je {fail}"], "r8d", fail)
    elif kk in INTS:
        code.append(f"movabs r8, {enum_value(e, kp, key) & 0xFFFFFFFFFFFFFFFF:#x}")
    else:
        sys.exit(f"{name}{{{key}}}: {kk} keys are not supported (numbers, enum members, names, $n)")
    code += [f"lea rdx, [{reg}+{e.prop_offset(p):#x}]", "mov r9, [rdx+0x20]", "test r9, r9", f"jnz {L}b", "lea r9, [rdx+0x10]", f"{L}b:",
             "mov r10d, dword ptr [rdx+8]", "mov r11, [rdx]", "test r11, r11", f"jz {fail}", "xor edx, edx",
             f"{L}l:", "cmp edx, r10d", f"jge {fail}", "bt dword ptr [r9], edx", f"jnc {L}n"]
    if kk == "NameProperty":
        code.append("cmp dword ptr [r11], r8d")
        if O["FName_Number"] >= 0:
            code += [f"jne {L}n", f"cmp dword ptr [r11+{O['FName_Number']:#x}], {num}"]
    else:
        code.append({1: "cmp byte ptr [r11], r8b", 2: "cmp word ptr [r11], r8w", 4: "cmp dword ptr [r11], r8d"}.get(ks, "cmp qword ptr [r11], r8"))
    code += [f"je {L}f", f"{L}n:", "inc edx", f"add r11, {stride:#x}", f"jmp {L}l", f"{L}f:"]
    vk = e.ffield_class_name(vp)
    if vk in ("ObjectProperty", "ClassProperty"):
        nxt = cls_of(e, cast) if cast else e.ptr(vp + O["ObjectProperty_Class"])
        return code + [f"mov {reg}, [r11+{voff:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt, e.prop_offset(p) + 0x50, 0, True
    code.append(f"lea {reg}, [r11+{voff:#x}]")
    nxt = e.ptr(vp + O["StructProperty_Struct"]) if vk == "StructProperty" else ("leaf", vp)     # a number: the path ends here
    return code, nxt, e.prop_offset(p) + 0x50, 0, False


def raw_leaf(seg):
    """"@0xd8:i" (from --addr, an unreflected C++ field) -> (offset, kind); type i/f/d/b, default i."""
    off, _, t = seg[1:].partition(":")
    return int(off, 0), RAW.get(t or "i") or sys.exit(f"{seg}: type must be one of {'/'.join(RAW)}")


def player_pawn(e, pawn_off):
    """The pawn a live PlayerController owns in the dump (its class = most-derived start for property paths)."""
    pc = e.find("PlayerController", CF["Class"])
    def is_pc(c):
        while c:
            if c == pc:
                return True
            c = e.super(c)
    for i, o in e.arr:
        if not (e.flags(o) or 0) & ueobj.RF_ClassDefaultObject and is_pc(e.cls(o)):
            pawn = e.ptr(o + pawn_off)
            if pawn and e.cls(pawn):
                return pawn


TYPES = dict(int=("IntProperty", "UInt32Property", "Int64Property"), float=("FloatProperty",), double=("DoubleProperty",),
             bool=("BoolProperty",), byte=("ByteProperty", "Int8Property"))


def word_match(word):
    """--find word: substring of the member name with the struct names it sits in; with * a pattern over one name
    (heal* = starts with heal, *hp = ends with hp, max*hp), so "hp" no longer hits every "Ship"."""
    w = word.lower()
    if "*" not in w:
        return lambda local, name: w in (local + name).lower()
    rx = re.compile(".*".join(map(re.escape, w.split("*"))) + "$")
    return lambda local, name: any(rx.match(x.split("[")[0]) for x in [name.lower()] + local.lower().split(".") if x)


def find_paths(e, roots, word, depth=4, addrs=None, kinds=None):
    """BFS over the live pawn and World (roots = [(object, path prefix)]): (path, kind, dump value) for every bool/number member whose name, or the name of a struct
    it sits in, contains `word` (object names earlier in the path don't count: that would list whole components).
    With `addrs` (addresses from a scanner, same dump/session) members whose bytes cover one of them match instead.
    Object hops use the instance's real class (":Class" downcast when it differs from the declared one)."""
    match, hits, raw, hit_addrs = word_match(word), [], {}, set()
    seen, todo = {o for o, _ in roots}, [(o, e.cls(o), pre, "") for o, pre in roots]
    for _ in range(depth + 1):
        nxt = []
        for addr, st, path, local in todo:                       # local = struct names since the last object hop
            for t in addrs or ():                                # inside this object/struct but maybe in an unreflected
                if addr <= t < addr + (e.struct_size(st) or 0) and t not in raw:      # C++ field: first (shortest) wins
                    raw[t] = (f"{path}@{t - addr:#x}", e.val(t, "<i"), e.val(t, "<f"))
            c = st
            while c:
                for p in e.properties(c):
                    objs, name, kind = [], e.ffield_name(p) or "?", e.ffield_class_name(p)
                    a, full = addr + e.prop_offset(p), path + name
                    if kind in FMT or kind == "BoolProperty":
                        cov = [t for t in addrs or () if a <= t < a + (e.prop_size(p) or 1)]
                        hit_addrs.update(cov)
                        if (cov if addrs is not None else match(local, name)) and (not kinds or kind in kinds):
                            if kind == "BoolProperty":
                                _, boff, mask, _ = e.bool_info(p)
                                v = int(bool((e.val(a + boff, "<B") or 0) & mask))
                            else:
                                v = e.val(a, FMT[kind])
                            hits.append((full, kind, v))
                    elif kind == "StructProperty":
                        nxt.append((a, e.ptr(p + e.O["StructProperty_Struct"]), full + ".", local + name + "."))
                    elif kind == "ObjectProperty":
                        objs = [(e.ptr(a), full, e.ptr(p + e.O["ObjectProperty_Class"]))]
                    elif kind == "ArrayProperty":                # TArray<UObject*> / TArray<struct>: elements as Name[i]
                        inner = e.ptr(p + e.O["ArrayProperty_Inner"])
                        ik, data, n = e.ffield_class_name(inner), e.ptr(a), e.i32(a + 8) or 0
                        if data and ik == "ObjectProperty":
                            dc = e.ptr(inner + e.O["ObjectProperty_Class"])
                            objs = [(e.ptr(data + 8 * i), f"{full}[{i}]", dc) for i in range(min(n, 64))]
                        elif data and ik == "StructProperty":
                            st, es = e.ptr(inner + e.O["StructProperty_Struct"]), e.prop_size(inner)
                            nxt += [(data + i * es, st, f"{full}[{i}].", f"{local}{name}[{i}].") for i in range(min(n, 64))]
                    for o, seg, dc in objs:
                        rc = o and o not in seen and e.cls(o)
                        # other pawns (enemies, NPCs) are noise; from World a single pointer to one is allowed: the
                        # controlled pawn is not always the character (Moss: camera pawn, GameInstance.SharedHero = Quill)
                        if rc and e.name(rc) and (not e.isa(o, CF["Pawn"]) or seg.startswith("World.") and not seg.endswith("]")):
                            seen.add(o)
                            # array elements have no name of their own: their class names them
                            # ("FloatAttributes[3]:StaminaAttribute.BaseValue" is found by "stamina")
                            nxt.append((o, rc, seg + ("" if rc == dc else f":{e.name(rc)}") + ".",
                                        f"{e.name(rc)}." if seg.endswith("]") else ""))
                c = e.super(c)
        todo = nxt
    for t, (path, i, f) in raw.items():                          # no named member covers t: raw offset, type guessed
        if not any(a == t for a in hit_addrs):
            fl = f is not None and 1e-3 < abs(f) < 1e7           # ponytail: int/float guess from the dump value only
            hits.append((path + (":f" if fl else ":i"), "Raw:" + ("FloatProperty" if fl else "IntProperty"), f if fl else i))
    return hits


WORDS = ("damage", "heal", "health", "spend", "consume", "cost", "unlock", "give", "grant", "add", "cheat", "debug", "god", "kill",
         "money", "gold", "xp", "experience", "level", "ammo", "stamina", "cooldown", "teleport")


def object_paths(e, roots, depth=4):
    """Every object reachable from the roots (pawn, World) through object members and TArray<UObject*>: [(object, path)], shortest first."""
    seen, out, todo = {o for o, _ in roots}, list(roots), list(roots)
    for _ in range(depth):
        nxt = []
        for o, path in todo:
            c = e.cls(o)
            while c:
                for p in e.properties(c):
                    k, a, n = e.ffield_class_name(p), o + e.prop_offset(p), e.ffield_name(p) or "?"
                    objs = [(e.ptr(a), f"{path}.{n}")] if k == "ObjectProperty" else []
                    if k == "ArrayProperty" and e.ffield_class_name(e.ptr(p + e.O["ArrayProperty_Inner"])) == "ObjectProperty":
                        data, cnt = e.ptr(a), e.i32(a + 8) or 0
                        objs = [(e.ptr(data + 8 * i), f"{path}.{n}[{i}]") for i in range(min(cnt, 8))] if data else []
                    for t, tp in objs:
                        if t and t not in seen and e.cls(t) and e.name(e.cls(t)) and not e.isa(t, CF["Pawn"]):
                            seen.add(t); out.append((t, tp)); nxt.append((t, tp))
                c = e.super(c)
        todo = nxt
    return out


def list_methods(e, pawn, world, word):
    """--methods [word]: functions whose name has the word (default: the usual cheat targets), on classes with a live object reachable
    from the pawn / World first, static library functions next; each with a ready call: spec and, for C++ functions, a patch: spec."""
    words = [word.lower()] if word else WORDS
    roots = [(pawn, "Pawn")] + ([(world, "World")] if world else [])
    objs = object_paths(e, roots)
    O, rows = e.O, []
    for _, fn in e.arr:
        n = e.name(fn) or ""
        if not e.isa(fn, CF["Function"]) or "__DelegateSignature" in n or not any(w.strip("*") in n.lower() for w in words):
            continue
        owner = e.outer(fn)
        if not owner or not e.isa(owner, CF["Class"]):
            continue
        fl = e.val(fn + O["UFunction_FunctionFlags"], "<I") or 0
        path = e.name(owner) if fl & 0x2000 else None             # FUNC_Static: called on the default object
        if not path:
            for o, p in objs:
                c = e.cls(o)
                while c and c != owner:
                    c = e.super(c)
                if c:
                    path = p
                    break
        params = [(p, e.val(p + O["Property_PropertyFlags"], "<Q") or 0) for p in e.properties(fn)]
        inputs = [p for p, f in params if not f & 0x400 and (not f & 0x100 or f & 0x8000002)]
        rp = next((p for p, f in params if f & 0x400), None)

        def arg(p):
            k, pn = e.ffield_class_name(p), e.ffield_name(p) or ""
            if k == "BoolProperty":
                return "true"
            if k in ("ObjectProperty", "ClassProperty", "InterfaceProperty"):
                return "World" if "worldcontext" in pn.lower() else f"<{e.name(e.ptr(p + O['ObjectProperty_Class'])) or 'object'}>"
            return "$0" if k in INTS or k in ("FloatProperty", "DoubleProperty") else f"<{k[:-8]}>"
        sig = ", ".join(f"{e.ffield_class_name(p)[:-8]} {e.ffield_name(p)}" for p in inputs)
        ret = e.ffield_class_name(rp)[:-8] if rp else "void"
        specs = [f"call:{path or '<path to ' + e.name(owner) + '>'}.{n}({', '.join(arg(p) for p in inputs)})"]
        if fl & 0x400 and impl_of(e, fn)[0]:                     # patch: only where a C++ body is found
            mode = {"void": "noop", "Bool": "true"}.get(ret, "0" if rp and e.ffield_class_name(rp) in INTS + ("FloatProperty", "DoubleProperty") else None)
            if mode:
                specs.append(f"patch:{e.name(owner)}.{n}={mode}")
        rows.append((0 if path and not fl & 0x2000 else 1 if path else 2, f"{e.name(owner)}::{n}({sig}) -> {ret}" +
                     (" [static]" if fl & 0x2000 else "") + ("" if fl & 0x400 else " [Blueprint]"), specs))
    rows.sort(key=lambda r: r[0])
    for _, head, specs in rows[:300]:
        print(head)
        for s in specs:
            print(f"    {s}")
    if len(rows) > 300:
        print(f"... {len(rows) - 300} more, give a narrower word")


def list_fields(e, pawn, world, path):
    """Members of the object/struct at `path` (pawn-relative or World.-rooted, same syntax as cheat paths) in the dump:
    (path.member, kind, value or a short hint for objects/structs/arrays to go deeper)."""
    toks = [t for t in path.split(".") if t]
    root = world if toks[:1] == ["World"] else pawn
    a, cls, segs = root, e.cls(root), toks[:1] if root == world else []
    for tok in toks[len(segs):]:
        name, _, cast = tok.partition(":")
        name, _, idx = name.rstrip("]").partition("[")
        p = member(e, cls, name)
        kind, a, seg, decl = e.ffield_class_name(p), a + e.prop_offset(p), name + (f"[{idx}]" if idx else ""), p
        if idx:
            inner, i = e.ptr(p + e.O["ArrayProperty_Inner"]), int(idx.lstrip("$") or 0, 0)
            ik, data, n = e.ffield_class_name(inner), e.ptr(a), e.i32(a + 8) or 0
            if kind != "ArrayProperty" or not data or i >= n:
                sys.exit(f"{tok}: not an array or index out of range (Num {n})")
            if ik == "StructProperty":
                a, cls = data + i * e.prop_size(inner), e.ptr(inner + e.O["StructProperty_Struct"])
                segs.append(seg)
                continue
            a, kind, decl = data + 8 * i, ik, inner
        if kind == "StructProperty":
            cls = e.ptr(p + e.O["StructProperty_Struct"])
        elif kind == "ObjectProperty":
            a = e.ptr(a) or sys.exit(f"{tok}: null pointer in the dump")
            cls = e.cls(a)                                        # real class of the instance
            if cls != e.ptr(decl + e.O["ObjectProperty_Class"]):  # paths into its members need the downcast
                seg += f":{e.name(cls)}"
        else:
            sys.exit(f"{tok} is {kind}, cannot go into it")
        segs.append(seg)
    rows, c, prefix = [], cls, "".join(s + "." for s in segs)
    while c:
        for p in e.properties(c):
            name, kind, at = e.ffield_name(p) or "?", e.ffield_class_name(p), a + e.prop_offset(p)
            if kind == "BoolProperty":
                _, boff, mask, _ = e.bool_info(p)
                v = int(bool((e.val(at + boff, "<B") or 0) & mask))
            elif kind in FMT:
                v = e.val(at, FMT[kind])
            elif kind == "ObjectProperty":
                o = e.ptr(at)
                v = f"-> {e.name(e.cls(o))}" if o and e.cls(o) else "null"
            elif kind == "StructProperty":
                v = f"{{{e.name(e.ptr(p + e.O['StructProperty_Struct']))}}}"
            elif kind == "ArrayProperty":
                v = f"[{e.i32(at + 8) or 0}] of {e.ffield_class_name(e.ptr(p + e.O['ArrayProperty_Inner']))}"
            else:
                v = ""
            rows.append((prefix + name, kind, v))
        c = e.super(c)
    return rows


def native_impl(e, fn):
    """C++ implementation behind a native UFunction: the exec thunk does P_FINISH then tail-jumps (or calls) it."""
    import capstone
    thunk = e.u64(fn + e.O["UFunction_ExecFunction"])
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    for ins in md.disasm(e.d.read(thunk, 0x60), thunk):
        if ins.mnemonic in ("jmp", "call") and ins.op_str.startswith("0x"):
            return int(ins.op_str, 16)
        if ins.mnemonic == "ret":
            break
    sys.exit(f"{e.name(fn)}: exec thunk at base+{thunk - e.d.base:#x} has no jmp/call to an implementation (inlined?)")


def unique_aob(e, addr, lo=16, hi=96):
    exe = e.d.read(e.d.base, e.d.exe_end - e.d.base)
    for n in range(lo, hi, 4):
        pat = e.d.read(addr, n)
        if exe.count(pat) == 1:
            return pat
    sys.exit(f"no unique AOB at base+{addr - e.d.base:#x}")


def site_instructions(e, addr):
    """Whole instructions covering >= 5 bytes at the injection site (same rule as the PRX), as asm text."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out, n = [], 0
    for ins in md.disasm(e.d.read(addr, 32), addr):
        if not out and ins.mnemonic in ("jmp", "call"):
            sys.exit(f"ProcessEvent already starts with {ins.mnemonic} {ins.op_str}: dump taken with a cheat hook active, "
                     "redump with all cheats off")
        if "rip" in ins.op_str:
            sys.exit(f"site instruction is rip-relative ({ins.mnemonic} {ins.op_str}); pick another hook")
        out.append(f"{ins.mnemonic} {ins.op_str}".strip()); n += ins.size
        if n >= 5:
            return out


def tlh_version(v):
    """dump.json version ("01.15", "01.102.999") -> TLH Version as uTrainer.SetVersion reads it: keep the first dot,
    drop the others, then cut (never round: 1.109 must not become 1.11, a real later update) to 2 decimals."""
    if not v:
        return ""
    major, _, rest = v.partition(".")
    return f"{int(major)}.{(rest.replace('.', '') + '00')[:2]}"


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def hop_code(e, cls, tok, reg, fail, cells=None):
    """Assembly for one object/struct hop in `reg`; returns (lines, class/struct after the hop or None, bytes read from
    reg (0 = none), bytes reg was advanced by (inline struct), whether reg now holds a UObject)."""
    m = re.fullmatch(r"(\w+)\{(.+)\}(?::(\w+))?", tok)
    if m:                                                         # TMap / TSet element by key
        return map_hop(e, cls, m[1], m[2], m[3], reg, fail, cells)
    if tok.startswith("weak:"):                                  # TWeakObjectPtr {int32 Index; int32 Serial} -> GObjects item
        off = int(tok[5:], 0)
        return [f"movsxd rdx, dword ptr [{reg}+{off:#x}]", "test rdx, rdx", f"js {fail}",
                "mov r8, [GObjects]", "test r8, r8", f"jz {fail}", "shl rdx, 4", "add r8, rdx",
                f"mov r9d, dword ptr [{reg}+{off + 4:#x}]", "cmp dword ptr [r8+0xc], r9d", f"jne {fail}",
                "test byte ptr [r8+0xb], 0x30", f"jnz {fail}",                       # pending kill / unreachable
                f"mov {reg}, [r8]", f"test {reg}, {reg}", f"jz {fail}"], None, off + 8, 0, True
    if tok.startswith("+"):
        return [f"mov {reg}, [{reg}+{int(tok, 0):#x}]", f"test {reg}, {reg}", f"jz {fail}"], None, int(tok, 0) + 8, 0, False
    name, _, cast = tok.partition(":")                            # "AnimScriptInstance:BendPlayerAnimInstance" = downcast
    name, _, idx = name.rstrip("]").partition("[")                # "SpawnedAttributes[3]" = TArray element
    p = member(e, cls or sys.exit(f"{tok}: class unknown after a raw hop, use +0xNN"), name)
    kind, off = e.ffield_class_name(p), e.prop_offset(p)
    if idx:
        # ponytail: fixed index, taken from the dump; arrays filled in a different order need a class check at runtime
        inner = e.ptr(p + e.O["ArrayProperty_Inner"])
        ik = e.ffield_class_name(inner) if kind == "ArrayProperty" else None
        if ik not in ("ObjectProperty", "StructProperty"):
            sys.exit(f"{name} is {kind}, [i] needs a TArray of object pointers or structs")
        es = e.prop_size(inner) if ik == "StructProperty" else 8
        if idx.startswith("$"):                                   # [$n]: index from an editable trainer cell, default n
            code = [f"movsxd rdx, dword ptr [{cell(cells, int(idx[1:] or 0, 0))}]", "test rdx, rdx", f"js {fail}",
                    f"cmp edx, dword ptr [{reg}+{off + 8:#x}]", f"jge {fail}",
                    f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}", f"imul rdx, rdx, {es:#x}", f"add {reg}, rdx"]
            elem = 0                                              # reg already points at the element
        else:
            elem = int(idx, 0) * es
            code = [f"cmp dword ptr [{reg}+{off + 8:#x}], {int(idx, 0)}", f"jle {fail}",               # TArray {Data, Num, Max}
                    f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}"]
        if ik == "StructProperty":                                # elements inline: Data + i * ElementSize
            return code + ([f"add {reg}, {elem:#x}"] if not idx.startswith("$") else []), e.ptr(inner + e.O["StructProperty_Struct"]), off + 0x10, 0, False
        nxt = cls_of(e, cast) if cast else e.ptr(inner + e.O["ObjectProperty_Class"])
        return code + [f"mov {reg}, [{reg}+{elem:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt, off + 0x10, 0, True
    if kind == "StructProperty":                                  # inline struct: no pointer, just advance (talixme)
        return [f"add {reg}, {off:#x}"], e.ptr(p + e.O["StructProperty_Struct"]), 0, off, False
    if kind not in ("ObjectProperty", "WeakObjectProperty"):
        sys.exit(f"{name} is {kind}, expected an object pointer or struct")
    nxt = cls_of(e, cast) if cast else e.ptr(p + e.O["ObjectProperty_Class"])
    return [f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt, off + 8, 0, True


def path_code(e, cls, toks, reg, fail, code, cells=None):
    """Hops for toks from the UObject in reg, appended to code. Before the first access into an object a guard is
    inserted (see close): menus can swap the pawn for a small menu pawn (Mortal Shell BP_EquipmentMenuPawn_*_C),
    and reading a component offset behind its end gives garbage pointers. Returns (cls, reg, pend)."""
    pend = [len(code), reg, 0]                                    # guard slot, object register, struct offset inside it
    for tok in toks:
        if reg == "rax":
            code.append("mov rcx, rax"); reg = "rcx"
        lines, cls, end, adv, fresh = hop_code(e, cls, tok, reg, fail, cells)
        if end:
            close(e, code, pend, end, fail); pend = None
        elif pend:
            pend[2] += adv                                        # inline struct: still inside the same object
        code += lines
        if fresh:
            pend = [len(code), reg, 0]
    return cls, reg, pend


def close(e, code, pend, end, fail):
    """Insert the guard for the pending object: 8-aligned, has a class, and that class is at least `end` bytes."""
    if pend:
        i, reg, base = pend
        code[i:i] = [f"test {reg}, 7", f"jnz {fail}", f"mov rdx, [{reg}+{e.O['UObject_Class']:#x}]", "test rdx, rdx",
                     f"jz {fail}", f"cmp dword ptr [rdx+{e.O['UStruct_Size']:#x}], {base + end:#x}", f"jl {fail}"]


# ---- patch: and call: cheats (the Unity il2cheat kinds on the UE side) ------------------------------------------------------
_helpers = None


def thunk_calls(e, t):
    """Calls an exec thunk makes, in order: direct targets (int) and virtual slots ("v", disp); stops at ret or a tail jmp."""
    import capstone
    md, out = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64), []
    for ins in md.disasm(e.d.read(t, 0x400) or b"", t):
        if ins.mnemonic in ("call", "jmp") and ins.op_str.startswith("0x"):
            tg = int(ins.op_str, 16)
            if ins.mnemonic == "jmp" and t <= tg < t + 0x400:   # branch inside the thunk
                continue
            out.append(tg)
            if ins.mnemonic == "jmp":
                break
        elif ins.mnemonic == "call":
            m = re.fullmatch(r"qword ptr \[r\w+(?: \+ (0x[0-9a-f]+))?\]", ins.op_str)
            out.append(("v", int(m[1] or "0", 16)) if m else ("i", ins.op_str))
        elif ins.mnemonic == "ret":
            break
    return out


def impl_of(e, fn):
    """C++ implementation of a native UFunction for patch:: the last call of its exec thunk that is not a helper every thunk uses
    (parameter Step, FName/FString conversion, FMemory::Free: called from more than 8 thunks / virtual slots from more than 50).
    A virtual call is resolved through the owner class's default object (that class's override). None if the body is inlined."""
    global _helpers
    O = e.O
    if _helpers is None:
        import collections
        cnt = collections.Counter()
        for _, f in e.arr:
            if e.isa(f, CF["Function"]) and (e.val(f + O["UFunction_FunctionFlags"], "<I") or 0) & 0x400:
                t = e.u64(f + O["UFunction_ExecFunction"])
                if t and e.d.base <= t < e.d.exe_end:
                    cnt.update(set(c for c in thunk_calls(e, t) if not isinstance(c, tuple) or c[0] == "v"))
        _helpers = {c for c, n in cnt.items() if n > (50 if isinstance(c, tuple) else 8)}
    t, owner = e.u64(fn + O["UFunction_ExecFunction"]), e.outer(fn)
    # a virtual body is a slot the owner class adds: behind its base class's vtable (slots below are UObject / base virtuals that an
    # inlined body calls; RuneScape HealthComponent::DecreaseHealth calls slots 0x58-0x88 of UObject: patching one hits every object)
    lo = vtable_size(e, e.super(owner)) if e.super(owner) else 0
    cands = [c for c in thunk_calls(e, t) if c not in _helpers and (not isinstance(c, tuple) or c[0] == "v" and c[1] >= lo)]
    if not cands:
        return None, "inlined"
    c, note = cands[-1], f" (last of {len(cands)} candidates, check it)" if len(cands) > 1 else ""
    if isinstance(c, int):
        return c, "direct" + note
    cdo = e.ptr(owner + O["UClass_ClassDefaultObject"])
    impl = cdo and e.u64(e.u64(cdo) + c[1])
    return (impl, f"virtual +{c[1]:#x}{note}") if impl and e.d.base <= impl < e.d.exe_end else (None, "virtual, no default object")


def vtable_size(e, cls):
    """Bytes of the vtable of the class's default object (entries until the first non-code pointer)."""
    cdo = e.ptr(cls + e.O["UClass_ClassDefaultObject"])
    vt, n = cdo and e.u64(cdo), 0
    while vt and e.d.base <= (e.u64(vt + n) or 0) < e.d.exe_end:
        n += 8
    return n


def ret_param(e, fn):
    return next((p for p in e.properties(fn) if (e.val(p + e.O["Property_PropertyFlags"], "<Q") or 0) & 0x400), None)


def patch_asm(e, fn, mode, who):
    """Assembly that returns `mode` from the function's C++ body (TLH Assembly entry at its start)."""
    rp = ret_param(e, fn)
    k = rp and e.ffield_class_name(rp)
    if mode == "noop":
        return "ret" if not rp else sys.exit(f"{who}: returns {k}, 'noop' is for void functions; use true/false or a number")
    if not rp:
        sys.exit(f"{who}: is void, use noop")
    if k == "BoolProperty":
        mode in ("true", "false", "1", "0") or sys.exit(f"{who}: returns bool, use true or false")
        return f"mov eax, {int(mode in ('true', '1'))}\nret"
    if k == "FloatProperty":
        return f"mov eax, {struct.unpack('<I', struct.pack('<f', float(mode)))[0]:#x}\nmovd xmm0, eax\nret"
    if k == "DoubleProperty":
        return f"movabs rax, {struct.unpack('<Q', struct.pack('<d', float(mode)))[0]:#x}\nmovq xmm0, rax\nret"
    if k in INTS:
        v = enum_value(e, rp, mode)
        return (f"movabs rax, {v & 0xFFFFFFFFFFFFFFFF:#x}" if e.prop_size(rp) == 8 else f"mov eax, {v & 0xFFFFFFFF:#x}") + "\nret"
    if k in ("ObjectProperty", "ClassProperty") and mode in ("null", "0"):
        return "xor eax, eax\nret"
    sys.exit(f"{who}: returns {k}, not patchable (numbers, bool, enum, null object)")


def obj_root(e, text, world, pawn):
    """Object path of a call: (root "world"/"pawn", hops, dump object of the root). World./GWorld. = from GWorld, Pawn. or no
    prefix = from the controlled pawn, PC. = the local PlayerController."""
    toks = split_path(text)
    if toks[0] == "GWorld":
        toks[0] = "World"
    if toks[0] == "PC":
        toks = ["World", "OwningGameInstance", "LocalPlayers[0]", "PlayerController"] + toks[1:]
    if toks[0] == "World":
        return "world", toks[1:], world
    return "pawn", toks[1:] if toks[0] == "Pawn" else toks, pawn


def split_args(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch in "[({"; depth -= ch in "])}"
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    return out + [cur.strip()] if cur.strip() else out


CELL_OF = dict(FloatProperty=("Single", 4), DoubleProperty=("Double", 8), BoolProperty=("Boolean", 1))


def compile_call(e, k, target, world, pawn, world_cls, pawn_cls, cells):
    """call:Path.Function(args): ProcessEvent(object, UFunction, params) once per switch-on, from the hook (= on the game thread).
    The object comes from its path, the UFunction is looked up by name in the object's class chain at runtime (FName indices move
    between sessions), the parameters are built in a buffer on the stack. Returns (asm lines, "world"/"pawn", description)."""
    O = e.O
    m = re.fullmatch(r"(.+)\.(\w+)\((.*)\)", target) or sys.exit(f"call:{target}: expected call:Path.Function(args)")
    head, fname, args = m[1], m[2], split_args(m[3])
    fail = f"k{k}_out"
    uses_pawn, code = False, []

    def load(text, nxt=None):
        """asm: object at `text` into rcx (jump to fail if missing) -> its class"""
        nonlocal uses_pawn
        if text == "World" or text == "GWorld":
            code.extend(["mov rcx, [GWorld]", "test rcx, rcx", f"jz {fail}"])
            return world_cls
        kind, toks, dobj = obj_root(e, text, world, pawn)
        toks = auto_cast(e, dobj, toks + [nxt or "?"])[:-1] if toks else toks
        uses_pawn |= kind == "pawn"
        code.extend(["mov rcx, [GWorld]" if kind == "world" else "mov rcx, rbx", "test rcx, rcx", f"jz {fail}"])
        cls, reg, pend = path_code(e, world_cls if kind == "world" else pawn_cls, toks, "rcx", fail, code, cells)
        close(e, code, pend, 8, fail)                             # 8-aligned and has a class
        return cls

    def by_name(o):                                               # asm: rax = the live object named like dump object o (class, default object)
        import ueitems_tlh
        hdr, h = ueitems_tlh.name_ref(e, e.i32(o + O["UObject_Name"]))
        code.extend(cached(cells, 8, [f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", "call ucname", "cmp r11d, -1", f"je {fail}",
                                      f"mov ecx, {e.i32(o + O['UObject_Name'] + 4) or 0}", "call ucobj", "test rax, rax", f"jz {fail}"], "rax", fail))

    static = len(split_path(head)) == 1 and head not in ("World", "GWorld", "Pawn", "PC") and not has_member(e, pawn_cls, head) and e.find(head, CF["Class"])
    tcls = e.find(head, CF["Class"]) if static else None
    if not static:                                                # the class of the object from the dump (parameter layout);
        n0, saved, code = len(cells), code, []                    # the code for `this` is emitted last
        tcls = load(head, fname)
        code = saved
        del cells[n0:]
    fn = func_in(e, tcls, fname) if tcls else None
    fn or sys.exit(f"call:{target}: {e.name(tcls) if tcls else head} has no function {fname}")
    params = [(p, e.val(p + O["Property_PropertyFlags"], "<Q") or 0) for p in e.properties(fn)]
    inputs = [p for p, fl in params if not fl & 0x400 and (not fl & 0x100 or fl & 0x8000002)]   # not Return; Out only when Ref/Const
    sig = ", ".join(f"{e.ffield_class_name(p)[:-8]} {e.ffield_name(p)}" for p in inputs)
    if len(args) != len(inputs):
        sys.exit(f"call:{target}: {fname} takes {len(inputs)} argument(s): ({sig})")
    size = e.struct_size(fn) or 0
    frame = max(16, (size + 15) & ~15)
    body = [f"mov qword ptr [rsp+{o:#x}], 0" for o in range(0, (size + 7) & ~7, 8)]
    code = body
    for p, a in zip(inputs, args):
        kind, o, n = e.ffield_class_name(p), e.prop_offset(p), e.prop_size(p)
        who = f"call:{target}: {e.ffield_name(p)}"
        if kind in ("ObjectProperty", "ClassProperty", "InterfaceProperty"):
            if a != "null":
                ac = e.find(a, CF["Class"]) if kind == "ClassProperty" else None
                if ac and not a.startswith(("World", "GWorld", "Pawn", "PC")):
                    by_name(ac); code.append(f"mov qword ptr [rsp+{o:#x}], rax")
                else:
                    load(a); code.append(f"mov qword ptr [rsp+{o:#x}], rcx")
            continue
        if kind == "BoolProperty":
            _, boff, mask, _ = e.bool_info(p)
            o += boff
        if a.startswith("$"):                                     # editable cell, default = number / enum member / true
            vt, cs = CELL_OF.get(kind) or (("Byte", 1) if n == 1 else ("Integer", 4))
            d = a[1:] or "0"
            if kind in INTS:
                d = enum_value(e, p, d)
            elif kind == "BoolProperty":
                d = int(d.lower() in ("1", "true"))
            c = cell(cells, d, vt, cs)
            r = {1: "al", 2: "ax", 4: "eax", 8: "rax"}
            code += ([f"movsxd rax, dword ptr [{c}]"] if n == 8 and cs == 4 else [f"mov {r[cs]}, {'byte' if cs == 1 else 'dword' if cs == 4 else 'qword'} ptr [{c}]"])
            if kind == "BoolProperty":
                code += ["test al, al", "setne al"]
            code.append(f"mov {'byte' if n == 1 else 'word' if n == 2 else 'dword' if n == 4 else 'qword'} ptr [rsp+{o:#x}], {r[n if n in r else 4]}")
            continue
        if kind == "BoolProperty":
            a.lower() in ("true", "false", "1", "0") or sys.exit(f"{who}: bool, give true or false")
            code.append(f"mov byte ptr [rsp+{o:#x}], {int(a.lower() in ('true', '1'))}")
        elif kind in ("FloatProperty", "DoubleProperty"):
            bits = struct.unpack("<I", struct.pack("<f", float(a)))[0] if n == 4 else struct.unpack("<Q", struct.pack("<d", float(a)))[0]
            code += [f"mov dword ptr [rsp+{o:#x}], {bits:#x}"] if n == 4 else [f"movabs rax, {bits:#x}", f"mov qword ptr [rsp+{o:#x}], rax"]
        elif kind in INTS:
            v = enum_value(e, p, a) & ((1 << 8 * n) - 1)
            code += [f"movabs rax, {v:#x}", f"mov qword ptr [rsp+{o:#x}], rax"] if n == 8 else \
                    [f"mov {'byte' if n == 1 else 'word' if n == 2 else 'dword'} ptr [rsp+{o:#x}], {v:#x}"]
        else:
            sys.exit(f"{who}: {kind} parameters are not supported (numbers, bool, enum, objects, $n)")
    # `this`, then the UFunction by name through its class chain (as ueitems_tlh.give_routine)
    if static:
        by_name(e.find("Default__" + e.name(tcls)) or sys.exit(f"{head}: no default object in the dump")); code.append("mov rbx, rax")
    else:
        load(head, fname); code.append("mov rbx, rcx")
    import ueitems_tlh
    hdr, h = ueitems_tlh.name_ref(e, e.i32(fn + O["UObject_Name"]))
    L = f"k{k}"
    code += cached(cells, 8, [f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", "call ucname", "cmp r11d, -1", f"je {fail}",
                              f"mov r13, [rbx+{O['UObject_Class']:#x}]", "mov r14d, 0x4000",
                              f"{L}cl:", "test r13, r13", f"jz {fail}", f"mov r12, [r13+{O['UStruct_Children']:#x}]",
                              f"{L}fn:", "test r12, r12", f"jz {L}sp", "dec r14d", f"jz {fail}",
                              f"cmp dword ptr [r12+{O['UObject_Name']:#x}], r11d", f"je {L}ok", f"mov r12, [r12+{O['UField_Next']:#x}]", f"jmp {L}fn",
                              f"{L}sp:", f"mov r13, [r13+{O['UStruct_SuperStruct']:#x}]", f"jmp {L}cl", f"{L}ok:"], "r12", fail)
    code += [f"mov byte ptr [Flag{k}], 2", "mov rdi, rbx", "mov rsi, r12", "mov rdx, rsp", "movabs rax, ProcessEvt", "call rax"]
    saved = (["rax"] if uses_pawn else []) + CALLER_SAVED + ["rbx", "r12", "r13", "r14", "r15"]
    pad = 8 if len(saved) % 2 == 0 else 0                         # 4 prologue pushes + saved + pad: rsp 16-aligned at the call
    out = [f"cmp byte ptr [Flag{k}], 1", f"jne k{k}_end"] + [f"push {r}" for r in saved] + \
          (["mov rbx, rax"] if uses_pawn else []) + [f"sub rsp, {frame + pad:#x}"] + code + \
          [f"{fail}:", f"add rsp, {frame + pad:#x}"] + [f"pop {r}" for r in reversed(saved)] + [f"k{k}_end:"]
    owner = e.name(e.outer(fn))
    return out, "pawn" if uses_pawn else "world", f"{owner}::{fname}({sig}) on {head}{' (static, default object)' if static else ''}"


def main(dump, out, *specs):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    hop = lambda cls, prop: e.prop_offset(member(e, cls_of(e, cls), prop))
    chain = [("World", "OwningGameInstance"), ("GameInstance", "LocalPlayers"), ("Player", "PlayerController"), ("Controller", "Pawn")]
    o_gi, o_lp, o_pc, o_pawn = (hop(c, p) for c, p in chain)
    pawn = player_pawn(e, o_pawn)
    meta = json.load(open(os.path.join(os.path.dirname(dump), "SDK", "offsets.json")))
    world = meta.get("gworld") and e.ptr(e.d.base + meta["gworld"])
    if out in ("--find", "--addr", "--fields", "--methods"):
        pawn or sys.exit("no live player pawn in this dump (dump in-game, not in a menu)")
        print(f"player pawn class in dump: {e.name(e.cls(pawn))}")
        roots = [(pawn, "")] + ([(world, "World.")] if world else [])
        kinds, specs = None, list(specs)
        if "--types" in specs:                                    # --types int,float: only these member types
            i = specs.index("--types")
            kinds = sum((TYPES.get(t.strip()) or sys.exit(f"--types: one of {', '.join(TYPES)}") for t in specs[i + 1].split(",")), ())
            del specs[i:i + 2]
        if out == "--methods":
            return list_methods(e, pawn, world, specs[0] if specs else "")
        if out == "--fields":
            rows = list_fields(e, pawn, world, specs[0] if specs else "")
        elif out == "--addr":                                     # addresses, optional last arg = depth if small
            depth = int(specs[-1]) if len(specs) > 1 and int(specs[-1], 0) < 0x100 else 6
            addrs = [int(x, 0) for x in specs if int(x, 0) >= 0x100]
            addrs or sys.exit("--addr needs at least one address (0x...)")
            rows = find_paths(e, roots, "", depth, addrs)
            if not rows:
                print(f"no member within {depth} hops of the pawn or World covers {', '.join(hex(x) for x in addrs)} "
                      "(other session's dump? value outside the pawn's objects? try uediff.py)")
                for t in addrs:
                    o = next((o for _, o in e.arr if e.cls(o) and o <= t < o + (e.struct_size(e.cls(o)) or 8)), None)
                    print(f"  {t:#x}: " + ("not inside the dump" if e.val(t, "<B") is None else
                                          f"inside {e.fullname(o)} +{t - o:#x}" if o else "not inside any UObject (heap data)"))
        else:
            rows = find_paths(e, roots, specs[0], int(specs[1]) if len(specs) > 1 else 4, kinds=kinds)
        for path, kind, v in rows:
            print(f"{path}	{kind}	{v}")
        return
    if not meta.get("gworld") or not meta.get("process_event"):
        sys.exit("offsets.json lacks gworld/process_event (rerun uegen with the console reachable)")
    info = json.load(open(dump[:-4] + ".json"))
    cusa = int(info["title_id"][4:]) if info["title_id"][4:].isdigit() else 0      # CUSA12345 / PPSA12345 -> 12345
    pe = e.d.base + meta["process_event"]
    O = e.O
    pawn_cls = e.cls(pawn) if pawn else cls_of(e, "Pawn")
    world_cls = cls_of(e, "World")
    print(f"player pawn class in dump: {e.name(pawn_cls)}")

    cheats, pawn_blocks, world_blocks, mems, syms, item_specs, cells = [], [], [], [], [], [], []
    for k, spec in enumerate(specs or DEFAULT, 1):
        name, rest = spec.split("=", 1)
        if rest.startswith("patch:"):                             # Name=patch:Class.Function=noop|true|false|<n>: TLH Assembly entry, no hook
            target, mode = rest[6:].rsplit("=", 1)
            cname, _, fname = target.rpartition(".")
            fn = func_in(e, cls_of(e, cname), fname) or sys.exit(f"{name}: {cname} has no function {fname}")
            (e.val(fn + O["UFunction_FunctionFlags"], "<I") or 0) & 0x400 or sys.exit(f"{name}: {cname}.{fname} is a Blueprint function (no C++ body to patch)")
            impl, how = impl_of(e, fn)
            impl or sys.exit(f"{name}: {cname}.{fname}: no C++ body found ({how} into its exec thunk)")
            asm = patch_asm(e, fn, mode.strip().lower(), f"{name}: {cname}.{fname}")
            aob = "-".join(f"{b:02X}" for b in unique_aob(e, impl))
            cheats.append((name, [f"      <AOB>{aob}</AOB>\n      <Type>Assembly</Type>\n      <Assembly>{xml_escape(asm)}</Assembly>"]))
            print(f"{name}: {e.name(e.outer(fn))}::{fname} body base+{impl - e.d.base:#x} ({how}) -> {asm.replace(chr(10), '; ')} (every caller)")
            continue
        path, value = rest.split("=", 1) if not rest.startswith("call:") else (rest, "")
        if path.startswith("GWorld."):                            # same root, as other tools write it
            path = path[1:]
        mems.append(f"GetMem( Flag{k}, 1 )")
        entries = [f"      <Symbol>Flag{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>Boolean</ValueType>\n      <Value>0</Value>"]
        n0 = len(cells)

        def add_cells():                                          # $n cells of this cheat: editable entries; name caches: memory only
            for lab, d, vt, size in cells[n0:]:
                mems.append(f"GetMem( {lab}, {size} )")
                if vt:
                    entries.append(f"      <Symbol>{lab}</Symbol>\n      <Type>Value</Type>\n      <ValueType>{vt}</ValueType>\n      <Value>{d}</Value>")
        if path.startswith("call:"):                              # Name=call:Path.Function(args): once per switch-on, through ProcessEvent
            code, where, text = compile_call(e, k, path[5:], world, pawn, world_cls, pawn_cls, cells)
            (pawn_blocks if where == "pawn" else world_blocks).append("\n".join(code))
            syms.append(f"RegisterSymbol( ProcessEvt, ModuleBase+{meta['process_event']:#x} )")
            add_cells()
            cheats.append((name, entries))
            print(f"{name}: call {text}")
            continue
        if path.startswith("item:"):                              # inventory rule (ueitems): "Name=item:ITEM_ID=amount" / "item:$money" / "item:$expand" (inventory to max)
            mems.append(f"GetMem( Val{k}, 4 )")
            entries.append(f"      <Symbol>Val{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>Integer</ValueType>\n      <Value>{value}</Value>")
            item_specs.append((k, path[5:], value))
            cheats.append((name, entries))
            print(f"{name}: inventory {path[5:]} = {value}")
            continue
        if path == "button":
            toks = value.split(".")
            root, cls, reg = toks[0], None, "rcx"
            code = [f"cmp byte ptr [Flag{k}], 1", f"jne b{k}_end", f"mov byte ptr [Flag{k}], 2"]      # once per switch-on
            saved = CALLER_SAVED + ["rax"] * (root == "Pawn")    # Pawn buttons run after the walk: keep the pawn in rax
            code += [f"push {r}" for r in saved]
            if root == "World":
                code += ["mov rcx, [GWorld]", "test rcx, rcx", f"jz b{k}_pop"]; cls = world_cls
            elif root == "Pawn":
                code += ["mov rcx, rax", "test rcx, rcx", f"jz b{k}_pop"]; cls = pawn_cls
            else:
                sys.exit(f"{name}: button path must start with World or Pawn")
            cls, reg, pend = path_code(e, cls, toks[1:-1], reg, f"b{k}_pop", code)
            fn = (func_in(e, cls, toks[-1]) if cls else None) or next((o for o in e.by_name.get(toks[-1].lower(), ()) if e.isa(o, CF["Function"])), None)
            if not fn:
                sys.exit(f"{name}: function {toks[-1]} not found")
            impl = native_impl(e, fn)
            close(e, code, pend, e.struct_size(cls) if cls else 8, f"b{k}_pop")      # callee may touch the whole object
            syms.append(f"RegisterSymbol( Fn{k}, ModuleBase+{impl - e.d.base:#x} )")
            pad = ["sub rsp, 8"] if len(saved) % 2 == 0 else []     # 4 prologue pushes + saved: keep rsp 16-aligned
            code += ["mov rdi, rcx"] + pad + [f"movabs rax, Fn{k}", "call rax"] + ["add rsp, 8"] * len(pad) + [f"b{k}_pop:"]
            code += [f"pop {r}" for r in reversed(saved)] + [f"b{k}_end:"]
            (pawn_blocks if root == "Pawn" else world_blocks).append("\n".join(code))
            cheats.append((name, entries))
            print(f"{name}: button -> {e.fullname(fn)} impl base+{impl - e.d.base:#x}")
            continue
        segs, code = split_path(path), []                         # hops: ObjectProperty -> its class, StructProperty -> its struct
        segs = segs[:1] + auto_cast(e, world, segs[1:]) if segs[0] == "World" else auto_cast(e, pawn, segs)
        leaf_map = "{" in segs[-1]                                # Map{key} at the end: the map's value is the number
        hops = (segs[1:] if segs[0] == "World" else segs)[:None if leaf_map else -1]
        if segs[0] == "World":                                    # World-rooted: runs before (and without) the pawn walk
            code = ["mov rcx, [GWorld]", "test rcx, rcx", f"jz c{k}_end"]
            cls, reg, pend = path_code(e, world_cls, hops, "rcx", f"c{k}_end", code, cells)
        else:
            cls, reg, pend = path_code(e, pawn_cls, hops, "rax", f"c{k}_end", code, cells)
        if leaf_map:
            isinstance(cls, tuple) or sys.exit(f"{segs[-1]}: the map's values are not numbers, go on with .Member")
            p = cls[1]; kind = e.ffield_class_name(p); off = 0
        elif segs[-1].startswith("@"):                            # raw offset of an unreflected C++ field: @0xd8:i
            off, kind = raw_leaf(segs[-1])
        else:
            p = member(e, cls, segs[-1]); kind = e.ffield_class_name(p); off = e.prop_offset(p)
        if kind == "BoolProperty":
            _, boff, mask, fmask = e.bool_info(p)
            close(e, code, pend, off + boff + 1, f"c{k}_end")
            on = value.strip().lower() not in ("0", "false", "no")
            if fmask == 0xFF:
                code.append(f"mov byte ptr [{reg}+{off + boff:#x}], {int(on)}")
            elif on:
                code.append(f"or byte ptr [{reg}+{off + boff:#x}], {mask:#x}")
            else:
                code.append(f"and byte ptr [{reg}+{off + boff:#x}], {(~mask) & 0xFF:#x}")
        elif kind in CELL:
            vt, size, vreg = CELL[kind]
            close(e, code, pend, off + size, f"c{k}_end")
            mems.append(f"GetMem( Val{k}, {size} )")
            code += [f"mov {vreg}, [Val{k}]", f"mov [{reg}+{off:#x}], {vreg}"]
            entries.append(f"      <Symbol>Val{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>{vt}</ValueType>\n      <Value>{value}</Value>")
        else:
            sys.exit(f"{segs[-1]}: {kind} not supported (bool/int/float/double/byte only)")
        (world_blocks if segs[0] == "World" else pawn_blocks).append(
            "\n".join([f"cmp byte ptr [Flag{k}], 1", f"jne c{k}_end"] + code + [f"c{k}_end:"]))
        add_cells()
        cheats.append((name, entries))
        print(f"{name}: {'' if segs[0] == 'World' else 'pawn.'}{'.'.join(segs)} ({kind} +{off:#x}) = {value}")

    if item_specs:                                              # all inventory rules share one block (own chain walk + add_item routine)
        import ueitems, ueitems_tlh
        cache = ueitems.load_cache(dump)
        if cache.get("mode") == "give":                          # no built-in inventory layout: call the game's own give function
            gi = cache["give"]["item"]
            pe_specs = [s for s in item_specs if gi.get("via") == "pe" or s[1].lower() == "$expand"]     # expansion: ProcessEvent only
            nat_specs = [s for s in item_specs if s not in pe_specs]
            if pe_specs:
                gcode, gsyms = ueitems_tlh.pe_block(e, dump, cache, pe_specs, meta)
                print(f"inventory rules: {len(pe_specs)} via ProcessEvent ({gi['owner']}::{gi['name']})")
                world_blocks.append("\n".join(gcode))
                syms += gsyms
            if nat_specs:
                gcode, gsyms = ueitems_tlh.give_block(cache, nat_specs, e, meta)
                print(f"inventory rules: {len(nat_specs)} via {gi['owner']}::{gi['name']} (impl base+{gi['impl_rva']:#x})")
                world_blocks.append("\n".join(gcode))
                syms += gsyms
        else:
            ch = ueitems_tlh.resolve_chain(e, dump, cache)
            entries, money = ueitems_tlh.rule_entries(cache, item_specs)
            world_blocks.append("\n".join(ueitems_tlh.world_block(e, ch, entries, money)))
            if ch["static"]:
                syms.append(ueitems_tlh.static_symbol(cache, ch))
    text = "\n".join(world_blocks + pawn_blocks)
    if "call ucname" in text or "call ucobj" in text:            # shared name / object lookups of call: and {Name} hops
        import ueitems_tlh
        rout = ["jmp ucskip"] + ueitems_tlh.name_routine("ucname", e) + ueitems_tlh.object_routine("ucobj", meta, e) * ("call ucobj" in text) + ["ucskip:"]
        world_blocks.append("\n".join(rout))
        syms += ueitems_tlh.gnames_symbol(rout, meta)
    syms = [f"RegisterSymbol( GWorld, ModuleBase+{meta['gworld']:#x} )"] + ([f"RegisterSymbol( GObjects, ModuleBase+{meta['gobjects']:#x} )"] if "[GObjects]" in str(world_blocks + pawn_blocks) else []) + list(dict.fromkeys(syms))
    asm = "\n".join(mems + syms + ["", "pushfq", "push rax", "push rcx", "push rdx", ""]
                    + world_blocks
                    + ["mov rax, [GWorld]", "test rax, rax", "jz done",
                       f"mov rax, [rax+{o_gi:#x}]", "test rax, rax", "jz done",            # OwningGameInstance
                       f"cmp dword ptr [rax+{o_lp + 8:#x}], 0", "jle done",                # LocalPlayers.Num
                       f"mov rax, [rax+{o_lp:#x}]", "test rax, rax", "jz done",            # LocalPlayers.Data
                       "mov rax, [rax]", "test rax, rax", "jz done",                       # [0]
                       f"mov rax, [rax+{o_pc:#x}]", "test rax, rax", "jz done",            # PlayerController
                       f"mov rax, [rax+{o_pawn:#x}]", "test rax, rax", "jz done", ""]      # Pawn
                    + pawn_blocks + ["done:", "pop rdx", "pop rcx", "pop rax", "popfq"] + site_instructions(e, pe))
    aob = "-".join(f"{b:02X}" for b in unique_aob(e, pe))
    game = xml_escape(os.path.basename(out).split("_")[0])
    xml = [f'<?xml version="1.0" encoding="utf-8" standalone="no"?>',
           f'<Trainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           f'Game="{game}" Creator="uecheat" CUSA="{cusa}" Version="{tlh_version(info.get("version"))}" Process="eboot.bin" Legacy="1">',
           '  <StartUP Name="UE player hook (ProcessEvent)" Description="GWorld -> GameInstance -> LocalPlayers[0] -> PlayerController -> Pawn">',
           '    <Entry>', '      <Section>0</Section>', f'      <AOB>{aob}</AOB>', '      <Type>Injection</Type>',
           f'      <Assembly>{xml_escape(asm)}</Assembly>', '    </Entry>', '  </StartUP>']
    for name, entries in cheats:
        xml.append(f'  <Cheat Name="{xml_escape(name)}" Description="">')
        for en in entries:
            xml += ['    <Entry>', '      <Section>0</Section>', en, '    </Entry>']
        xml.append('  </Cheat>')
    xml.append('</Trainer>')
    open(out, "w", encoding="utf-8-sig", newline="\n").write("\n".join(xml) + "\n")
    print(f"wrote {out}: hook at base+{meta['process_event']:#x} (AOB {len(aob.split('-'))} bytes), GWorld base+{meta['gworld']:#x}, {len(cheats)} cheats")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
