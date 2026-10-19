"""Stage-1 cheat generator: UE4 dump -> TLH trainer file for the cheat-menu PRX (no PRX changes, no names at runtime).
    python uecheat.py ../dumps/CUSA09175/dump.bin out.TLH                      # default set (God, Player time, Walk speed)
    python uecheat.py <dump.bin> <out.TLH> "Name=Path=Value" ...              # Path relative to the player pawn
      "God=bCanBeDamaged=0"  "Player time=CustomTimeDilation=2"  "Walk speed=CharacterMovement.MaxWalkSpeed=1200"
      "Anim speed=Mesh.AnimScriptInstance:BendPlayerAnimInstance.LocationBasedSpeedScale=2"   (Prop:Class = downcast)
      "Inf Hp=HealthComponent.HealthSet.Damage.CurrentValue=0"   (struct members inline: HealthSet, Damage)
      "Hp=AbilitySystemComponent.SpawnedAttributes[3]:ATR_Health.Health.CurrentValue=500"   (TArray<UObject*> element)
      "Items=Controller:GameplayPC_C.PlayerInventoryComponent.Inventory[15].Amount=99"     (TArray<struct> element)
    python uecheat.py <dump.bin> --find Damage [depth]   # list ready-to-use paths whose name contains the word (default depth 4)
      walks the dump's live player pawn (real component classes, downcasts added), prints path, type and dump value
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


def find_paths(e, pawn, word, depth=4):
    """BFS over the live pawn: (path, kind, dump value) for every bool/number member whose name, or the name of a struct
    it sits in, contains `word` (object names earlier in the path don't count: that would list whole components).
    Object hops use the instance's real class (":Class" downcast when it differs from the declared one)."""
    word, seen, todo, hits = word.lower(), {pawn}, [(pawn, e.cls(pawn), "", "")], []
    for _ in range(depth + 1):
        nxt = []
        for addr, st, path, local in todo:                       # local = struct names since the last object hop
            c = st
            while c:
                for p in e.properties(c):
                    objs, name, kind = [], e.ffield_name(p) or "?", e.ffield_class_name(p)
                    a, full = addr + e.prop_offset(p), path + name
                    if kind in FMT or kind == "BoolProperty":
                        if word in (local + name).lower():
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
                        if rc and e.name(rc) and not e.isa(o, CF["Pawn"]):   # other pawns (enemies, NPCs) are noise
                            seen.add(o)
                            nxt.append((o, rc, seg + ("" if rc == dc else f":{e.name(rc)}") + ".", ""))
                c = e.super(c)
        todo = nxt
    return hits


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
    """Assembly for one object/struct hop in `reg`; returns (lines, class/struct after the hop or None)."""
    if tok.startswith("weak:"):                                  # TWeakObjectPtr {int32 Index; int32 Serial} -> GObjects item
        off = int(tok[5:], 0)
        return [f"movsxd rdx, dword ptr [{reg}+{off:#x}]", "test rdx, rdx", f"js {fail}",
                "mov r8, [GObjects]", "test r8, r8", f"jz {fail}", "shl rdx, 4", "add r8, rdx",
                f"mov r9d, dword ptr [{reg}+{off + 4:#x}]", "cmp dword ptr [r8+0xc], r9d", f"jne {fail}",
                "test byte ptr [r8+0xb], 0x30", f"jnz {fail}",                       # pending kill / unreachable
                f"mov {reg}, [r8]", f"test {reg}, {reg}", f"jz {fail}"], None
    if tok.startswith("+"):
        return [f"mov {reg}, [{reg}+{int(tok, 0):#x}]", f"test {reg}, {reg}", f"jz {fail}"], None
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
            return code + [f"add {reg}, {i * e.prop_size(inner):#x}"], e.ptr(inner + e.O["StructProperty_Struct"])
        nxt = cls_of(e, cast) if cast else e.ptr(inner + e.O["ObjectProperty_Class"])
        return code + [f"mov {reg}, [{reg}+{8 * i:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt
    if kind == "StructProperty":                                  # inline struct: no pointer, just advance (talixme)
        return [f"add {reg}, {off:#x}"], e.ptr(p + e.O["StructProperty_Struct"])
    if kind not in ("ObjectProperty", "WeakObjectProperty"):
        sys.exit(f"{name} is {kind}, expected an object pointer or struct")
    nxt = cls_of(e, cast) if cast else e.ptr(p + e.O["ObjectProperty_Class"])
    return [f"mov {reg}, [{reg}+{off:#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt


def main(dump, out, *specs):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    hop = lambda cls, prop: e.prop_offset(member(e, cls_of(e, cls), prop))
    chain = [("World", "OwningGameInstance"), ("GameInstance", "LocalPlayers"), ("Player", "PlayerController"), ("Controller", "Pawn")]
    o_gi, o_lp, o_pc, o_pawn = (hop(c, p) for c, p in chain)
    pawn = player_pawn(e, o_pawn)
    if out == "--find":
        pawn or sys.exit("no live player pawn in this dump (dump in-game, not in a menu)")
        print(f"player pawn class in dump: {e.name(e.cls(pawn))}")
        for path, kind, v in find_paths(e, pawn, specs[0], int(specs[1]) if len(specs) > 1 else 4):
            print(f"{path}	{kind}	{v}")
        return
    sdk = os.path.join(os.path.dirname(dump), "SDK", "offsets.json")
    meta = json.load(open(sdk))
    if not meta.get("gworld") or not meta.get("process_event"):
        sys.exit("offsets.json lacks gworld/process_event (rerun uegen with the console reachable)")
    info = json.load(open(dump[:-4] + ".json"))
    cusa = int(info["title_id"][4:]) if info["title_id"][4:].isdigit() else 0      # CUSA12345 / PPSA12345 -> 12345
    pe = e.d.base + meta["process_event"]
    O = e.O
    pawn_cls = e.cls(pawn) if pawn else cls_of(e, "Pawn")
    world_cls = cls_of(e, "World")
    print(f"player pawn class in dump: {e.name(pawn_cls)}")

    cheats, pawn_blocks, button_blocks, mems, syms = [], [], [], [], []
    for k, spec in enumerate(specs or DEFAULT, 1):
        name, path, value = spec.split("=", 2)
        mems.append(f"GetMem( Flag{k}, 1 )")
        entries = [f"      <Symbol>Flag{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>Boolean</ValueType>\n      <Value>0</Value>"]
        if path == "button":
            toks = value.split(".")
            root, cls, reg = toks[0], None, "rcx"
            code = [f"cmp byte ptr [Flag{k}], 1", f"jne b{k}_end", f"mov byte ptr [Flag{k}], 2"]      # once per switch-on
            code += [f"push {r}" for r in CALLER_SAVED]
            if root == "World":
                code += ["mov rcx, [GWorld]", "test rcx, rcx", f"jz b{k}_pop"]; cls = world_cls
            elif root == "Pawn":
                code += ["mov rcx, rax", "test rcx, rcx", f"jz b{k}_pop"]; cls = pawn_cls
            else:
                sys.exit(f"{name}: button path must start with World or Pawn")
            for tok in toks[1:-1]:
                lines, cls = hop_code(e, cls, tok, reg, f"b{k}_pop"); code += lines
            fn = (func_in(e, cls, toks[-1]) if cls else None) or next((o for o in e.by_name.get(toks[-1].lower(), ()) if e.isa(o, CF["Function"])), None)
            if not fn:
                sys.exit(f"{name}: function {toks[-1]} not found")
            impl = native_impl(e, fn)
            syms.append(f"RegisterSymbol( Fn{k}, ModuleBase+{impl - e.d.base:#x} )")
            code += ["mov rdi, rcx", "sub rsp, 8", f"mov rax, Fn{k}", "call rax", "add rsp, 8", f"b{k}_pop:"]
            code += [f"pop {r}" for r in reversed(CALLER_SAVED)] + [f"b{k}_end:"]
            button_blocks.append("\n".join(code))
            cheats.append((name, entries))
            print(f"{name}: button -> {e.fullname(fn)} impl base+{impl - e.d.base:#x}")
            continue
        segs = path.split(".")
        cls, code, reg = pawn_cls, [], "rax"
        for seg in segs[:-1]:                                     # hops: ObjectProperty -> its class, StructProperty -> its struct
            if reg == "rax":
                code.append("mov rcx, rax"); reg = "rcx"
            lines, cls = hop_code(e, cls, seg, reg, f"c{k}_end"); code += lines
        p = member(e, cls, segs[-1]); kind = e.ffield_class_name(p); off = e.prop_offset(p)
        if kind == "BoolProperty":
            _, boff, mask, fmask = e.bool_info(p)
            on = value.strip().lower() not in ("0", "false", "no")
            if fmask == 0xFF:
                code.append(f"mov byte ptr [{reg}+{off + boff:#x}], {int(on)}")
            elif on:
                code.append(f"or byte ptr [{reg}+{off + boff:#x}], {mask:#x}")
            else:
                code.append(f"and byte ptr [{reg}+{off + boff:#x}], {(~mask) & 0xFF:#x}")
        elif kind in CELL:
            vt, size, vreg = CELL[kind]
            mems.append(f"GetMem( Val{k}, {size} )")
            code += [f"mov {vreg}, [Val{k}]", f"mov [{reg}+{off:#x}], {vreg}"]
            entries.append(f"      <Symbol>Val{k}</Symbol>\n      <Type>Value</Type>\n      <ValueType>{vt}</ValueType>\n      <Value>{value}</Value>")
        else:
            sys.exit(f"{segs[-1]}: {kind} not supported (bool/int/float/double/byte only)")
        pawn_blocks.append("\n".join([f"cmp byte ptr [Flag{k}], 1", f"jne c{k}_end"] + code + [f"c{k}_end:"]))
        cheats.append((name, entries))
        print(f"{name}: pawn.{path} ({kind} +{off:#x}) = {value}")

    syms = [f"RegisterSymbol( GWorld, ModuleBase+{meta['gworld']:#x} )"] + ([f"RegisterSymbol( GObjects, ModuleBase+{meta['gobjects']:#x} )"] if button_blocks else []) + syms
    asm = "\n".join(mems + syms + ["", "pushfq", "push rax", "push rcx", "push rdx", ""]
                    + button_blocks
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
