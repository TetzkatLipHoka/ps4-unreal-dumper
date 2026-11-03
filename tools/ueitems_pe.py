"""Generic "give item / give money" through UObject::ProcessEvent - for games whose add-item function is an instance function,
a Blueprint function or takes a UClass* (no static native entry point to call directly).

Everything is derived from the dump itself (UFunction objects, their parameter properties, class layouts): no SDK headers and no
capstone needed.  Two halves:

  resolve(e)                    offline, at scan time: find the best add-item / add-money UFunction and describe its parameters
  call_live(...) / LiveGraph    at give time: find the live object (walk object pointers from GWorld, using the dump's class layouts),
                                find the live UFunction in its class, fill the parameter buffer and call ProcessEvent through ps4debug's RPC.

Works in any session: nothing is read from the dump's addresses except the item UClass objects (checked live first).
"""
import re, struct, sys
from collections import deque

import ueobj
from ueobj import CF, RF_ClassDefaultObject
import ueitems_give as G

CPF_Parm, CPF_Return = 0x80, 0x400
FUNC_Native, FUNC_Static = 0x400, 0x2000

PREDICATE = re.compile(r"^(?:[Ss]_|[Cc]_|[Ss]erver_|[Cc]lient_)?(?:[Cc]an|[Ii]s|[Hh]as|[Ss]hould|[Gg]et|[Ff]ind|[Cc]heck|[Nn]eeds?|[Ww]ill|[Dd]oes|[Ww]ould|[Vv]alidate|[Cc]ompute|[Cc]alc|[Qq]uery|[Cc]ontains|[Cc]ount)(?=[A-Z_0-9]|$)")
NAME_OK = lambda n: bool(G.GIVE_STRONG.search(n) or G.MONEY.search(n)) and "__delegatesignature" not in n.lower() and not PREDICATE.search(n)
NEG = re.compile(r"(?i)notify|widget|preview|shop|sell|remove|spawn|effect|anim|tutorial|display|achiev|ability|price|cost|log|event$|^on|^k2_|delegate|rpc|sound|audio|vfx|fx|analytic|telemetry|record|track|stat$")
COUNT_NAME = re.compile(r"(?i)^(count|quantity|qty|amount|num|number|stacks?|stack_?count|item_?count|delta|value|new_?amount|add_?amount|in_?amount)$|stack|amount|count|quantity")
ID_INT_NAME = re.compile(r"(?i)^(id|item_?id|row_?id|index|key)$|item.?(id|index|key)")
FULL_NAME = re.compile(r"(?i)percent|durability|condition|quality|health|ratio")      # float parameters passed as 1.0, not 0
WORLD_NAME = re.compile(r"(?i)^(__)?world.?context")
SKIP_CLS = re.compile(r"(?i)mesh|material|texture|skelet|anim|sound|audio|niagara|particle|spline|camera|light|collision|physics|capsule|arrow|render|widget|navigation|brush|landscape|decal|sky|fog|postprocess|streaming|sprite|billboard|volume|trigger")
SKIP_PROP = re.compile(r"(?i)^(outer|owner|persistentlevel|levels|streaminglevels|currentlevel|levelcollections|netdriver|demonetdriver|actors|allactors|world|worldsettings|navigationsystem|physicsscene|aisystem|audiodevice|controllingnetdriver)$")


ACTION = re.compile(r"(?i)(add|give|grant|obtain|acquire|gain|receive|award|pick_?up|collect|insert|earn|increase)")
ITEM_WORD = re.compile(r"(?i)(item|inventory|loot|stack|material|resource|pickup|weapon|equip|ingredient|consumable|artifact|relic|organ|gear|accessory|card)")
INV_OWNER = re.compile(r"(?i)inventory|item|bag|backpack|storage|loot|equip")
ENGINE_OWNER = re.compile(r"(?i)geometry|niagara|slate|widget|listview|treeview|material|mesh|anim|sequenc|chaos|landscape|metasound|audio|movie|libraryfunctions|rig|spline|splinecomponent|texture|render|physics|navigation|mass|statetree|dataprep|datasmith|usd|interchange|mediaplayer|paper2d|foliage|decal|vehicle|camera|light|sound|particle|cinematic")
ENGINE_PKG = re.compile(r"(?i)^/script/(coreuobject|engine|slate|slatecore|umg|geometry\w*|niagara\w*|movie\w*|enhancedinput|aimodule|navigationsystem|chaos\w*|landscape|foliage|rendercore|metasound\w*|audio\w*|media\w*|animationcore|controlrig\w*|mass\w*|statetree\w*|interchange\w*|usd\w*|commonui|xr\w*|paper2d|cinematic\w*|levelsequence|gameplaytags|inputcore|onlinesubsystem\w*)$")
MIN_ITEM, MIN_MONEY = 7.5, 6.5


class PEError(Exception):
    pass


# ============================================================ calling code in the game: both ps4debug APIs =========================
REGS = ("rdi", "rsi", "rdx", "rcx", "r8", "r9")


