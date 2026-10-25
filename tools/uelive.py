"""Live read/write of a property on all instances of a class, using the dump for lookup and ps4debug for access.

    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C bGodMode        # read
    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C bGodMode 1      # write
    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C                 # list properties
    python uelive.py <dump.bin> <ip> player MovementAttributeSet.SprintSpeed.CurrentValue 650
      class "player" = the pawn the local PlayerController owns in the dump (same start as uecheat paths)
      property paths as in uecheat (object/struct hops, Name[i] array element, Prop:Class downcast); pointers are
      followed live, so the dump must come from the running game session (object addresses change on restart)
    ... player World.<path> [value]      # uecheat World-rooted path, starts at the live GWorld (works from any session)
    ... player <path> [value] --live    # other session: the pawn is found on the console (GWorld chain), dump only
                                         # supplies the class layouts (needs the same game binary/base)
"""
import asyncio, json, math, os, struct, sys
import ueobj
from ueobj import CF, RF_ClassDefaultObject

FMT = dict(BoolProperty="<B", ByteProperty="<B", Int8Property="<b", Int16Property="<h", UInt16Property="<H",
           IntProperty="<i", UInt32Property="<I", Int64Property="<q", UInt64Property="<Q", FloatProperty="<f",
           DoubleProperty="<d", EnumProperty="<B", ObjectProperty="<Q", ClassProperty="<Q", NameProperty="<i")


LIVE = False      # --live: find the player pawn on the console itself (GWorld chain) instead of using the dump's addresses


async def live_player(e, dbg, pid, dump, cls):
    """Player pawn of the RUNNING game, found like the cheat's hook does: GWorld -> OwningGameInstance -> LocalPlayers[0]
    -> PlayerController -> Pawn. Needs only class layouts from the dump (stable per binary) + GWorld from offsets.json, so
    the dump may come from another session. Assumes the eboot is loaded at the same base as when dumped (0x400000 with ASLR off)."""
    import uecheat
    meta = json.load(open(os.path.join(os.path.dirname(dump), "SDK", "offsets.json")))
    if not meta.get("gworld"):
        sys.exit("offsets.json lacks gworld (rerun the SDK dump with the console reachable)")
    if not await dbg.read_memory(pid, e.d.base, 4):          # base unreadable = different layout (the eboot is r-x, no ELF magic to test)
        sys.exit(f"module base {e.d.base:#x} of the dump is not readable in this session - cannot resolve live, redump")
    hop = lambda c, p: e.prop_offset(uecheat.member(e, uecheat.cls_of(e, c), p))
    o_gi, o_lp, o_pc, o_pawn = hop("World", "OwningGameInstance"), hop("GameInstance", "LocalPlayers"), \
        hop("Player", "PlayerController"), hop("Controller", "Pawn")
    u64 = lambda raw: struct.unpack("<Q", raw)[0] if raw and len(raw) == 8 else 0

    async def rp(a, what):
        v = u64(await dbg.read_memory(pid, a, 8))
        if not v:
            sys.exit(f"live resolve: {what} is null (in a menu / loading? start the game first)")
        return v
    world = await rp(e.d.base + meta["gworld"], "GWorld")
    gi = await rp(world + o_gi, "OwningGameInstance")
    n = await dbg.read_memory(pid, gi + o_lp + 8, 4)
    if not n or struct.unpack("<i", n)[0] <= 0:
        sys.exit("live resolve: LocalPlayers is empty")
    lp0 = await rp(await rp(gi + o_lp, "LocalPlayers.Data"), "LocalPlayers[0]")
    pawn = await rp(await rp(lp0 + o_pc, "PlayerController") + o_pawn, "Pawn")
    # class addresses differ per session, so compare the class size: menus swap in small menu pawns (Mortal Shell) and
    # the dump's offsets would then read/write behind the object
    size = struct.unpack("<i", await dbg.read_memory(pid, await rp(pawn + e.O["UObject_Class"], "Pawn.Class") + e.O["UStruct_Size"], 4))[0]
    if size != e.struct_size(cls):
        sys.exit(f"live pawn is another class than {e.name(cls)} (size {size:#x}, dump {e.struct_size(cls):#x}): "
                 "in a menu or another character? play as the dumped character, or redump")
    return pawn


async def live_world(e, dbg, pid, dump):
    """The running game's UWorld: GWorld is a global in the eboot (offsets.json), so this works from any session's dump."""
    meta = json.load(open(os.path.join(os.path.dirname(dump), "SDK", "offsets.json")))
    meta.get("gworld") or sys.exit("offsets.json lacks gworld (rerun the SDK dump with the console reachable)")
    raw = await dbg.read_memory(pid, e.d.base + meta["gworld"], 8)
    return (struct.unpack("<Q", raw)[0] if raw else 0) or sys.exit("GWorld is null (loading?)")


