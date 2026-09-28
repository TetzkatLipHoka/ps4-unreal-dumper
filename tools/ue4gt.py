"""UE4: run a UFunction ON THE GAME THREAD by briefly hijacking the bytecode of a Blueprint Ubergraph that an
every-frame event (ReceiveTick) drives. Object/widget creation from the ps4debug RPC thread crashes the game
(measured: Days Gone ToggleCheatMenu), this does not.
    python ue4gt.py ../dumps/CUSA09175/dump.bin 192.168.x.y 0x24f573c00 ToggleCheatMenu carrier=BP_Storm_Manager_C.ExecuteUbergraph_BP_Storm_Manager
    python ue4gt.py <dump> <host> <0xADDR|Class|Class.Property> <Function> [Param=value ...] carrier=Class.ExecuteUbergraph_X
Payload (tokens measured on Days Gone 4.11: EX_Context = expr, u32 skip, 8-byte r-value property, no trailing
byte; natives are called by UFunction pointer; script FNames are 12 bytes):
    JumpIfNot(flag) -> T ; Return              already ran in this window -> nothing
 T: LetBool flag = True
    Context(ObjectConst obj) FinalFunction target(args) ; Return ; EndOfScript
flag = the first bool local of the carrier Ubergraph (lives on the instance's persistent frame, cleared before and
after), so the call runs exactly once per carrier instance instead of once per frame.
"""
import struct, sys, time
import ueobj
from ueobj import CF, RF_ClassDefaultObject
import ue3gt
from ue3gt import EX, func_in, resolve, const_expr

EX = dict(EX, LocalVariable=0x00, JumpIfNot=0x07, LetBool=0x14, FinalFunction=0x1C)


def live_instance(e, cname):
    """First non-CDO instance (UE4 objects always carry ObjectFlags, so ue3gt's flags==0 test never matches)."""
    cls = e.find(cname, CF["Class"]) or sys.exit(f"class {cname} not found")
    def is_a(c):
        while c:
            if c == cls:
                return True
            c = e.super(c)
    return next((o for i, o in e.arr if not (e.flags(o) or 0) & RF_ClassDefaultObject and is_a(e.cls(o))), None) or sys.exit(f"no live {cname} in dump")


ue3gt.live_instance = live_instance          # resolve() (Class / Class.Property targets) uses it too


def ue4_const(e, kind, pname, text):
    if kind == "NameProperty":                       # FScriptName {ComparisonIndex, DisplayIndex, Number}
        idx = next((i for i in range(e.pool.num) if e.pool.name(i) == text), None)
        if idx is None:
            sys.exit(f"name '{text}' does not exist in the name table")
        return bytes([EX["NameConst"]]) + struct.pack("<iii", idx, idx, 0)
    if kind == "StrProperty":                        # EX_StringConst: ANSI, NUL-terminated (UE4 execStringConst)
        return bytes([EX["StringConst"]]) + text.encode("ascii") + b"\0"
    return const_expr(e, kind, pname, text)


def member_in(e, c, name):
    while c:
        m = e.member(c, name)
        if m:
            return m
        c = e.super(c)


