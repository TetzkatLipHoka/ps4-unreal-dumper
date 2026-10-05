#!/usr/bin/env python3
"""peek.py - read, write, freeze and watch memory of the RUNNING game through ps4debug, and find out which instruction
writes a value (hardware watchpoint).  Console: --host IP or environment variable PS4_HOST

  python peek.py read   0x1B814D667C --host 192.168.x.y   # read a float
  python peek.py write  0x1B814D667C 100                  # write a float (and check whether it sticks)
  python peek.py freeze 0x1B814D667C 100                  # rewrite every 50 ms, Ctrl+C stops
  python peek.py watch  0x1B814D667C                      # watch the value live (read only, harmless)
  python peek.py trace  0x1B814D667C                      # WHICH instruction writes here? (experimental, see below)

  --type float|int32|int64|double|u8|u32|u64   (default float)

trace: sets a hardware write watchpoint on the address; take damage in the game and the console reports the
instruction. Its address matches the addresses in your dumps / in Ghidra (image base 0x400000).
Windows firewall: the console connects back to THIS PC on port 755 -> allow Python for 'Private networks'.
"""
import argparse, asyncio, collections, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FMT = {"float": "<f", "int32": "<i", "int64": "<q", "double": "<d", "u8": "<B", "u32": "<I", "u64": "<Q"}
PREFIXES = {0x66, 0xF2, 0xF3, 0xF0, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65, 0x67}
IMAGE_BASE = 0x400000
REG64 = {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"}


def pack(typ, text):
    f = FMT[typ]
    return struct.pack(f, float(text) if f[1] in "fd" else int(text, 0))


def unpack(typ, data):
    return struct.unpack(FMT[typ], bytes(data[:struct.calcsize(FMT[typ])]))[0]


def show(typ, data):
    v = unpack(typ, data)
    return f"{v:.6g}" if FMT[typ][1] in "fd" else f"{v} ({v:#x})"


async def connect(host):
    import ps4debug
    import ueobj
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)           # like ps4ue.py: eboot.bin or by title id
    return dbg, pid


async def read(dbg, pid, addr, n):
    data = await dbg.read_memory(pid, addr, n)
    if data is None:
        sys.exit(f"{addr:#x} not readable - game restarted / new area loaded? The address is only valid for the session of the dump.")
    return data


# ---------------------------------------------------------------------------------------------------------------------
async def cmd_read(dbg, pid, a):
    n = struct.calcsize(FMT[a.type])
    print(f"{a.address:#x} = {show(a.type, await read(dbg, pid, a.address, n))}  ({a.type})")


async def cmd_write(dbg, pid, a):
    n = struct.calcsize(FMT[a.type])
    before = await read(dbg, pid, a.address, n)
    st = await dbg.write_memory(pid, a.address, pack(a.type, a.value))
    after = await read(dbg, pid, a.address, n)
    await asyncio.sleep(0.5)
    later = await read(dbg, pid, a.address, n)
    print(f"{a.address:#x}: {show(a.type, before)}  ->  {show(a.type, after)}   (status {getattr(st, 'name', st)})")
    if bytes(later) != bytes(after):
        print(f"  after 0.5 s: {show(a.type, later)}  -> the game overwrites the value again (mirror/display copy or regeneration). "
              f"Hold it with 'freeze' or look for the real source with 'trace'.")
    else:
        print("  Value sticks after 0.5 s. Check in the game whether the bar changes (maybe only after the next hit/tick).")


