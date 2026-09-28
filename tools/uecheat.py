"""Stage-1 cheat generator: UE4 dump -> TLH trainer file for the cheat-menu PRX (no PRX changes, no names at runtime).
    python uecheat.py ../dumps/CUSA09175/dump.bin out.TLH                      # default set (God, Player time, Walk speed)
    python uecheat.py <dump.bin> <out.TLH> "Name=Path=Value" ...              # Path relative to the player pawn
      "God=bCanBeDamaged=0"  "Player time=CustomTimeDilation=2"  "Walk speed=CharacterMovement.MaxWalkSpeed=1200"
      "Anim speed=Mesh.AnimScriptInstance:BendPlayerAnimInstance.LocationBasedSpeedScale=2"   (Prop:Class = downcast)
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

CELL = dict(FloatProperty=("Single", 4, "edx"), IntProperty=("Integer", 4, "edx"), UInt32Property=("Cardinal", 4, "edx"),
            ByteProperty=("Byte", 1, "dl"), Int8Property=("Byte", 1, "dl"))
DEFAULT = ["God=bCanBeDamaged=0", "Player time=CustomTimeDilation=1", "Walk speed=CharacterMovement.MaxWalkSpeed=600"]
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


def player_pawn_class(e, pawn_off):
    """Class of the pawn a live PlayerController owns in the dump (most-derived start for property paths)."""
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
                return e.cls(pawn)


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


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def hop_code(e, cls, tok, reg, fail):
    """Assembly for one object hop in `reg`; returns (lines, class after the hop or None)."""
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
    p = member(e, cls or sys.exit(f"{tok}: class unknown after a raw hop, use +0xNN"), name)
    if e.ffield_class_name(p) not in ("ObjectProperty", "WeakObjectProperty"):
        sys.exit(f"{name} is {e.ffield_class_name(p)}, expected an object pointer")
    nxt = cls_of(e, cast) if cast else e.ptr(p + e.O["ObjectProperty_Class"])
    return [f"mov {reg}, [{reg}+{e.prop_offset(p):#x}]", f"test {reg}, {reg}", f"jz {fail}"], nxt


def main(dump, out, *specs):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    sdk = os.path.join(os.path.dirname(dump), "SDK", "offsets.json")
    meta = json.load(open(sdk))
    if not meta.get("gworld") or not meta.get("process_event"):
        sys.exit("offsets.json lacks gworld/process_event (rerun uegen with the console reachable)")
    info = json.load(open(dump[:-4] + ".json"))
    cusa = int(info["title_id"][4:]) if info["title_id"].startswith("CUSA") else 0
    pe = e.d.base + meta["process_event"]
    O = e.O
    hop = lambda cls, prop: e.prop_offset(member(e, cls_of(e, cls), prop))
    chain = [("World", "OwningGameInstance"), ("GameInstance", "LocalPlayers"), ("Player", "PlayerController"), ("Controller", "Pawn")]
    o_gi, o_lp, o_pc, o_pawn = (hop(c, p) for c, p in chain)
    pawn_cls = player_pawn_class(e, o_pawn) or cls_of(e, "Pawn")
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
        for seg in segs[:-1]:                                     # object hops: ObjectProperty -> its class
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
            sys.exit(f"{segs[-1]}: {kind} not supported (bool/int/float/byte only)")
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
           f'Game="{game}" Creator="uecheat" CUSA="{cusa}" Version="" Process="eboot.bin" Legacy="1">',
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