async def get_stub(dbg, pid, cache_file=None):
    """RPC stub address: new ps4debug (get_rpc finds / installs it) or the older install_rpc API (cached in a file like uecall.py)."""
    if hasattr(dbg, "get_rpc"):
        return await dbg.get_rpc(pid)
    import uecall
    return await uecall.rpc_stub(dbg, pid, cache_file or "rpc_stub.txt")


async def rpc_call(dbg, pid, addr, args, stub):
    """addr(*args) in the game with System V registers (rdi rsi rdx rcx r8 r9) -> rax as an int."""
    args = [int(a) & (2**64 - 1) for a in args][:6]
    if hasattr(dbg, "get_rpc"):                          # ps4debug with structured registers
        Regs = None
        for mod in ("ps4debug.core.models", "ps4debug.core", "ps4debug"):
            try:
                Regs = getattr(__import__(mod, fromlist=["CallRegisters"]), "CallRegisters")
                break
            except (ImportError, AttributeError):
                continue
        if Regs is None:
            raise PEError("ps4debug.CallRegisters not found: unsupported ps4debug version")
        r = await dbg.call(pid, addr, Regs(**dict(zip(REGS, args))), rpc_stub=stub)
        return int.from_bytes(r, "little") if isinstance(r, (bytes, bytearray)) else r
    return await dbg.call(pid, addr, *args, rpc_stub=stub)          # older API: call(pid, rip, rdi, rsi, ..., rpc_stub=)


async def game_thread_call(dbg, pid, pe, obj, func, buf, timeout=5.0, say=print):
    """ProcessEvent(obj, func, params) on the game thread: a one-shot hook on ProcessEvent's entry makes the next game call to it run ours
    first (RPC runs on the debugger's thread: RuneScape's TryGiveItemToPlayer loads the item icon -> FlushAsyncLoading -> terminate).
    -> the parameter buffer after the call. Block: +0 flag (1 armed, 2 running, 3 done), +8 obj, +0x10 func, +0x18 params, +0x20 pe,
    +0x28 return address, +0x40 code, +0x800 params."""
    import asyncio, capstone
    head = await dbg.read_memory(pid, pe, 32)
    if not head:
        raise PEError(f"ProcessEvent {pe:#x} not readable")
    B = await dbg.allocate_memory(pid, 4096)
    cave = B + 0x40
    far = abs(cave - (pe + 5)) >= 2**31
    need, n = (14 if far else 5), 0
    for ins in capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64).disasm(head, pe):
        if "rip" in ins.op_str or ins.mnemonic.startswith(("j", "call", "loop")):
            await dbg.free_memory(pid, B, 4096)
            raise PEError(f"ProcessEvent starts with {ins.mnemonic} {ins.op_str}: turn off cheats that hook ProcessEvent "
                          "(item cheat, master code), restart the game, try again")
        n += ins.size
        if n >= need:
            break
    if n < need:
        await dbg.free_memory(pid, B, 4096)
        raise PEError("cannot decode ProcessEvent's first instructions")
    code = bytearray()

    def rip(target, tail=0):                                   # disp32 to `target` from the end of the instruction (opcode already in code)
        return struct.pack("<i", target - (cave + len(code) + 4 + tail))
    code += bytes.fromhex("50 51 b8 01000000 b9 02000000")     # push rax; push rcx; mov eax, 1; mov ecx, 2
    code += bytes.fromhex("f0 0f b1 0d")
    code += rip(B)                                             # lock cmpxchg [flag], ecx: only the first caller takes it
    # ponytail: "first caller" is whichever thread calls ProcessEvent next; UE does that on the game thread almost always.
    # Check IsInGameThread here (GGameThreadId) if a game ever runs it elsewhere.
    code += bytes.fromhex("75 00")
    jne = len(code) - 1
    code += bytes.fromhex("52 56 57 4150 4151 4152 4153")      # push rdx, rsi, rdi, r8-r11 (9 pushes: rsp 16-aligned at the call)
    for op, off in (("48 8b 3d", 8), ("48 8b 35", 0x10), ("48 8b 15", 0x18)):
        code += bytes.fromhex(op)
        code += rip(B + off)                                   # rdi = obj, rsi = func, rdx = params
    code += bytes.fromhex("ff 15")
    code += rip(B + 0x20)                                      # call [pe]: re-enters the hook, flag is 2 -> straight through
    code += bytes.fromhex("c7 05")
    code += rip(B, 4) + struct.pack("<I", 3)                   # mov dword [flag], 3
    code += bytes.fromhex("415b 415a 4159 4158 5f 5e 5a")
    code[jne] = len(code) - jne - 1
    code += bytes.fromhex("59 58") + head[:n]                  # pop rcx; pop rax; the instructions the patch replaces
    code += bytes.fromhex("ff 25")
    code += rip(B + 0x28)                                      # jmp [pe + n]
    params = B + 0x800
    blk = struct.pack("<QQQQQQ", 0, obj, func, params, pe, pe + n) + bytes(0x10) + bytes(code)
    patch = (bytes.fromhex("ff 25 00000000") + struct.pack("<Q", cave) if far else b"\xe9" + struct.pack("<i", cave - (pe + 5)))
    patch += b"\x90" * (n - len(patch))
    await dbg.write_memory(pid, B, blk)
    await dbg.write_memory(pid, params, bytes(buf))
    await dbg.write_memory(pid, B, b"\x01")
    say(f"   obj {obj:#x} func {func:#x} ProcessEvent {pe:#x} (game thread, hook {n} bytes) params @ {params:#x}")
    flag = 1
    try:
        await dbg.write_memory(pid, pe, patch)
        t = 0.0
        while t < timeout or flag == 2:                        # once running, wait for it however long it takes
            await asyncio.sleep(0.05)
            t += 0.05
            flag = ((await dbg.read_memory(pid, B, 1)) or b"\0")[0]
            if flag == 3:
                break
    finally:
        await dbg.write_memory(pid, pe, head[:n])
    await asyncio.sleep(0.3)                                   # threads already inside the cave leave it before it is freed
    flag = ((await dbg.read_memory(pid, B, 1)) or b"\0")[0]
    out = await dbg.read_memory(pid, params, len(buf))
    try:
        await dbg.free_memory(pid, B, 4096)
    except Exception:
        pass
    if flag != 3:
        raise PEError(f"the game did not call ProcessEvent within {timeout:.0f} s (paused / in a menu?)")
    return out


