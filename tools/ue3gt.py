"""UE3: run a script function ON THE GAME THREAD by briefly hijacking the bytecode of an event the engine calls
every frame (HUD.PostRender by default). The first bytes become
`Context(ObjectConst obj) VirtualFunction name(<const args>); Return`; half a second later the original bytes are
restored. Use for calls that allocate objects (AddCheats etc.), which crash when made from the ps4debug RPC thread.

    python ue3gt.py ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController AddCheats
    python ue3gt.py ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController.CheatManager Fly
    python ue3gt.py ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController.CheatManager Slomo T=0.3
    python ue3gt.py <dump> <host> <0xADDR|Class|Class.Property> <Function> [Param=value ...] [carrier=HUD.PostRender]

Parameters are embedded as bytecode constants (float/int/byte/bool/object/name/string); struct params are not
supported. Return values are discarded.
"""
import asyncio, struct, sys, time
import ueobj
from ueobj import CF

# EExprToken (measured on Dishonored: FloatConst 0x1E; the rest per UE3 source)
EX = dict(Return=0x04, Nothing=0x0B, EndFunctionParms=0x16, Context=0x19, IntConst=0x1D, FloatConst=0x1E,
          StringConst=0x1F, ObjectConst=0x20, NameConst=0x21, ByteConst=0x24, True_=0x27, False_=0x28,
          VirtualFunction=0x1B, EndOfScript=0x53)


def func_in(e, c, name):
    while c:                                   # most-derived definition wins (virtual dispatch by name)
        f = e.member(c, name, CF["Function"])
        if f:
            return f
        c = e.super(c)


def live_instance(e, cname):
    cls = e.find(cname, CF["Class"]) or sys.exit(f"class {cname} not found")
    def is_a(c):
        while c:
            if c == cls:
                return True
            c = e.super(c)
    return next((o for i, o in e.arr if not e.flags(o) and is_a(e.cls(o))), None) or sys.exit(f"no live {cname} in dump")


async def resolve(e, dbg, pid, spec):
    """0xADDR | ClassName (first live instance in the dump) | ClassName.Property (ObjectProperty read live)."""
    if spec.lower().startswith("0x"):
        return int(spec, 0)
    cname, _, prop = spec.partition(".")
    inst = live_instance(e, cname)
    if not prop:
        return inst
    c, p = e.cls(inst), None
    while c and not p:
        p = e.member(c, prop)
        c = e.super(c)
    if not p:
        sys.exit(f"{cname} has no property {prop}")
    v = struct.unpack("<Q", await dbg.read_memory(pid, inst + e.prop_offset(p), 8))[0]
    if not v:
        sys.exit(f"{e.fullname(inst)}.{prop} is None (live) - for CheatManager run AddCheats first")
    print(f"{spec} -> {v:#x}")
    return v


def const_expr(e, kind, pname, text):
    """Bytecode for one constant argument of the given property class."""
    if kind == "FloatProperty":
        return bytes([EX["FloatConst"]]) + struct.pack("<f", float(text))
    if kind == "IntProperty":
        return bytes([EX["IntConst"]]) + struct.pack("<i", int(text, 0))
    if kind == "ByteProperty":
        return bytes([EX["ByteConst"], int(text, 0) & 0xFF])
    if kind == "BoolProperty":
        return bytes([EX["False_"] if text.lower() in ("0", "false", "no") else EX["True_"]])
    if kind in ("ObjectProperty", "ClassProperty", "ComponentProperty"):
        return bytes([EX["ObjectConst"]]) + struct.pack("<Q", int(text, 0))
    if kind == "NameProperty":
        idx = next((i for i in range(e.pool.num) if e.pool.name(i) == text), None)
        if idx is None:
            sys.exit(f"name '{text}' does not exist in the name table")
        return bytes([EX["NameConst"]]) + struct.pack("<ii", idx, 0)
    if kind == "StrProperty":
        return bytes([EX["StringConst"]]) + text.encode("latin1") + b"\x00"
    sys.exit(f"parameter {pname}: {kind} not supported as a bytecode constant")


async def main(dump, host, obj, fname, *rest):
    import ps4debug
    e = ueobj.open_engine(dump, log=lambda *a: None)
    O = e.O
    values = dict(a.split("=", 1) for a in rest if "=" in a and not a.startswith("carrier="))
    carrier_class, _, event = next((a[8:] for a in rest if a.startswith("carrier=")), "HUD.PostRender").partition(".")
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    obj = await resolve(e, dbg, pid, obj)
    live_cls = struct.unpack("<Q", await dbg.read_memory(pid, obj + O["UObject_Class"], 8))[0]
    if live_cls not in {c for i, c in e.arr if e.isa(c, CF["Class"])}:
        sys.exit(f"object {obj:#x} is not a live UObject any more (class field {live_cls:#x}); refusing to call {fname}")
    target = func_in(e, live_cls, fname) or sys.exit(
        f"live {e.name(live_cls)} {obj:#x} has no function {fname} in its class chain (calling an unknown name crashes the VM)")
    # arguments in declaration order; unspecified ones are left out (the VM zero-fills them)
    args = b""
    for p in e.properties(target):
        n = e.ffield_name(p)
        if n == "ReturnValue" or not (e.val(p + O["Property_PropertyFlags"], "<Q") or 0) & 0x80:   # CPF_Parm
            continue
        if n in values:
            args += const_expr(e, e.ffield_class_name(p), n, values.pop(n))
        elif values:
            sys.exit(f"parameter {n} must be given too (arguments are positional in bytecode)")
    if values:
        sys.exit(f"unknown parameter(s) {list(values)} for {fname}")
    carrier = live_instance(e, carrier_class)
    func = func_in(e, e.cls(carrier), event) or sys.exit(f"{event} not found in {e.fullname(carrier)}")
    script, n = e.u64(func + O["UStruct_Script"]), e.i32(func + O["UStruct_Script"] + 8)
    # Context: ObjectExpr, skip:u16, retprop:8, bsize:1 (measured), then the call expression
    call = bytes([EX["VirtualFunction"]]) + struct.pack("<ii", e.i32(target + O["UObject_Name"]), 0) + args + bytes([EX["EndFunctionParms"]])
    patch = (bytes([EX["Context"], EX["ObjectConst"]]) + struct.pack("<Q", obj) + struct.pack("<H", len(call)) + bytes(9)
             + call + bytes([EX["Return"], EX["Nothing"], EX["EndOfScript"]]))
    if (n or 0) < len(patch) or (e.i32(func + O["UFunction_FunctionFlags"]) or 0) & 0x400:
        sys.exit(f"{event} bytecode unusable ({n} bytes / native / patch {len(patch)} bytes)")
    orig = await dbg.read_memory(pid, script, len(patch))
    print(f"carrier {e.fullname(carrier)} :: {event} script @ {script:#x} ({n} bytes) -> {fname}({', '.join(f'{k}={v}' for k, v in (a.split('=', 1) for a in rest if '=' in a and not a.startswith('carrier=')))}) on {obj:#x}")
    await dbg.write_memory(pid, script, patch)
    time.sleep(0.5)
    await dbg.write_memory(pid, script, orig)
    print("restored:", (await dbg.read_memory(pid, script, len(patch))) == orig)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    ueobj.run(main(*sys.argv[1:]))