def resolve(e, cls, prop):
    """Walk the class chain for a property; returns (field, owning class)."""
    c = cls
    while c:
        m = e.member(c, prop)
        if m:
            return m, c
        c = e.super(c)
    return None, None


def resolve_path(e, cls, path):
    """uecheat path syntax -> (hops, field, owner); hops are ("*" pointer | "+" inline struct | "[]" object array element
    | "[]s" struct array element, offset, index | (index, element size))."""
    hops, segs = [], path.split(".")
    for tok in segs[:-1]:
        name, _, cast = tok.partition(":")
        name, _, idx = name.rstrip("]").partition("[")
        f, _ = resolve(e, cls, name)
        if not f:
            sys.exit(f"property {name} not found in {e.name(cls)} or its supers")
        kind, off = e.ffield_class_name(f), e.prop_offset(f)
        if idx:
            inner = e.ptr(f + e.O["ArrayProperty_Inner"])
            ik = e.ffield_class_name(inner) if kind == "ArrayProperty" else None
            if ik == "StructProperty":                           # elements inline: Data + i * ElementSize
                hops.append(("[]s", off, (int(idx, 0), e.prop_size(inner)))); cls = e.ptr(inner + e.O["StructProperty_Struct"])
            elif ik == "ObjectProperty":
                hops.append(("[]", off, int(idx, 0))); cls = e.ptr(inner + e.O["ObjectProperty_Class"])
            else:
                sys.exit(f"{name} is {kind}, [i] needs a TArray of object pointers or structs")
        elif kind == "StructProperty":
            hops.append(("+", off, 0)); cls = e.ptr(f + e.O["StructProperty_Struct"])
        elif kind == "ObjectProperty":
            hops.append(("*", off, 0)); cls = e.ptr(f + e.O["ObjectProperty_Class"])
        else:
            sys.exit(f"{name} is {kind}, expected an object pointer, struct or object array")
        if cast:
            cls = e.find(cast, CF["Class"]) or sys.exit(f"class {cast} not found")
    f, owner = resolve(e, cls, segs[-1])
    if not f:
        sys.exit(f"property {segs[-1]} not found in {e.name(cls)} or its supers")
    return hops, f, owner


async def follow(dbg, pid, a, hops):
    """Apply the hops to the live object at `a`; None if a pointer is null or the array too short."""
    u64 = lambda raw: struct.unpack("<Q", raw)[0] if raw else 0
    for op, off, i in hops:
        if op == "+":
            a += off
            continue
        if op in ("[]", "[]s"):
            i, es = i if op == "[]s" else (i, 8)                  # struct elements inline, object elements are pointers
            n = await dbg.read_memory(pid, a + off + 8, 4)            # TArray {Data, Num, Max}
            if not n or struct.unpack("<i", n)[0] <= i:
                return None
            a = u64(await dbg.read_memory(pid, a + off, 8))
            if not a:
                return None
            if op == "[]s":
                a += i * es
                continue
            off = 8 * i
        a = u64(await dbg.read_memory(pid, a + off, 8))
        if not a:
            return None
    return a