# ============================================================ offline: find the function and describe its parameters ===========
def describe(e, fn):
    """parameter list of a UFunction from its UProperty/FProperty children flagged CPF_Parm."""
    ps = []
    for p in e.properties(fn):
        fl = e.val(p + e.O["Property_PropertyFlags"], "<Q") or 0
        if not fl & CPF_Parm:
            continue
        kind = e.ffield_class_name(p)
        d = dict(name=e.ffield_name(p), ptype=kind, off=e.prop_offset(p), size=e.prop_size(p) * max(1, e.i32(p + e.O["Property_ArrayDim"]) or 1),
                 ret=bool(fl & CPF_Return))
        if kind in ("ClassProperty", "SoftClassProperty") and "ClassProperty_MetaClass" in e.O:
            meta = e.ptr(p + e.O["ClassProperty_MetaClass"])
            d["meta"] = e.name(meta) if meta else None
            d["meta_addr"] = meta or 0
        elif kind == "ObjectProperty":
            oc = e.ptr(p + e.O["ObjectProperty_Class"])
            d["ocls"] = e.name(oc) if oc else ""
            d["ocls_addr"] = oc or 0
        elif kind == "StructProperty":
            st = e.ptr(p + e.O["StructProperty_Struct"])
            d["sname"] = e.name(st) if st else ""
        ps.append(d)
    return ps


def assign_roles(ps):
    """-> (params with role/type, id kind, ok). Roles: id | count | world | pawn | other.  ok = usable as an item function (one id + one count)."""
    ids, cnts = [], []
    for d in ps:
        k, n = d["ptype"], d["name"] or ""
        d["role"] = "other"
        if d["ret"]:
            continue
        if k == "ObjectProperty" and WORLD_NAME.search(n):
            d["role"] = "world"
        elif k == "ObjectProperty" and re.search(r"(?i)controller", d.get("ocls") or ""):
            d["role"] = "pc"                      # TryGiveItemToPlayer(PlayerController, ItemData, Count): never pass null
        elif k == "ObjectProperty" and re.search(r"(?i)pawn|character|playerchar", (d.get("ocls") or "") + n):
            d["role"] = "pawn"
        elif k == "ClassProperty":
            ids.append((0, d))
        elif k == "ObjectProperty" and ITEM_WORD.search((d.get("ocls") or "") + n):
            ids.append((1, d))                    # item definition object (data asset): RuneScape AddItemByData(ItemData, Count)
        elif k == "StructProperty" and d.get("sname") == "GameplayTag":
            ids.append((1, d))                    # FGameplayTag {FName TagName}: Valor Mortis AddInventoryItem(Tag, Value)
        elif k == "NameProperty":
            ids.append((1, d))
        elif k in ("IntProperty", "UInt32Property", "Int64Property"):
            if COUNT_NAME.search(n):
                cnts.append(d)
            elif ID_INT_NAME.search(n):
                ids.append((2, d))
        elif k in ("FloatProperty", "DoubleProperty") and COUNT_NAME.search(n):
            cnts.append(d)
        elif k == "FloatProperty" and FULL_NAME.search(n):
            d["role"] = "full"
    idp = min(ids, key=lambda t: t[0])[1] if ids else None
    if idp is not None:
        idp["role"] = "id"
        idp["type"] = {"ClassProperty": "UClass", "NameProperty": "FName", "ObjectProperty": "UObject",
                       "StructProperty": "FName"}.get(idp["ptype"], "int32_t")
        if idp["ptype"] == "ObjectProperty":      # catalog = instances of the declared class (meta, as for a UClass parameter)
            idp["meta"], idp["meta_addr"] = idp["ocls"], idp["ocls_addr"]
    if not cnts and idp is not None:          # one unnamed int left over: that is the amount
        rest = [d for d in ps if d["role"] == "other" and not d["ret"] and d["ptype"] in ("IntProperty", "UInt32Property")
                and not re.search(r"(?i)slot|index|level|seed", d["name"] or "")]      # AddItemToSlot(Item, SlotIndex): no amount
        if len(rest) == 1:
            cnts = rest
    if cnts:
        cnts[0]["role"] = "count"
    for d in ps:
        if "type" not in d:
            d["type"] = {"IntProperty": "int32_t", "UInt32Property": "uint32_t", "Int64Property": "int64_t", "FloatProperty": "float",
                         "DoubleProperty": "double"}.get(d["ptype"], d["ptype"])
    return ps, idp, bool(cnts)


