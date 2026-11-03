"""Give-function finder (the ReaperPy idea, no per-game profile): lists the game's own "add item" functions from the SDK headers,
finds their native implementation behind the exec thunk (read from the dump) and prints a disassembly to check the argument layout.

    python ueitems_give.py <dump.bin> [SDK_HEADERS folder]      # default: <dump folder>/SDK/SDK_HEADERS

Read-only. Needs numpy-free pure python; uses capstone for the listing when installed (pip install capstone), else raw bytes.
"""
import os, re, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ueobj

FUNC_STATIC = 0x2000
GIVE_STRONG = re.compile(r"(?i)(add|give|grant|obtain|acquire|gain|receive|insert|award|pickup|collect|set)"
                         r".*(item|inventory|loot|gold|money|coin|currency|cash|credit|resource|material)"
                         r"|(item|inventory|gold|money|coin|currency|cash|credit).*(add|give|grant|obtain|get|set)")
ID_NAME = re.compile(r"(?i)^(id|uid|key|rowname|name|tag|index)$|item.?(id|key|name|index|row|tag|def|type|data)")
CNT_NAME = re.compile(r"(?i)^(count|quantity|qty|amount|num|number|stack|stacks|itemcount|itemnum|value)$")
OWNER_BONUS = [(r"(?i)inventory|bag|storage|backpack|item", 2.5), (r"(?i)cheat", 3.0), (r"(?i)playercontroller|playerstate|player", 1.5),
               (r"(?i)gameinstance|gamemode|savegame|subsystem|profile|bfl|library", 1.0)]
FN = re.compile(r"^\s*//\s*(?P<ret>.+?)\s+(?P<name>\w+)\((?P<args>.*)\);\s*//\s*flags\s+(?P<flags>0x[0-9a-fA-F]+)(?:\s+native\s+(?P<native>0x[0-9a-fA-F]+))?")
CLS = re.compile(r"^class\s+(?P<name>\w+)\s*(?::\s*public\s+(?P<base>\w+))?")


def split_args(a):
    out, depth, cur = [], 0, ""
    for ch in a:
        if ch in "<(": depth += 1
        if ch in ">)": depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip(): out.append(cur.strip())
    res = []
    for p in out:
        m = re.match(r"(.+?)\s*(\w+)$", p)
        res.append((m.group(1).strip(), m.group(2)) if m else (p, ""))
    return res


def parse_sdk(folder):
    funcs = []
    for path in sorted(glob.glob(os.path.join(folder, "*_classes.hpp"))):
        cls = None
        for line in open(path, encoding="utf-8", errors="replace"):
            m = CLS.match(line)
            if m: cls = m.group("name"); continue
            f = FN.match(line)
            if f and cls and f.group("native"):
                funcs.append(dict(cls=cls, name=f.group("name"), ret=f.group("ret"), params=split_args(f.group("args")),
                                  flags=int(f.group("flags"), 16), native=int(f.group("native"), 16), file=os.path.basename(path)))
    return funcs


def shape(params):
    ids = [p for p in params if "*" not in p[0] and "&" not in p[0] and (p[0].split()[-1] in ("int32_t", "int", "FName", "uint32_t", "int64_t") and ID_NAME.search(p[1]))]
    cnt = [p for p in params if p[0].split()[-1] in ("int32_t", "int", "uint32_t", "int64_t", "float") and CNT_NAME.search(p[1])]
    return ids, cnt


def candidates(funcs):
    out = []
    for f in funcs:
        if not GIVE_STRONG.search(f["name"]): continue
        if any("*" in t or "&" in t and "const" not in t for t, _ in f["params"]): continue     # out parameters / pointers: skip
        ids, cnt = shape(f["params"])
        score = 4.5
        for rx, b in OWNER_BONUS:
            if re.search(rx, f["cls"]): score += b; break
        if ids and cnt: score += 3
        elif cnt: score += 1
        if f["flags"] & FUNC_STATIC: score += 0.5
        f["score"], f["ids"], f["cnt"] = score, ids, cnt
        out.append(f)
    return sorted(out, key=lambda f: -f["score"])


def disasm(code, addr, n=40):
    try:
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        return [f"{i.address:#x}: {i.mnemonic} {i.op_str}" for i in md.disasm(code, addr)][:n]
    except Exception:
        return [code[i:i + 16].hex(" ") for i in range(0, min(len(code), 160), 16)]


def find_impl(d, base, thunk_rva):
    """Native implementation behind an exec thunk: the last direct call (E8) before the first ret whose target starts like a function."""
    code = d.read(base + thunk_rva, 0x300) or b""
    calls, i = [], 0
    while i < len(code) - 5:
        if code[i] == 0xE8:
            rel = int.from_bytes(code[i + 1:i + 5], "little", signed=True)
            tgt = thunk_rva + i + 5 + rel
            if 0 < tgt < 0x20000000 and tgt != thunk_rva: calls.append((i, tgt))
        i += 1
    return code, calls