async def main(dump, host, obj, fname, *rest):
    import ps4debug
    e = ueobj.open_engine(dump, log=lambda *a: None)
    O = e.O
    script_off = O.get("UStruct_Script", O["UStruct_Size"] + 8)
    values = dict(a.split("=", 1) for a in rest if "=" in a and not a.startswith(("carrier=", "then=")))
    carrier_spec = next((a[8:] for a in rest if a.startswith("carrier=")), None) or sys.exit("carrier=Class.ExecuteUbergraph_X required")
    then_spec = next((a[5:] for a in rest if a.startswith("then=")), None)   # then=<0xADDR|Class>.<Function>:<Param=value,...>
    carrier_class, _, event = carrier_spec.partition(".")
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    carrier = live_instance(e, carrier_class)
    func = func_in(e, e.cls(carrier), event) or sys.exit(f"{event} not found in {e.fullname(carrier)}")
    classes = {c for i, c in e.arr if e.isa(c, CF["Class"])}

    async def build_call(obj, fname, values):
        ret = values.pop("ret", None)
        """Context(ObjectConst obj) FinalFunction fname(args). Values: constant, or local:<Name> = a local of the
        carrier Ubergraph (lives on its persistent frame, so it can carry an out-parameter into the next call)."""
        obj = await resolve(e, dbg, pid, obj)
        live_cls = struct.unpack("<Q", await dbg.read_memory(pid, obj + O["UObject_Class"], 8))[0]
        if live_cls not in classes:
            sys.exit(f"object {obj:#x} is not a live UObject any more (class field {live_cls:#x}); refusing to call {fname}")
        target = func_in(e, live_cls, fname) or sys.exit(f"live {e.name(live_cls)} {obj:#x} has no function {fname}")
        args = b""
        for p in e.properties(target):
            n = e.ffield_name(p)
            if n == "ReturnValue" or not (e.val(p + O["Property_PropertyFlags"], "<Q") or 0) & 0x80:   # CPF_Parm
                continue
            if n in values:
                v = values.pop(n)
                if v.startswith("local:"):
                    lp = next((q for q in e.properties(func) if e.ffield_name(q) == v[6:]), None) or sys.exit(f"carrier has no local {v[6:]}")
                    args += bytes([EX["LocalVariable"]]) + struct.pack("<Q", lp)
                else:
                    args += ue4_const(e, e.ffield_class_name(p), n, v)
            elif values:
                sys.exit(f"parameter {n} must be given too (arguments are positional in bytecode)")
        if values:
            sys.exit(f"unknown parameter(s) {list(values)} for {fname}")
        call = bytes([EX["FinalFunction"]]) + struct.pack("<Q", target) + args + bytes([EX["EndFunctionParms"]])
        ctx = bytes([EX["Context"], EX["ObjectConst"]]) + struct.pack("<Q", obj) + struct.pack("<I", len(call)) + bytes(8) + call
        if ret:                                      # ret=local:<Name>: LetObj <local> = <call>  (UE4 EX_LetObj 0x5F)
            lp = next((q for q in e.properties(func) if e.ffield_name(q) == ret[6:]), None) or sys.exit(f"carrier has no local {ret[6:]}")
            ctx = bytes([0x5F, EX["LocalVariable"]]) + struct.pack("<Q", lp) + ctx
        return obj, ctx

    obj, ctx = await build_call(obj, fname, values)
    if then_spec:
        obj2, _, rest2 = then_spec.partition(".")
        fname2, _, params2 = rest2.partition(":")
        values2 = dict(a.split("=", 1) for a in params2.split(",") if "=" in a)
        ctx += (await build_call(obj2, fname2, values2))[1]
        fname += f" ; {fname2}({params2})"
    script, n = e.u64(func + script_off), e.i32(func + script_off + 8)
    n = max(n or 0, e.i32(func + script_off + 12) or 0)   # TArray capacity: slack past Num is ours (FF7R Cloud AI: 56 used / 64)
    flag = next((p for p in e.properties(func) if e.ffield_class_name(p) == "BoolProperty"
                 and not (e.val(p + O["Property_PropertyFlags"], "<Q") or 0) & 0x80), None) or sys.exit(f"{event} has no bool local for the once-flag")
    frame_prop = member_in(e, e.cls(carrier), "UberGraphFrame") or sys.exit("carrier class has no UberGraphFrame")
    frame = struct.unpack("<Q", await dbg.read_memory(pid, carrier + e.prop_offset(frame_prop), 8))[0] or sys.exit("persistent frame is NULL")
    _, byte_off, byte_mask, _ = e.bool_info(flag)
    flag_addr = frame + e.prop_offset(flag) + byte_off

    async def clear_flag():
        b = (await dbg.read_memory(pid, flag_addr, 1))[0]
        await dbg.write_memory(pid, flag_addr, bytes([b & ~byte_mask & 0xFF]))

    head =bytes([EX["JumpIfNot"]]) + struct.pack("<I", 16) + bytes([EX["LocalVariable"]]) + struct.pack("<Q", flag) + bytes([EX["Return"], EX["Nothing"]])
    assert len(head) == 16
    patch = head + bytes([EX["LetBool"], EX["LocalVariable"]]) + struct.pack("<Q", flag) + bytes([EX["True_"]]) + ctx + bytes([EX["Return"], EX["Nothing"], EX["EndOfScript"]])
    if (n or 0) < len(patch) or (e.i32(func + O["UFunction_FunctionFlags"]) or 0) & 0x400:
        sys.exit(f"{event} bytecode unusable ({n} bytes / native / patch {len(patch)} bytes)")
    orig = await dbg.read_memory(pid, script, len(patch))
    print(f"carrier {e.fullname(carrier)} :: {event} script @ {script:#x} ({n} bytes), once-flag {e.ffield_name(flag)} @ {flag_addr:#x} "
          f"-> {fname}({', '.join(f'{k}={v}' for k, v in (a.split('=', 1) for a in rest if '=' in a and not a.startswith('carrier=')))}) on {obj:#x}")
    await clear_flag()
    await dbg.write_memory(pid, script, patch)
    patched = (await dbg.read_memory(pid, script, len(patch))) == patch
    time.sleep(0.5)
    await dbg.write_memory(pid, script, orig)
    ran = bool((await dbg.read_memory(pid, flag_addr, 1))[0] & byte_mask)
    await clear_flag()
    print("patched:", patched, " restored:", (await dbg.read_memory(pid, script, len(patch))) == orig, " executed:", ran,
          "" if ran else " (carrier did not run in 0.5 s: game paused / event not firing?)")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    ueobj.run(main(*sys.argv[1:]))
