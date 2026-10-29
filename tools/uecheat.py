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
                           search from the pawn and from World)
      "Hp=GameData:RmoPlayerData.@0xd8:i=999"   (@offset:type = unreflected C++ field, as --addr prints it; i/f/d/b)
    python uecheat.py <dump.bin> --find Damage [depth]   # list ready-to-use paths whose name contains the word (default depth 4)
      walks the dump's live player pawn (real component classes, downcasts added), prints path, type and dump value
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
import json, os, struct, sys
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


def find_paths(e, roots, word, depth=4, addrs=None):
    """BFS over the live pawn and World (roots = [(object, path prefix)]): (path, kind, dump value) for every bool/number member whose name, or the name of a struct
    it sits in, contains `word` (object names earlier in the path don't count: that would list whole components).
    With `addrs` (addresses from a scanner, same dump/session) members whose bytes cover one of them match instead.
    Object hops use the instance's real class (":Class" downcast when it differs from the declared one)."""
    word, hits, raw, hit_addrs = word.lower(), [], {}, set()
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
                        if cov if addrs is not None else word in (local + name).lower():
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
            inner, i = e.ptr(p + e.O["ArrayProperty_Inner"]), int(idx, 0)
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
    sys.exit("no unique AOB for ProcessEvent")


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


def hop_code(e, cls, tok, reg, fail):
    """Assembly for one object/struct hop in `reg`; returns (lines, class/struct after the hop or None, bytes read from
    reg (0 = none), bytes reg was advanced by (inline struct), whether reg now holds a UObject)."""
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
        i, inner = int(idx, 0), e.ptr(p + e.O["ArrayProperty_Inner"])
        ik = e.ffield_class_name(inner) if kind == "ArrayProperty" else None
        if ik not in ("ObjectProperty", "StructProperty"):
            sys.exit(f"{name} is {kind}, [i] needs a TArray of object pointers or structs")
        code = [f"cmp dword ptr [{reg}+{off + 8:#x}], {i}", f"jle {fail}",               # TArray {Data, Num, Max}
                f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}"]
        if ik == "StructProperty":                                # elements inline: Data + i * ElementSize
            return code + [f"add {reg}, {i * e.prop_size(inner):#x}"], e.ptr(inner + e.O["StructProperty_Struct"]), off + 0x10, 0, False
        nxt = cls_of(e, cast) if cast else e.ptr(inner + e.O["ObjectProperty_Class"])
        return code + [f"mov {reg}, [{reg}+{8 * i:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt, off + 0x10, 0, True
    if kind == "StructProperty":                                  # inline struct: no pointer, just advance (talixme)
        return [f"add {reg}, {off:#x}"], e.ptr(p + e.O["StructProperty_Struct"]), 0, off, False
    if kind not in ("ObjectProperty", "WeakObjectProperty"):
        sys.exit(f"{name} is {kind}, expected an object pointer or struct")
    nxt = cls_of(e, cast) if cast else e.ptr(p + e.O["ObjectProperty_Class"])
    return [f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt, off + 8, 0, True


def path_code(e, cls, toks, reg, fail, code):
    """Hops for toks from the UObject in reg, appended to code. Before the first access into an object a guard is
    inserted (see close): menus can swap the pawn for a small menu pawn (Mortal Shell BP_EquipmentMenuPawn_*_C),
    and reading a component offset behind its end gives garbage pointers. Returns (cls, reg, pend)."""
    pend = [len(code), reg, 0]                                    # guard slot, object register, struct offset inside it
    for tok in toks:
        if reg == "rax":
            code.append("mov rcx, rax"); reg = "rcx"
        lines, cls, end, adv, fresh = hop_code(e, cls, tok, reg, fail)
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


def main(dump, out, *specs):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    hop = lambda cls, prop: e.prop_offset(member(e, cls_of(e, cls), prop))
    chain = [("World", "OwningGameInstance"), ("GameInstance", "LocalPlayers"), ("Player", "PlayerController"), ("Controller", "Pawn")]
    o_gi, o_lp, o_pc, o_pawn = (hop(c, p) for c, p in chain)
    pawn = player_pawn(e, o_pawn)
    meta = json.load(open(os.path.join(os.path.dirname(dump), "SDK", "offsets.json")))
    world = meta.get("gworld") and e.ptr(e.d.base + meta["gworld"])
    if out in ("--find", "--addr", "--fields"):
        pawn or sys.exit("no live player pawn in this dump (dump in-game, not in a menu)")
        print(f"player pawn class in dump: {e.name(e.cls(pawn))}")
        roots = [(pawn, "")] + ([(world, "World.")] if world else [])
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
            rows = find_paths(e, roots, specs[0], int(specs[1]) if len(specs) > 1 else 4)
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

    cheats, pawn_blocks, world_blocks, mems, syms = [], [], [], [], []
    for k, spec in enumerate(specs or DEFAULT, 1):
        name, path, value = spec.split("=", 2)
        mems.append(f"GetMem( Flag{k}, 1 )")
        entries = [f"      <Symbol>Flag{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>Boolean</ValueType>\n      <Value>0</Value>"]
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
        segs, code = path.split("."), []                          # hops: ObjectProperty -> its class, StructProperty -> its struct
        if segs[0] == "World":                                    # World-rooted: runs before (and without) the pawn walk
            code = ["mov rcx, [GWorld]", "test rcx, rcx", f"jz c{k}_end"]
            cls, reg, pend = path_code(e, world_cls, segs[1:-1], "rcx", f"c{k}_end", code)
        else:
            cls, reg, pend = path_code(e, pawn_cls, segs[:-1], "rax", f"c{k}_end", code)
        if segs[-1].startswith("@"):                              # raw offset of an unreflected C++ field: @0xd8:i
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
        cheats.append((name, entries))
        print(f"{name}: {'' if segs[0] == 'World' else 'pawn.'}{path} ({kind} +{off:#x}) = {value}")

    syms = [f"RegisterSymbol( GWorld, ModuleBase+{meta['gworld']:#x} )"] + ([f"RegisterSymbol( GObjects, ModuleBase+{meta['gobjects']:#x} )"] if "[GObjects]" in str(world_blocks + pawn_blocks) else []) + syms
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