def _score(owner, name, flags, idp, ps, pkg=""):
    s = 4.5
    if ENGINE_PKG.search(pkg or "") or ENGINE_OWNER.search(owner):
        s -= 6
    elif (pkg or "").lower().startswith("/game/"):
        s += 1.0
    for rx, b in G.OWNER_BONUS:
        if re.search(rx, owner):
            s += b
            break
    if idp is not None:
        s += 3 if idp["ptype"] != "IntProperty" else 2.5
        if idp.get("meta") and re.search(r"(?i)item|inventory|equip|object|pickup", idp["meta"]):
            s += 1
    if flags & FUNC_Native:
        s += 1.5
    if flags & FUNC_Static:
        s += 0.5
    if NEG.search(name):
        s -= 3
    s -= 0.15 * sum(1 for d in ps if d["role"] == "other" and not d["ret"])
    if any(d["role"] == "other" and not d["ret"] and re.search(r"(?i)slot", d["name"] or "") for d in ps):
        s -= 2                                    # we pass slot 0: taken in any real inventory (RuneScape AddItemByDataToSlot -> false)
    return s


def functions(e, ok=NAME_OK):
    """(UFunction, owner UClass, name) of every function whose name passes `ok` (default: add/give/grant ... item/money)."""
    for i, o in e.arr:
        n = e.name(o)
        if not n or not ok(n) or not e.isa(o, CF["Function"]):
            continue
        owner = e.outer(o)
        if owner and e.isa(owner, CF["Class"]):
            yield o, owner, n


class _Instances:
    """Which classes have a (non-default) object in the dump, subclasses counted for their bases."""

    def __init__(self, e):
        self.e, self.have = e, None

    def __contains__(self, cls):
        if self.have is None:
            e, self.have = self.e, set()
            for c in {e.cls(o) for i, o in e.arr if not (e.flags(o) or 0) & RF_ClassDefaultObject}:
                n = 0
                while c and c not in self.have and n < 40:
                    self.have.add(c)
                    c, n = e.super(c), n + 1
        return cls in self.have


def _entry(e, fn, owner, n, ps, idp, insts, say):
    """JSON-able description of one UFunction for call_live / the cheat export. An instance function whose class has no object in the
    dump gets this_unused=True when its native code never reads `this` (called on any object then), else a lower score."""
    flags = e.val(fn + e.O["UFunction_FunctionFlags"], "<I") or 0
    oname = e.name(owner)
    pk = e.outer(owner)
    pkg = (e.name(pk) or "") if pk else ""
    ent = dict(via="pe", owner=oname, name=n, static=bool(flags & FUNC_Static), native=bool(flags & FUNC_Native),
               params=[dict(name=d["name"], type=d["type"], role=d["role"], size=d["size"], off=d["off"], ptype=d["ptype"], ret=d["ret"],
                            meta=d.get("meta"), meta_addr=d.get("meta_addr", 0), sname=d.get("sname")) for d in ps],
               score=_score(oname, n, flags, idp, ps, pkg))
    if not ent["static"] and owner not in insts:
        unused = False
        thunk = e.u64(fn + e.O["UFunction_ExecFunction"]) if ent["native"] and "UFunction_ExecFunction" in e.O else 0
        if thunk:
            try:
                impl = G.impl_behind_thunk(e, thunk)[0]
                unused = bool(impl) and G.thunk_passes_this(e, thunk, impl) and G.this_unused(e, impl)
            except ImportError:
                pass
        if unused:
            ent["this_unused"] = True
        else:
            ent["score"] -= 3               # no object of the class in the dump: probably not reachable
    return ent


