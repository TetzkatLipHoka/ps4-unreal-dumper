"""Call a UFunction on the console through ps4debug's RPC stub: obj->ProcessEvent(func, params).

    python uecall.py ../dumps/CUSA31820/dump.bin 192.168.x.y KismetMathLibrary Add_IntInt A=2 B=40
    python uecall.py <dump> <host> <Class> <Function> [obj=0xADDR] [Param=value ...]

Object defaults to the class default object (fine for static/BlueprintPure library functions). Out params and
ReturnValue are read back from the parameter buffer. Runs on the RPC thread, not the game thread.
"""
import asyncio, os, struct, sys
import ueobj
from ueobj import CF
from uelive import FMT

RPC_MAGIC = b"\x52\x53\x54\x42\xA3"


async def rpc_stub(dbg, pid, cache):
    """RPC stub address: reuse the cached one if its magic is still there, else install and cache."""
    try:
        a = int(open(cache).read(), 0)
        if await dbg.read_memory(pid, a, len(RPC_MAGIC)) == RPC_MAGIC:
            return a
    except (OSError, ValueError):
        pass
    a = await dbg.install_rpc(pid)
    open(cache, "w").write(hex(a))
    return a


async def main(dump, host, cname, fname, args):
    import json, ps4debug
    e = ueobj.open_engine(dump, log=lambda *a: None)
    cls = e.find(cname, CF["Class"]) or sys.exit(f"class {cname} not found")
    func = e.find_in_outer(fname, cname) or sys.exit(f"function {cname}::{fname} not found")
    if e.O.get("UFunction_iNative") and e.d.u16(func + e.O["UFunction_iNative"]):
        sys.exit(f"{cname}::{fname} is a UE3 opcode native (iNative {e.d.u16(func + e.O['UFunction_iNative'])}); "
                 "ProcessEvent returns without calling it")
    obj = e.ptr(cls + e.O["UClass_ClassDefaultObject"])
    vals = {}
    for a in args:
        k, v = a.split("=", 1)
        if k == "obj":
            obj = int(v, 0)
        else:
            vals[k] = v
    params = [(e.ffield_name(p), e.ffield_class_name(p), e.prop_offset(p), e.prop_size(p)) for p in e.properties(func)]
    size = max([off + sz for _, _, off, sz in params] + [8])
    buf = bytearray(size)
    for n, kind, off, sz in params:
        if n in vals:
            fmt = FMT.get(kind) or sys.exit(f"{kind} {n} not supported")
            v = float(vals[n]) if fmt[-1] in "fd" else int(vals[n], 0)
            struct.pack_into(fmt, buf, off, v)
    meta = json.load(open(os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "offsets.json")))
    pe_idx = meta.get("process_event_index")
    if pe_idx is None:
        sys.exit("no ProcessEvent index in offsets.json (rerun uegen)")

    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    stub = await rpc_stub(dbg, pid, os.path.join(os.path.dirname(os.path.abspath(dump)), "rpc_stub.txt"))
    vt = struct.unpack("<Q", await dbg.read_memory(pid, obj, 8))[0]
    pe = struct.unpack("<Q", await dbg.read_memory(pid, vt + pe_idx * 8, 8))[0]
    mem = await dbg.allocate_memory(pid, 4096)
    await dbg.write_memory(pid, mem, bytes(buf))
    print(f"obj {obj:#x} func {func:#x} ProcessEvent {pe:#x} params @ {mem:#x} ({size} bytes)")
    rax = await dbg.call(pid, pe, obj, func, mem, rpc_stub=stub)
    out = await dbg.read_memory(pid, mem, size)
    await dbg.free_memory(pid, mem, 4096)
    print(f"returned rax={rax}")
    for n, kind, off, sz in params:
        fmt = FMT.get(kind)
        shown = struct.unpack_from(fmt, out, off)[0] if fmt else out[off:off + sz].hex()
        print(f"  {n:24} {kind:16} = {shown}")


if __name__ == "__main__":
    ueobj.run(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]))