async def main(dump, host, cname, prop=None, value=None):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    player = None
    world = cname.lower() == "player" and (prop or "").startswith("World.")   # uecheat World-rooted path
    if world:
        cname, prop = "World", prop[6:]
    elif cname.lower() == "player":                              # the local player's pawn, whatever its class is called
        import uecheat
        player = uecheat.player_pawn(e, e.prop_offset(uecheat.member(e, uecheat.cls_of(e, "Controller"), "Pawn")))
        if not player:
            sys.exit("no live player pawn in this dump (dump in-game, not in a menu)")
        cname = e.name(e.cls(player))
    cls = e.cls(player) if player else e.find(cname, CF["Class"])
    if not cls:
        sys.exit(f"class {cname} not found (use 'player' for the player pawn)")
    if not prop:
        c = cls
        while c:
            for p in e.properties(c):
                print(f"  +{e.prop_offset(p):#06x} {e.ffield_class_name(p):26} {e.ffield_name(p)}   ({e.name(c)})")
            c = e.super(c)
        return
    hops, f, owner = resolve_path(e, cls, prop)
    kind, off = e.ffield_class_name(f), e.prop_offset(f)
    mask = 0xFF
    if kind == "BoolProperty":
        _, boff, bmask, fmask = e.bool_info(f)
        if fmask != 0xFF:                                        # bitfield bool: byte offset + mask within that byte
            off, mask = off + boff, bmask
    fmt = FMT.get(kind)
    if not fmt:
        sys.exit(f"{kind} not supported for live editing")
    print(f"{cname}.{prop}: {kind} at +{off:#x} (declared in {e.name(owner)})" + (f" bitmask {mask:#04x}" if mask != 0xFF else ""))

    def is_a(c):
        while c:
            if c == cls:
                return True
            c = e.super(c)
        return False
    if player or world:
        inst = [player]
    else:
        subs = {c for c in {e.cls(o) for i, o in e.arr} if is_a(c)}
        inst = [o for i, o in e.arr if e.cls(o) in subs and not (e.flags(o) or 0) & RF_ClassDefaultObject]
        print(f"{len(inst)} instance(s) in dump ({len(subs)} classes incl. subclasses)")
    import ps4debug
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    live_o = await live_world(e, dbg, pid, dump) if world else await live_player(e, dbg, pid, dump, cls) if LIVE and player else None
    for o in inst:
        if live_o:
            print(f"  live {cname} {live_o:#x}" + (f" (dump had {o:#x})" if o else ""))
        else:
            live_cls = await dbg.read_memory(pid, o + e.O["UObject_Class"], 8)
            if not live_cls or struct.unpack("<Q", live_cls)[0] != e.cls(o):
                print(f"  {o:#x}: object gone (class changed) - dump from another session? redump (or use --live)")
                continue
        o = live_o or o
        base = await follow(dbg, pid, o, hops)
        if not base:
            print(f"  {o:#x}: path not set on this instance (null pointer / short array)")
            continue
        addr = base + off
        raw = await dbg.read_memory(pid, addr, struct.calcsize(fmt))
        cur = struct.unpack(fmt, raw)[0]
        shown = bool(cur & mask) if mask != 0xFF else cur
        print(f"  {o:#x} {e.fullname(o).split(' ', 1)[1] if not live_o else cname}: {shown}" + (f"   (at {addr:#x})" if hops else ""))
        if value is not None:
            if mask != 0xFF:
                new = (cur | mask) if value not in ("0", "false") else (cur & ~mask & 0xFF)
            else:
                new = float(value) if fmt[-1] in "fd" else int(value, 0)
            await dbg.write_memory(pid, addr, struct.pack(fmt, new))
            back = struct.unpack(fmt, await dbg.read_memory(pid, addr, struct.calcsize(fmt)))[0]
            print(f"    -> written, readback {bool(back & mask) if mask != 0xFF else back}")


async def read_many(dump, host, paths):
    """Read the CURRENT live value of several property paths on the player pawn, opening the dump/console only once.
    Output, one line per path: "path<TAB>value" (bools as 0/1, floats as %g) or "path<TAB>?<TAB>reason"."""
    e = ueobj.open_engine(dump, log=lambda *a: None)
    import uecheat
    player = uecheat.player_pawn(e, e.prop_offset(uecheat.member(e, uecheat.cls_of(e, "Controller"), "Pawn")))
    if not player:
        sys.exit("no live player pawn in this dump (dump in-game, not in a menu)")
    cls = e.cls(player)
    import ps4debug
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    if LIVE:
        player = await live_player(e, dbg, pid, dump, cls)
    else:
        live_cls = await dbg.read_memory(pid, player + e.O["UObject_Class"], 8)
        if not live_cls or struct.unpack("<Q", live_cls)[0] != cls:
            sys.exit("player pawn gone (class changed) - dump from another session? redump (or use --live)")
    world = None
    for p in paths:
        try:
            if p.startswith("World."):                           # uecheat World-rooted path: start at the live GWorld
                world = world or await live_world(e, dbg, pid, dump)
                hops, f, owner = resolve_path(e, e.find("World", CF["Class"]), p[6:])
            else:
                hops, f, owner = resolve_path(e, cls, p)
            kind, off, mask = e.ffield_class_name(f), e.prop_offset(f), 0xFF
            if kind == "BoolProperty":
                _, boff, bmask, fmask = e.bool_info(f)
                if fmask != 0xFF:
                    off, mask = off + boff, bmask
            fmt = FMT.get(kind)
            if not fmt:
                print(f"{p}\t?\t{kind} not supported")
                continue
            base = await follow(dbg, pid, world if p.startswith("World.") else player, hops)
            if not base:
                print(f"{p}\t?\tpath not set (null pointer / short array)")
                continue
            raw = await dbg.read_memory(pid, base + off, struct.calcsize(fmt))
            cur = struct.unpack(fmt, raw)[0]
            if mask != 0xFF:
                cur = int(bool(cur & mask))
            print(f"{p}\t{cur:g}" if fmt[-1] in "fd" else f"{p}\t{cur}")
        except SystemExit as ex:
            print(f"{p}\t?\t{ex}")
        except Exception as ex:
            print(f"{p}\t?\t{ex}")


if __name__ == "__main__":
    LIVE = "--live" in sys.argv                              # anywhere on the command line
    a = [x for x in sys.argv[1:] if x != "--live"]
    if len(a) > 4 and a[3] == "--read-many":                 # uelive.py dump ip player --read-many path1 path2 ...
        ueobj.run(read_many(a[0], a[1], a[4:]))
    else:
        ueobj.run(main(a[0], a[1], a[2], a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None))