def resolve(e, say=print, keep=4, tested=None):
    """-> dict(item=..., item_alts=[...], money=...|None, expand=...|None) or None.  Entries are JSON-able (see call_live).
    tested = {"Owner::Name": "ok"|"fail"} from earlier console tests: +4 / -8."""
    items, money, insts = [], [], _Instances(e)
    for fn, owner, n in functions(e):
        ps, idp, has_cnt = assign_roles(describe(e, fn))
        if not has_cnt or any(d["role"] == "other" and not d["ret"] and d["ptype"] == "ObjectProperty" for d in ps):
            continue            # an object parameter we cannot fill would go in as null (Stupid Never Dies AddEventCommand_*(EventData, ...))
        is_item = idp is not None and ACTION.search(n) and (ITEM_WORD.search(n) or INV_OWNER.search(e.name(owner)))
        if not is_item and not (idp is None and G.MONEY.search(n)):
            continue
        ent = _entry(e, fn, owner, n, ps, idp, insts, say)
        ent["score"] += {"ok": 4, "fail": -8}.get((tested or {}).get(f"{ent['owner']}::{n}"), 0)
        if is_item and ent["score"] >= MIN_ITEM:
            items.append(ent)
        elif not is_item and ent["score"] >= MIN_MONEY:
            money.append(ent)
    items.sort(key=lambda x: -x["score"])
    money.sort(key=lambda x: -x["score"])
    if not items:
        say("process-event mode: no add-item function (id + amount parameters) found in the dump")
        return None
    best = items[0]
    say("process-event give function: %s::%s(%s) score %.1f%s" % (best["owner"], best["name"], ", ".join(f"{p['type']} {p['name']}" for p in best["params"] if p["role"] != "other"),
                                                         best["score"], " [static]" if best["static"] else " [instance]"))
    for alt in items[1:keep]:
        say(f"   alt: {alt['owner']}::{alt['name']} score {alt['score']:.1f}")
    if money:
        say(f"process-event money function: {money[0]['owner']}::{money[0]['name']}")
    return dict(item=best, item_alts=items[1:keep], money=money[0] if money else None, money_alts=money[1:keep])


EXPAND_VERB = re.compile(r"(?i)expan|extend|unlock|upgrade|increas|enlarge|grow")
EXPAND_NOUN = re.compile(r"(?i)slot|capacit|inventor|backpack|bag|pocket|storage|warehouse|stash|pouch")
EXPAND_STRONG = re.compile(r"(?i)capacit|inventor|backpack|bag|pocket|storage|warehouse|stash|pouch")
EXPAND_BAD = re.compile(r"(?i)^(get|is|can|has|on|set|check|calc|update|remove|lock|should|find|bp_?get|k2_)|delegate|widget|notify|preview")
EXPAND_BONUS = (("max", 3.0), ("all", 1.5), ("full", 1.5), ("dev", 1.0), ("cheat", 1.0), ("level", 1.0))


def find_expand(e, say=print, keep=3):
    """Parameterless functions that enlarge the inventory ("DevExpansionEnableSlotByMaxLevel") -> [entry] best first, [] when the game
    has none (no "inventory to max" cheat then)."""
    out, insts = [], _Instances(e)
    ok = lambda n: bool(EXPAND_VERB.search(n) and EXPAND_NOUN.search(n)) and not EXPAND_BAD.search(n)
    for fn, owner, n in functions(e, ok):
        ps = describe(e, fn)
        if any(not d["ret"] for d in ps):
            continue                                        # nothing to fill in: parameterless only
        oname = e.name(owner)
        if not EXPAND_STRONG.search(n) and not (INV_OWNER.search(oname) or re.search(r"(?i)expan", n)):
            continue                                        # "slot" alone: RuneInSlot_IncreaseLevel (Mortal Shell) is not the inventory
        pk = e.outer(owner)
        if ENGINE_PKG.search((e.name(pk) or "") if pk else "") or ENGINE_OWNER.search(oname) or re.search(r"(?i)widget|hud|menu|view", oname):
            continue
        for d in ps:
            d["role"], d["type"] = "other", d["ptype"]
        ent = _entry(e, fn, owner, n, ps, None, insts, say)
        low = n.lower()
        ent["score"] = 4.0 + sum(w for k, w in EXPAND_BONUS if k in low) + (1.0 if INV_OWNER.search(oname) else 0) \
            + (0.5 if ent["native"] else 0) - (3 if not ent["static"] and not ent.get("this_unused") and owner not in insts else 0)
        out.append(ent)
    out.sort(key=lambda x: -x["score"])
    if out:
        say(f"inventory expansion function: {out[0]['owner']}::{out[0]['name']} score {out[0]['score']:.1f}")
    return out[:keep]


def class_catalog(e, meta):
    """Items whose identity is a UClass (blueprint item definitions): every class deriving from the parameter's meta class."""
    import ueitems as U
    out = []
    vague = (e.name(meta) or "").lower() in ("object", "actor", "pawn", "character", "dataasset", "primarydataasset")   # too generic a base: keep item-like names
    for i, o in e.arr:
        if o == meta or (e.flags(o) or 0) & RF_ClassDefaultObject or not e.isa(o, CF["Class"]):
            continue
        n = e.name(o)
        if not n or n.startswith(("SKEL_", "REINST_", "Default__", "PLACEHOLDER")) or (vague and not re.search(r"(?i)item", n)):
            continue
        c, d = e.super(o), 0
        while c and c != meta and d < 40:
            c, d = e.super(c), d + 1
        if c != meta:
            continue
        par = e.super(o)
        cdo = e.ptr(o + e.O["UClass_ClassDefaultObject"])
        lb, lw, ls = U.read_limits(e, cdo, U.limit_fields(e, o)) if cdo else (None, None, None)   # limits of the item's default object
        out.append(dict(id=n, idx=e.i32(o + e.O["UObject_Name"]), num=0, cat=(e.name(par) if par and par != meta else e.name(meta)) or "", price=None,
                        lim_bag=lb, lim_ware=lw, lim_stack=ls, sort=len(out), table="", numid=0, class_addr=o))
    out.sort(key=lambda x: (x["cat"], x["id"]))
    return out