# ---- resolve: the game's own "give item" / "give money" function -> native address + argument layout ---------------------------
MONEY = re.compile(r"(?i)(add|give|grant|gain|earn|receive).*(money|gold|gil|coin|currency|cash|credit)|(money|gold|gil|coin|currency|cash).*(add|give|grant|gain)")
INTISH = ("int32_t", "int", "uint32_t", "int64_t", "uint8_t", "bool", "int16_t", "uint16_t", "uint64_t")


def _dump_name(sdk_cls):
    return sdk_cls[1:] if len(sdk_cls) > 2 and sdk_cls[0] in "UAF" and sdk_cls[1].isupper() else sdk_cls


def impl_behind_thunk(e, thunk):
    """(impl address | None, note, argument-load check). The exec thunk reads the parameters from the script stack, then calls the
    C++ implementation: the LAST direct call before the first ret (what follows the ret is the stack-protector failure path)."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    code = e.d.read(thunk, 0x400) or b""
    last, window = None, []
    for ins in md.disasm(code, thunk):
        if ins.mnemonic == "call" and ins.op_str.startswith("0x"):
            last = (int(ins.op_str, 16), list(window[-8:]))
        if ins.mnemonic.startswith("ret"):
            break
        window.append(f"{ins.mnemonic} {ins.op_str}")
    if not last:
        return None, "no direct call before the thunk's ret (inlined / tail call?)", []
    tgt, win = last
    pro = e.d.read(tgt, 4) or b""
    note = "implementation starts with push rbp; mov rbp,rsp" if pro == b"\x55\x48\x89\xE5" else f"implementation starts with {pro.hex(' ')}"
    return tgt, note, win


THIS_REGS = {"rdi", "edi", "di", "dil"}


def this_unused(e, impl, max_insn=80):
    """True when the native implementation never reads `this` (rdi): it is overwritten, or a call/ret comes first, without rdi being read.
    Then the function works on ANY object (e.g. a CheatManager that shipping builds never create). Conservative: a jump, a read of rdi
    or a copy of it = used (False)."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    for ins in md.disasm(e.d.read(impl, 0x300) or b"", impl):
        if ins.mnemonic.startswith("ret"):
            return True
        if ins.mnemonic.startswith("j"):
            return False
        if ins.mnemonic == "call":
            return False                    # rdi still holds `this`: it is passed on (or the call goes through it)
        rd, wr = ins.regs_access()
        if ins.mnemonic in ("xor", "sub") and len(ins.operands) == 2 and ins.op_str.split(",")[0].strip() == ins.op_str.split(",")[1].strip():
            rd = []                         # xor edi, edi: no read
        if {ins.reg_name(r) for r in rd} & THIS_REGS:
            return False
        if {ins.reg_name(r) for r in wr} & {"rdi", "edi"}:
            return True                     # overwritten before any use
        max_insn -= 1
        if not max_insn:
            return False
    return False


CALLER_SAVED = {"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"}


def thunk_passes_this(e, thunk, impl):
    """True when the exec thunk hands its Context (rdi) on to `impl` as `this` (rdi) without touching the object itself. False when it
    reads through Context (the function body is inlined: impl is only a helper) or calls impl with something else in rdi."""
    import capstone
    from capstone import x86
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    taint = {"rdi"}
    for ins in md.disasm(e.d.read(thunk, 0x400) or b"", thunk):
        ops = ins.operands
        if any(o.type == x86.X86_OP_MEM and {ins.reg_name(o.mem.base), ins.reg_name(o.mem.index)} & taint for o in ops):
            return False
        if ins.mnemonic == "call":
            if ins.op_str.startswith("0x") and int(ins.op_str, 16) == impl:
                return "rdi" in taint
            taint -= CALLER_SAVED
            continue
        if ins.mnemonic.startswith("ret"):
            return False
        if ins.mnemonic == "push" and ops and ops[0].type == x86.X86_OP_REG and ins.reg_name(ops[0].reg) in taint:
            return False                    # this on the stack: not followed
        if len(ops) == 2 and ops[0].type == x86.X86_OP_REG:
            dst = ins.reg_name(ops[0].reg)
            if ins.mnemonic == "mov" and ops[1].type == x86.X86_OP_REG and ins.reg_name(ops[1].reg) in taint:
                taint.add(dst)
            elif ins.mnemonic not in ("cmp", "test"):
                taint.discard(dst)
    return False


def _reg_loads(win):
    """registers loaded from the thunk's locals right before the call: ['edi', 'esi', ...]"""
    return [m.group(1) for w in win if (m := re.match(r"mov (edi|esi|edx|ecx|r8d|r9d|rdi|rsi|rdx|rcx|r8|r9), (?:dword|qword|word|byte) ptr \[rbp", w))]