async def cmd_freeze(dbg, pid, a):
    data = pack(a.type, a.value)
    n, writes, t0 = len(data), 0, time.time()
    print(f"freezing {a.address:#x} at {a.value} ({a.type}), Ctrl+C stops ...")
    try:
        while True:
            await dbg.write_memory(pid, a.address, data)
            writes += 1
            if writes % max(1, int(1 / a.interval)) == 0:
                cur = await read(dbg, pid, a.address, n)
                print(f"  {time.time() - t0:6.1f}s  {writes} writes, current value {show(a.type, cur)}", end="\r")
            await asyncio.sleep(a.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopped.")


async def cmd_watch(dbg, pid, a):
    n = struct.calcsize(FMT[a.type])
    last, t0, changes = None, time.time(), 0
    print(f"watching {a.address:#x} ({a.type}), Ctrl+C stops ...")
    try:
        while True:
            cur = bytes(await read(dbg, pid, a.address, n))
            if cur != last:
                if last is not None:
                    changes += 1
                print(f"  {time.time() - t0:7.2f}s  {show(a.type, cur)}")
                last = cur
            await asyncio.sleep(a.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\nstopped, {changes} changes.")


# ---------------------------------------------------------------------------------------------------------------------
# trace: hardware watchpoint -> writing instruction
# ---------------------------------------------------------------------------------------------------------------------
def effective_address(cs, ins, op, regs):
    """Effective address of the memory operand with the register values at the trap (None if not computable)."""
    m = op.mem
    if m.segment not in (0, cs.x86.X86_REG_DS, cs.x86.X86_REG_ES, cs.x86.X86_REG_SS, cs.x86.X86_REG_CS):
        return None                                              # fs:/gs: -> TLS, not comparable
    ea = m.disp
    for reg, scale in ((m.base, 1), (m.index, m.scale)):
        if not reg:
            continue
        name = ins.reg_name(reg)
        if name == "rip":
            ea += ins.address + ins.size
        elif name in REG64:
            ea += getattr(regs, name) * scale
        else:
            return None
    return ea & 0xFFFFFFFFFFFFFFFF


def writer_candidates(code, rip, regs=None, target=None, size=4):
    """code = the last bytes BEFORE rip (the trap fires AFTER the writing instruction). Result: possible instructions,
    most likely first. Decoding from the middle of an instruction nearly always yields 'something' (e.g. 00 00 =
    add [rax],al); so the first criterion is whether the EFFECTIVE ADDRESS of the memory operand, with the registers
    of the trap, is exactly the watched address. Then: writes to memory, no prefix/0F/REX right before it, shorter
    first."""
    try:
        import capstone as cs
    except ImportError:
        return None
    md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    md.detail = True
    out = []
    for back in range(1, min(16, len(code) + 1)):
        start = len(code) - back
        ins = next(md.disasm(bytes(code[start:]), rip - back, 1), None)
        if not ins or ins.size != back:
            continue
        mem = [op for op in ins.operands if op.type == cs.x86.X86_OP_MEM]
        writes = any(op.access & cs.CS_AC_WRITE for op in mem)
        ea_ok = False
        if regs is not None and target is not None:
            for op in mem:
                ea = effective_address(cs, ins, op, regs)
                ea_ok = ea_ok or (ea is not None and ea <= target < ea + max(size, op.size or 1))
        plausible = start == 0 or not (code[start - 1] in PREFIXES or code[start - 1] == 0x0F or 0x40 <= code[start - 1] <= 0x4F)
        out.append((not ea_ok, not writes, not plausible, back, ins))
    out.sort(key=lambda t: t[:4])
    return [t[4] for t in out]


def make_handler(on_interrupt):
    """Own handler instead of DebuggingContext.debug_connected: the original one looks the hit up in the software
    breakpoints only (next(...) -> StopIteration) and crashes on hardware watchpoints."""
    import ps4debug.core as core

    async def handler(self, reader, writer):
        length = core.DebuggerInterrupt.sizeof()
        while not self.stop_flag.is_set():
            try:
                data = await reader.readexactly(length)
            except asyncio.IncompleteReadError:
                writer.close()
                return
            try:
                on_interrupt(core.DebuggerInterrupt.parse(data))
            finally:
                await self.resume_process()           # let the game continue right away
    return handler


async def cmd_trace(dbg, pid, a):
    import ps4debug
    import ps4debug.core as core
    queue = asyncio.Queue()
    ps4debug.DebuggingContext.debug_connected = make_handler(queue.put_nowait)
    length = {1: core.WatchPointLengthType.DBREG_DR7_LEN_1, 2: core.WatchPointLengthType.DBREG_DR7_LEN_2,
              4: core.WatchPointLengthType.DBREG_DR7_LEN_4, 8: core.WatchPointLengthType.DBREG_DR7_LEN_8}[
        struct.calcsize(FMT[a.type])]
    if a.address % struct.calcsize(FMT[a.type]):
        sys.exit("The address must be aligned to the type size (hardware watchpoint).")
    counts, first = collections.Counter(), {}
    t0 = time.time()
    print(f"Hardware write watchpoint on {a.address:#x} ({a.seconds:.0f}s or {a.max_hits} hits).")
    print("-> NOW take damage or heal in the game. Waiting for hits ...\n")
    async with dbg.debugger(pid, resume=True) as ctx:
        st = await ctx.set_watchpoint(0, True, a.address, length, core.WatchPointBreakType.DBREG_DR7_WRONLY)
        if st != core.ResponseCode.SUCCESS:
            sys.exit(f"Could not set the watchpoint: {st}. (Debugger already in use by another tool?)")
        try:
            n = 0
            while n < a.max_hits and time.time() - t0 < a.seconds:
                try:
                    intr = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                n += 1
                rip = intr.regs.rip
                code = await dbg.read_memory(pid, rip - 16, 16)
                cands = writer_candidates(code, rip, intr.regs, a.address, struct.calcsize(FMT[a.type])) if code else None
                if cands:
                    ins = cands[0]
                    key = ins.address
                    first.setdefault(key, (ins, intr))
                    counts[key] += 1
                    print(f"[{time.time() - t0:6.1f}s] thread {intr.name.strip() or intr.lwpid}: {ins.address:#x}  "
                          f"{bytes(ins.bytes).hex(' ')}  {ins.mnemonic} {ins.op_str}   (image+{ins.address - IMAGE_BASE:#x})")
                    if len(cands) > 1:
                        print("           alternative decoding: " + " | ".join(
                            f"{c.address:#x} {c.mnemonic} {c.op_str}" for c in cands[1:3]))
                else:
                    counts[rip] += 1
                    print(f"[{time.time() - t0:6.1f}s] RIP after the write: {rip:#x}  (image+{rip - IMAGE_BASE:#x})"
                          f"{'  [capstone missing: pip install capstone]' if cands is None else ''}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await ctx.set_watchpoint(0, False, a.address, length, core.WatchPointBreakType.DBREG_DR7_WRONLY)
    print("\nSummary (most frequent writing instruction first):")
    if not counts:
        print("  no hits - did the value really change in that time? (address still current? check with 'watch')")
    for addr, c in counts.most_common():
        ins = first.get(addr, (None,))[0]
        print(f"  {c:4d}x  {addr:#x}  {ins.mnemonic + ' ' + ins.op_str if ins else ''}")
    if counts:
        best = counts.most_common(1)[0][0]
        print(f"\nDisassembly address: {best:#x}  (image+{best - IMAGE_BASE:#x})")


# ---------------------------------------------------------------------------------------------------------------------
async def amain(a):
    dbg, pid = await connect(a.host)
    print(f"game process pid {pid} on {a.host}")
    await {"read": cmd_read, "write": cmd_write, "freeze": cmd_freeze, "watch": cmd_watch, "trace": cmd_trace}[a.cmd](dbg, pid, a)


def main(argv=None):
    ap = argparse.ArgumentParser(description="read/write/freeze/watch/trace memory of the running game")
    ap.add_argument("cmd", choices=["read", "write", "freeze", "watch", "trace"])
    ap.add_argument("address", type=lambda x: int(x, 0))
    ap.add_argument("value", nargs="?", help="value for write/freeze")
    ap.add_argument("--type", choices=sorted(FMT), default="float")
    ap.add_argument("--host", default=os.environ.get("PS4_HOST"), help="console IP (default: environment variable PS4_HOST)")
    ap.add_argument("--interval", type=float, default=0.05, help="seconds between writes/reads (freeze/watch)")
    ap.add_argument("--seconds", type=float, default=60, help="trace: maximum duration")
    ap.add_argument("--max-hits", type=int, default=40, help="trace: stop after this many hits")
    a = ap.parse_args(argv)
    if not a.host:
        ap.error("no console given: pass --host 192.168.x.y or set PS4_HOST")
    if a.cmd in ("write", "freeze") and a.value is None:
        ap.error(f"{a.cmd} needs a value, e.g.:  python peek.py {a.cmd} {a.address:#x} 100")
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        print("\naborted.")


if __name__ == "__main__":
    main()