def object_catalog(e, cls):
    """Items whose identity is an object (data asset item definitions): every non-default instance of the parameter's class."""
    import ueitems as U
    out, lims = [], {}
    for i, o in e.arr:
        c = e.cls(o)
        if (e.flags(o) or 0) & RF_ClassDefaultObject or not c:
            continue
        k, d = c, 0
        while k and k != cls and d < 40:
            k, d = e.super(k), d + 1
        n = e.name(o)
        if k != cls or not n or n.startswith(("Default__", "PLACEHOLDER")):
            continue
        if c not in lims:
            lims[c] = U.limit_fields(e, c)
        lb, lw, ls = U.read_limits(e, o, lims[c])
        out.append(dict(id=n, idx=e.i32(o + e.O["UObject_Name"]), num=e.i32(o + e.O["UObject_Name"] + 4) or 0,
                        cat=e.name(c) or "", price=None, lim_bag=lb, lim_ware=lw, lim_stack=ls, sort=len(out), table="", numid=0, class_addr=o))
    out.sort(key=lambda x: (x["cat"], x["id"]))
    return out


TAG_ROOT = re.compile(r"(?i)^(items?|inventory|loot|pick_?ups?|consumables?|materials?|resources?|ingredients?)$")
TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


def tag_catalog(e):
    """Items whose identity is an FGameplayTag: the leaf tags under item-like roots (Items., PickUp., Inventory., ...), read from the
    FName pool (registered tags are plain FNames). UE 4.23+ name pool only."""
    P, d = e.pool, e.d
    if not hasattr(P, "block_bits"):
        return []
    names, bs = [], 1 << P.block_bits
    for b in range(P.current_block + 1):
        base = d.u64(P.addr + 0x10 + 8 * b)
        blob = d.read(base, P.cursor if b == P.current_block else bs * P.stride) if base else b""
        off = 0
        while off + 2 <= len(blob):
            hdr = int.from_bytes(blob[off:off + 2], "little")
            ln, wide = hdr >> 6, hdr & 1
            if not ln:
                break
            sz = 2 + ln * (2 if wide else 1)
            if not wide:
                s = blob[off + 2:off + sz].decode("latin1")
                if TAG_RE.match(s) and TAG_ROOT.match(s.split(".")[0]):
                    names.append(((b << P.block_bits) | (off // P.stride), s))
            off += (sz + P.stride - 1) // P.stride * P.stride
    parents = {s.rsplit(".", 1)[0] for _, s in names}
    out = [dict(id=s, idx=i, num=0, cat=s.rsplit(".", 1)[0], price=None, lim_bag=None, lim_ware=None, sort=0, table="", numid=0)
           for i, s in names if s not in parents]                  # leaves only: "Items.Consumable" is a category
    out.sort(key=lambda x: x["id"])
    return out


# ============================================================ live: find objects from GWorld, call ProcessEvent ==================
def _ok_ptr(v):
    return 0x10000 <= v < 0x800000000000 and not v & 7


class LiveGraph:
    """Breadth-first walk over the live object pointers starting at GWorld, using the dump's class layouts for the offsets.
    Finds an instance of a class (by name, subclasses included) without needing any address from the dump."""

    def __init__(self, e, dbg, pid, gworld, say=print, max_nodes=6000, max_depth=6):
        self.e, self.dbg, self.pid, self.gworld, self.say = e, dbg, pid, gworld, say
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self.kc, self.sc, self.found, self.seen, self.paths = {}, {}, {}, set(), {}
        O = e.O
        self.o_name, self.o_cls, self.o_sup, self.o_size = O["UObject_Name"], O["UObject_Class"], O["UStruct_SuperStruct"], O["UStruct_Size"]

    async def rd(self, a, n):
        try:
            raw = await self.dbg.read_memory(self.pid, a, n)
        except Exception:
            return None
        return bytes(raw) if raw and len(raw) == n else None

    def pname(self, idx):
        try:
            return self.e.pool.name(idx, 0) or ""
        except Exception:
            return ""

    async def kinfo(self, c):
        """live UClass -> dict(names=[lowercase class names up the Super chain], chain=[live ptrs], size)"""
        if c in self.kc:
            return self.kc[c]
        names, chain, x, size = [], [], c, 0
        n = max(self.o_name, self.o_sup, self.o_size) + 8
        for _ in range(40):
            if not _ok_ptr(x):
                break
            raw = await self.rd(x, n)
            if not raw:
                break
            nm = self.pname(struct.unpack_from("<i", raw, self.o_name)[0])
            if not nm:
                break
            if x == c:
                size = struct.unpack_from("<i", raw, self.o_size)[0]
            names.append(nm.lower())
            chain.append(x)
            x = struct.unpack_from("<Q", raw, self.o_sup)[0]
        info = dict(names=names, chain=chain, size=size) if names and 0 < size < 0x100000 else None
        self.kc[c] = info
        return info

    def slots(self, cname):
        """[(offset, is_array)] of the object-pointer properties of a class and its supers (dump layouts)."""
        if cname in self.sc:
            return self.sc[cname]
        e, out = self.e, []
        c = e.find(cname, CF["Class"])
        seen = set()
        while c and c not in seen:
            seen.add(c)
            for p in e.properties(c):
                n = e.ffield_name(p) or ""
                if SKIP_PROP.match(n):
                    continue
                k = e.ffield_class_name(p)
                if k == "ObjectProperty":
                    out.append((e.prop_offset(p), False))
                elif k == "ArrayProperty":
                    inner = e.ptr(p + e.O["ArrayProperty_Inner"])
                    if inner and e.ffield_class_name(inner) == "ObjectProperty":
                        out.append((e.prop_offset(p), True))
            c = e.super(c)
        self.sc[cname] = out
        return out

    async def find(self, wanted):
        """{lowercase class name: live address} for the first node reached whose class derives from each wanted name"""
        wanted = {w.lower() for w in wanted}
        todo = {w for w in wanted if w not in self.found}
        if not todo:
            return {w: self.found[w] for w in wanted}
        q = deque([(self.gworld, 0, ())])
        seen = {self.gworld}
        nodes = 0
        while q and todo and nodes < self.max_nodes:
            a, depth, path = q.popleft()
            head = await self.rd(a, 0x20)
            if not head:
                continue
            c = struct.unpack_from("<Q", head, self.o_cls)[0]
            info = await self.kinfo(c) if _ok_ptr(c) else None
            if not info:
                continue
            nodes += 1
            for w in list(todo):
                if w in info["names"]:
                    self.found[w] = a
                    self.paths[a] = path
                    todo.discard(w)
            if not todo:
                break
            if depth >= self.max_depth or SKIP_CLS.search(info["names"][0]):
                continue
            sl = self.slots(info["names"][0]) or next((s for nm in info["names"][1:] if (s := self.slots(nm))), [])
            if not sl:
                continue
            body = await self.rd(a, min(info["size"], 0x4000))
            if not body:
                continue
            for off, arr in sl:
                if off + 8 > len(body):
                    continue
                v = struct.unpack_from("<Q", body, off)[0]
                if not _ok_ptr(v):
                    continue
                if arr:
                    num = struct.unpack_from("<i", body, off + 8)[0] if off + 12 <= len(body) else 0
                    if 0 < num <= 100000:
                        data = await self.rd(v, min(num, 16) * 8)
                        for j in range(len(data) // 8 if data else 0):
                            p = struct.unpack_from("<Q", data, j * 8)[0]
                            if _ok_ptr(p) and p not in seen:
                                seen.add(p)
                                q.append((p, depth + 1, path + (off, 8 * j)))
                elif v not in seen:
                    seen.add(v)
                    q.append((v, depth + 1, path + (off,)))
        return {w: self.found[w] for w in wanted if w in self.found}

    async def live_function(self, obj_class, name_idx):
        """UFunction of the live class chain whose FName index is name_idx"""
        info = await self.kinfo(obj_class)
        if not info:
            return None
        o_next = self.e.O["UField_Next"]
        o_ch = self.e.O["UStruct_Children"]
        for c in info["chain"]:
            raw = await self.rd(c + o_ch, 8)
            f = struct.unpack("<Q", raw)[0] if raw else 0
            for _ in range(4000):
                if not _ok_ptr(f):
                    break
                h = await self.rd(f, max(self.o_name, o_next) + 8)
                if not h:
                    break
                if struct.unpack_from("<i", h, self.o_name)[0] == name_idx:
                    return f
                f = struct.unpack_from("<Q", h, o_next)[0]
        return None

    async def valid_object(self, a, dump_obj):
        """is the object at live address `a` the same one as the dump's `dump_obj` (same FName and class pointer)?"""
        raw = await self.rd(a, 0x20)
        if not raw:
            return False
        e = self.e
        return (struct.unpack_from("<i", raw, self.o_name)[0] == e.i32(dump_obj + self.o_name)
                and struct.unpack_from("<Q", raw, self.o_cls)[0] == (e.cls(dump_obj) or 0))


def _pack(d, v):
    k = d["ptype"]
    if k in ("FloatProperty",):
        return struct.pack("<f", float(v))
    if k in ("DoubleProperty",):
        return struct.pack("<d", float(v))
    if d["size"] >= 8:
        return struct.pack("<q", int(v))
    return struct.pack("<i", int(v)) if d["size"] >= 4 else struct.pack("<h", int(v))


async def call_live(e, dbg, pid, graph, fn, item, count, pe_index, stub, say=print, rpc=False):
    """Call one give function (an entry of resolve()) for `item` (catalog entry, or None for money) x count.
    -> (rax, refused): refused = the function returns a bool and it came back false (the game did not take the item).
    Runs on the game thread (game_thread_call); rpc=True calls from the debugger's thread as before (rax is only known then)."""
    owner = e.find(fn["owner"], CF["Class"])
    if not owner:
        raise PEError(f"class {fn['owner']} is not in this dump")
    ufn = e.find_in_outer(fn["name"], fn["owner"])
    if not ufn:
        raise PEError(f"function {fn['owner']}::{fn['name']} is not in this dump")
    name_idx = e.i32(ufn + e.O["UObject_Name"])
    fcls = None                                                 # class whose chain holds the UFunction (None: the object's own)
    if fn["static"] or fn.get("this_unused"):
        cdo = e.ptr(owner + e.O["UClass_ClassDefaultObject"])
        if cdo and await graph.valid_object(cdo, cdo):
            obj = cdo
        elif await graph.valid_object(owner, owner):
            obj, fcls = graph.gworld, owner                     # `this` is never read: any live UObject will do
            say(f"   {fn['owner']} default object not at the dump's address: calling on GWorld")
        else:
            raise PEError(f"class {fn['owner']} is not at the dump's address in this session: take a new dump, scan again")
    else:
        got = await graph.find({fn["owner"]})
        obj = got.get(fn["owner"].lower())
        if not obj:
            raise PEError(f"no live {fn['owner']} reachable from GWorld (still in a menu / loading? start the game and load the save first)")
    if not fn["static"] and not fn.get("this_unused") and obj in graph.paths:   # the chain that really reached the live object (cheat export)
        fn["chain_live"] = dict(hops=["+%#x" % o for o in graph.paths[obj]], text="World" + "".join("." + "+%#x" % o for o in graph.paths[obj]))
    head = await graph.rd(obj, 0x20)
    if not head:
        raise PEError(f"cannot read the object at {obj:#x}")
    ocls = fcls or struct.unpack_from("<Q", head, graph.o_cls)[0]
    func = await graph.live_function(ocls, name_idx)
    if not func:
        raise PEError(f"{fn['name']} not found in the live class of the object ({fn['owner']} changed in this build?)")
    size = max([p["off"] + p["size"] for p in fn["params"]] + [8])
    buf = bytearray((size + 0x2F) & ~0xF)
    for p in fn["params"]:
        r, o = p["role"], p["off"]
        if r == "id":
            if p["type"] in ("UClass", "UObject"):
                ca = (item or {}).get("class_addr")
                if not ca or not await graph.valid_object(ca, ca):
                    raise PEError(f"the item class {item and item['id']} is not at the dump's address in this session: "
                                  "take a new dump (Make Dump+SDK) with the game running, scan again")
                buf[o:o + 8] = struct.pack("<Q", ca)
            elif p["type"] == "FName":
                buf[o:o + 8] = struct.pack("<Q", (item["idx"] | (item["num"] << 32)) & (2**64 - 1))
            else:
                buf[o:o + p["size"]] = _pack(p, item["numid"])
        elif r == "count":
            buf[o:o + p["size"]] = _pack(p, count)
        elif r == "full":
            buf[o:o + 4] = struct.pack("<f", 1.0)
        elif r == "world":
            buf[o:o + 8] = struct.pack("<Q", graph.gworld)
        elif r == "pc":
            got = await graph.find({"playercontroller"})
            pc = got.get("playercontroller") or 0
            if not pc:                                  # null: the native code crashes the game
                raise PEError("no live PlayerController reachable from GWorld (load the save first)")
            buf[o:o + 8] = struct.pack("<Q", pc)
        elif r == "pawn":
            pawn = await _live_pawn(e, graph)
            if not pawn:
                raise PEError("no live player pawn reachable from GWorld (load the save first)")
            buf[o:o + 8] = struct.pack("<Q", pawn)
    vt = struct.unpack("<Q", (await graph.rd(obj, 8)) or bytes(8))[0]
    pe = struct.unpack("<Q", (await graph.rd(vt + 8 * pe_index, 8)) or bytes(8))[0]
    if not _ok_ptr(pe):
        raise PEError("ProcessEvent address not readable from the object's vtable (process_event_index wrong?)")
    rb = next((p for p in fn["params"] if (p.get("ret") or p["name"] == "ReturnValue") and p["ptype"] == "BoolProperty"), None)
    refused = False
    if not rpc:
        out = await game_thread_call(dbg, pid, pe, obj, func, buf, say=say)
        return None, bool(rb and out and out[rb["off"]] == 0)
    mem = await dbg.allocate_memory(pid, 4096)
    try:
        await dbg.write_memory(pid, mem, bytes(buf))
        say(f"   obj {obj:#x} func {func:#x} ProcessEvent {pe:#x} params @ {mem:#x}")
        rax = await rpc_call(dbg, pid, pe, [obj, func, mem], stub)
        if rb:
            back = await graph.rd(mem + rb["off"], 1)
            refused = back is not None and back[0] == 0
    finally:
        try:
            await dbg.free_memory(pid, mem, 4096)
        except Exception:
            pass
    return rax, refused


async def _live_pawn(e, graph):
    got = await graph.find({"playercontroller"})
    pc = got.get("playercontroller")
    if not pc:
        return 0
    ctrl = e.find("Controller", CF["Class"])
    m = e.member(ctrl, "Pawn") if ctrl else None
    if not m:
        return 0
    raw = await graph.rd(pc + e.prop_offset(m), 8)
    return struct.unpack("<Q", raw)[0] if raw else 0