def pick(e, cands, want):
    """first usable function of `want` ('item'|'money') -> dict or None"""
    from ueobj import CF
    for f in cands:
        if want == "money" and not MONEY.search(f["name"]): continue
        if want == "item" and not (f["ids"] and f["cnt"]): continue
        if not f["flags"] & FUNC_STATIC: continue                     # an instance function needs `this`: not handled
        if any(t.split()[-1] not in INTISH and t.split()[-1] != "FName" for t, _ in f["params"]): continue
        if want == "money" and (not f["params"] or f["params"][0][0].split()[-1] not in INTISH): continue
        cls = e.find(_dump_name(f["cls"]), CF["Class"])
        c, fn = cls, None
        while c and not fn:
            fn = e.member(c, f["name"], CF["Function"]); c = e.super(c)
        if not fn: continue
        thunk = e.u64(fn + e.O["UFunction_ExecFunction"])
        impl, note, win = impl_behind_thunk(e, thunk)
        if not impl: continue
        kinds = []
        for t, n in f["params"]:
            b = t.split()[-1]
            role = "other"
            if want == "item":
                role = "id" if (n, b) in [(p[1], p[0].split()[-1]) for p in f["ids"]] else "count" if any(n == c2[1] for c2 in f["cnt"]) else "other"
            else:
                role = "count" if n == f["params"][0][1] else "other"
            kinds.append(dict(name=n, type=b, role=role, size=8 if b in ("FName", "int64_t", "uint64_t") else 4 if b in ("int32_t", "int", "uint32_t") else 1 if b in ("bool", "uint8_t") else 2))
        return dict(owner=f["cls"], name=f["name"], ret=f["ret"], impl_rva=impl - e.d.base, thunk_rva=thunk - e.d.base, params=kinds, note=note,
                    loads=_reg_loads(win), score=f["score"])
    return None


def resolve(e, dump, sdk_dir=None, say=print):
    """-> dict(item=..., money=...|None) from the SDK headers next to the dump + the dump itself, or None."""
    folder = sdk_dir or os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "SDK_HEADERS")
    if not os.path.isdir(folder):
        say(f"no SDK headers at {folder}: generate the SDK for this game first (give-function mode needs it)")
        return None
    cands = candidates(parse_sdk(folder))
    try:
        item, money = pick(e, cands, "item"), pick(e, cands, "money")
    except ImportError:
        say("give-function mode needs capstone (pip install capstone)")
        return None
    if item:
        say(f"give function: {item['owner']}::{item['name']}({', '.join(p['type'] + ' ' + p['name'] for p in item['params'])}) impl base+{item['impl_rva']:#x}; {item['note']}; thunk loads {item['loads']}")
    if money:
        say(f"money function: {money['owner']}::{money['name']}({', '.join(p['type'] + ' ' + p['name'] for p in money['params'])}) impl base+{money['impl_rva']:#x}")
    return dict(item=item, money=money) if item else None



def grep_main(argv):
    """ueitems_give.py <dump.bin> --grep word1,word2 [--out file]: every SDK function (native AND blueprint) whose owner or name has a word."""
    dump = argv[0]
    words = [w.lower() for w in argv[argv.index("--grep") + 1].split(",") if w]
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    folder = os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "SDK_HEADERS")
    funcs = parse_sdk(folder)
    rows = []
    for f in funcs:
        key = (f["cls"] + "::" + f["name"]).lower()
        if any(w in key for w in words):
            rows.append(f"{f['cls']}::{f['name']}({', '.join(t + ' ' + n for t, n in f['params'])}) -> {f['ret']}  flags {f['flags']:#x}"
                        f"{' STATIC' if f['flags'] & FUNC_STATIC else ''}  thunk {f['native']:#x}")
    rows.sort()
    text = f"{len(funcs)} functions, {len(rows)} match {words}\n" + "\n".join(rows) + "\n"
    if out:
        open(out, "w", encoding="utf-8").write(text)
        print(f"{len(rows)} matches written to {out}")
    else:
        print(text)


def main():
    if "--grep" in sys.argv:
        return grep_main(sys.argv[1:])
    dump = sys.argv[1]
    folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "SDK_HEADERS")
    funcs = parse_sdk(folder)
    print(f"{len(funcs)} native functions in {folder}")
    cand = candidates(funcs)
    print(f"{len(cand)} give-like candidates; best 12:\n")
    for f in cand[:12]:
        print(f"  [{f['score']:.1f}] {f['cls']}::{f['name']}({', '.join(t + ' ' + n for t, n in f['params'])}) -> {f['ret']}  flags {f['flags']:#x}{' STATIC' if f['flags'] & FUNC_STATIC else ''}  thunk RVA {f['native']:#x}")
    e = ueobj.open_engine(dump, log=lambda *a: None); d = e.d
    print(f"\nmodule base {d.base:#x}")
    known = set()
    for f in cand[:3]:
        code, calls = find_impl(d, d.base, f["native"])
        print(f"\n== {f['cls']}::{f['name']}  thunk {f['native']:#x}: all direct calls (offset -> target RVA): "
              + ", ".join(f"+{o:#x}->{t:#x}" for o, t in calls))
        for line in disasm(code, d.base + f["native"], 150): print("   ", line)
        for o, t in calls:
            if t in known: continue
            known.add(t)
            impl = d.read(d.base + t, 0x100) or b""
            print(f"\n   -- call target RVA {t:#x} (from thunk +{o:#x}): first bytes {impl[:16].hex(' ')}")
            for line in disasm(impl, d.base + t, 14): print("       ", line)


if __name__ == "__main__":
    main()
