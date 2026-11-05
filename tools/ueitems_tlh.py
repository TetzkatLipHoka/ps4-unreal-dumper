"""ueitems_tlh.py - item/money cheats for the cheat-menu PRX, built from a dump + its item scan (ueitems.py scan / GUI "Scan items").
Two ways to use it:
  * inside uecheat.py / the GUI: a rule "Name = item:ITEM_ID = amount" (and "item:$money") becomes a cheat in the same .TLH as the
    player rules, one hook for everything (uecheat.py calls world_block() below);
  * on its own: python ueitems_tlh.py <dump.bin> <out.TLH> [--cand N] [--cat Material,Food] [--match ITEM_MTL] [--skip ITEM_WEP]
                          [--amount 99] [--no-money] [--chain "World.A:Cls.+0xf0.+0x260@0xbc8"]
How the inventory is found at run time: if the scan found it at a fixed (executable image) address the trainer uses ModuleBase+off,
otherwise the chain of pointers from GWorld (ueitems_chain.py, searched in THIS dump and cached in <dump>.items.json) is written as
plain hops: named properties where the class is known, "+0xNN" raw pointer hops otherwise. Item list, name indexes and the slot
layout come from the dump, nothing about the game has to be known beforehand.
Cheat "Add ITEM_X": switch on -> the item goes into the first free unlocked backpack slot with the amount of its value cell (or the
amount of an existing stack is set to it); the flag then goes to 2, so it is added once per switch-on, not frozen. "Money": the
money is set to the value every frame while on."""
import json, os, sys
import ueitems
from ueitems import PROFILE


def resolve_chain(e, dump, cache, cand=0, forced=None):
    """-> dict(static=bool, off=int) for an inventory in the executable image, or dict(static=False, hops=[...], leaf=int, text=str)."""
    import ueitems_chain
    if forced:
        path, _, leaf = forced.rpartition("@")
        toks = path.split(".")
        if toks[:1] != ["World"] or not leaf:
            sys.exit('--chain must look like "World.<hops>@0xLEAF"')
        return dict(static=False, hops=toks[1:], leaf=int(leaf, 0), text=forced)
    cands = [c for c in cache.get("candidates", []) if not c.get("num_odd")]
    cands or sys.exit("the scan found no inventory in this dump (see the Items window message); scan again with items in the backpack")
    c = cands[cand]
    if c["static"]:
        return dict(static=True, off=c["off"])
    addr = ueitems.cand_addr(cache, c)
    ch = cache.get("chain")
    if ch and ch.get("addr") == addr and ch.get("hops"):
        return dict(static=False, hops=ch["hops"], leaf=ch["leaf"], text=ch["text"])
    print(f"inventory at {addr:#x} (heap): searching the pointer chain from GWorld ...")
    roots, _ = ueitems_chain.world_root(e, dump)
    res = [r for r in ueitems_chain.find_chain(e, addr, roots, 5, say=print) if r[1] >= 0]
    res or sys.exit("no pointer chain from GWorld reaches the inventory (dump taken at another moment? use --chain)")
    toks, leaf, hops_n, _ = res[0]
    text = ".".join(toks) + f"@{leaf:#x}"
    print(f"chain: {text}   ({hops_n} hops)")
    cache["chain"] = dict(addr=addr, hops=toks[1:], leaf=leaf, text=text)
    try:
        json.dump(cache, open(ueitems.cache_path(dump), "w", encoding="utf-8"))
    except OSError:
        pass
    return dict(static=False, hops=toks[1:], leaf=leaf, text=text)


def add_item_code(P):
    """Subroutine: put (r10d = name index, r11d = name number) with amount edi in the slot array at rcx: an existing stack gets
    the amount, else the first free unlocked slot is filled."""
    SLOT = ueitems.SLOT
    return ["add_item:", "xor r8d, r8d", "mov rdx, rcx", f"mov r9d, {P['bag_n']}",
            "ai_loop:", "mov esi, dword ptr [rdx+4]", "cmp esi, r10d", "jne ai_other",
            "cmp dword ptr [rdx+8], r11d", "jne ai_other", "mov dword ptr [rdx], edi", "ret",
            "ai_other:", "test esi, esi", "jnz ai_next", "test r8, r8", "jnz ai_next",
            "cmp byte ptr [rdx+0xc], 1", "jne ai_next", "mov r8, rdx",
            "ai_next:", f"add rdx, {SLOT}", "dec r9d", "jnz ai_loop",
            "test r8, r8", "jz ai_ret",
            "mov dword ptr [r8], edi", "mov dword ptr [r8+4], r10d", "mov dword ptr [r8+8], r11d",
            "ai_ret:", "ret"]


def world_block(e, ch, entries, money_k, P=PROFILE):
    """Self-contained asm block (own register saves) for uecheat's world_blocks. entries = [(k, name index, name number)] with the
    cells Flag{k}/Val{k}; money_k = k of the Money cell or None. Returns a list of lines."""
    import uecheat
    saved = ["rsi", "rdi", "r8", "r9", "r10", "r11"]
    code = [f"push {r}" for r in saved]
    if ch["static"]:
        code += ["lea rcx, [Inv]"]
    else:
        code += ["mov rcx, [GWorld]", "test rcx, rcx", "jz it_pop"]
        uecheat.path_code(e, uecheat.cls_of(e, "World"), ch["hops"], "rcx", "it_pop", code)
        code += ["test rcx, 7", "jnz it_pop"]
        code += [f"add rcx, {ch['leaf'] + P['bag']:#x}"]
    code += [f"cmp byte ptr [rcx+0xc], 1", "ja it_pop"]                # slot 0 must look like a slot (bEnable is 0/1)
    if money_k:
        code += [f"cmp byte ptr [Flag{money_k}], 1", "jne it_money_end", f"mov edx, dword ptr [Val{money_k}]",
                 f"mov dword ptr [rcx{P['money'] - P['bag']:+#x}], edx", "it_money_end:"]
    for k, idx, nb in entries:
        code += [f"cmp byte ptr [Flag{k}], 1", f"jne it{k}_end", f"mov byte ptr [Flag{k}], 2", f"mov r10d, {idx:#x}",
                 f"mov r11d, {nb:#x}", f"mov edi, dword ptr [Val{k}]", "call add_item", f"it{k}_end:"]
    code += ["jmp it_pop"] + add_item_code(P) + ["it_pop:"] + [f"pop {r}" for r in reversed(saved)]
    return code


def static_symbol(cache, ch):
    return f"RegisterSymbol( Inv, ModuleBase+{ch['off'] + PROFILE['bag']:#x} )" if ch["static"] else None


GI32 = ["edi", "esi", "edx", "ecx", "r8d", "r9d"]
GI64 = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]


def item_limit(it):
    """The game's own amount limit of a catalog item (bag first) or None: the cheats cut the value cell to it, so nothing is thrown away."""
    lim = (it or {}).get("lim_bag") or (it or {}).get("lim_ware")
    return lim if lim and lim > 0 else None


def _arg_lines(fn, k, numid, idx, num, count_cell, lim=None):
    """Load the integer-class arguments of the game's give function in declaration order (System V: rdi, rsi, rdx, rcx, r8, r9)."""
    out = []
    for n, p in enumerate(fn["params"][:6]):
        r32, r64 = GI32[n], GI64[n]
        if p["role"] == "id":
            if p["type"] == "FName":
                out += [f"mov {r32}, {idx}"] + ([f"movabs rax, {num << 32:#x}", f"or {r64}, rax"] if num else [])   # idx: r11d after name_routine, or 0
            else:
                out.append(f"mov {r32}, {numid}")
        elif p["role"] == "count":
            out.append(f"mov {r32}, [{count_cell}]" if p["size"] <= 4 else f"movsxd {r64}, dword ptr [{count_cell}]")
            if lim:
                out += [f"mov eax, {lim}", f"cmp {r32}, eax", f"cmovg {r32}, eax"] if p["size"] <= 4 else \
                       [f"mov eax, {lim}", f"cmp {r64}, rax", f"cmovg {r64}, rax"]
        else:
            out.append(f"xor {r32}, {r32}")
    return out


def gnames_symbol(code, meta):
    return [f"RegisterSymbol( GNames, ModuleBase+{meta['gnames']:#x} )"] if any("[GNames]" in l for l in code) else []


def give_block(cache, specs, e, meta):
    """Inventory rules for a game WITHOUT a built-in inventory layout: call the game's own add-item (and add-money) function.
    specs = [(k, id, value)] -> (asm lines, RegisterSymbol lines). One call per switch-on: the flag goes 1 -> 2."""
    g = cache.get("give") or sys.exit("this dump's item scan found no give-item function (scan again with the SDK generated next to the dump)")
    item, money = g["item"], g.get("money")
    cat = {i["id"].lower(): i for i in cache["catalog"]}
    saved = ["rsi", "rdi", "r8", "r9", "r10", "r11"]
    code = [f"push {r}" for r in saved] + ["sub rsp, 8"]                 # 4 prologue pushes + 6 here + 8: rsp stays 16-aligned at the call
    syms = [f"RegisterSymbol( GiveItem, ModuleBase+{item['impl_rva']:#x} )"]
    for k, iid, _ in specs:
        if iid.lower() == "$money":
            money or sys.exit("this game has no money function in its SDK: use the item rules only")
            body = _arg_lines(money, k, 0, 0, 0, f"Val{k}") + ["movabs rax, GiveMoney", "call rax"]
        else:
            it = cat.get(iid.lower()) or sys.exit(f"item rule: {iid} is not in the item catalog of this dump (scan again?)")
            scan = []
            if any(p["role"] == "id" and p["type"] == "FName" for p in item["params"]):
                hdr, h = name_ref(e, it["idx"])
                scan = [f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", "call gvname", "cmp r11d, -1", f"je gv{k}_end"]
            body = scan + _arg_lines(item, k, it["numid"], "r11d", it["num"], f"Val{k}", item_limit(it)) + ["movabs rax, GiveItem", "call rax"]
        code += [f"cmp byte ptr [Flag{k}], 1", f"jne gv{k}_end", f"mov byte ptr [Flag{k}], 2"] + body + [f"gv{k}_end:"]
    if any("call gvname" in l for l in code):
        code += ["jmp gvskip"] + name_routine("gvname", e) + ["gvskip:"]
    code += ["add rsp, 8"] + [f"pop {r}" for r in reversed(saved)]
    if money and any(i.lower() == "$money" for _, i, _ in specs):
        syms.append(f"RegisterSymbol( GiveMoney, ModuleBase+{money['impl_rva']:#x} )")
    return code, syms + gnames_symbol(code, meta)


# ---- give functions that run through ProcessEvent (instance / Blueprint / UClass* parameters) --------------------------------------
PE_BUF = 0x20            # parameter buffer at [rsp+0x20]
PE_MAX = 0x58            # ends at PE_COUNT


def pe_instance_chain(e, dump, cache, fn):
    """Pointer chain from GWorld to the live instance the function is called on, searched in THIS dump (the dump's own instance of the
    owner class) and cached in <dump>.items.json. -> dict(hops=[...], text=str) or None."""
    if fn.get("chain_live"):                                    # learned by a successful "Give now" on the real console: always preferred
        return fn["chain_live"]
    if fn.get("chain"):
        return fn["chain"]
    import ueitems_chain
    from ueobj import CF, RF_ClassDefaultObject
    owner = e.find(fn["owner"], CF["Class"])
    if not owner:
        return None
    memo, insts = {}, []
    for i, o in e.arr:
        if (e.flags(o) or 0) & RF_ClassDefaultObject:
            continue
        c = e.cls(o)
        if c not in memo:
            x, n = c, 0
            while x and x != owner and n < 40:
                x, n = e.super(x), n + 1
            memo[c] = x == owner
        if memo[c]:
            insts.append(o)
    roots, _ = ueitems_chain.world_root(e, dump)
    best = None
    for toks, leaf, hops_n, _ in ueitems_chain.find_chain(e, insts, roots, 6, say=lambda *a: None, span=8):    # all instances, one walk
        if leaf:
            continue
        pref = 0 if any(t.startswith(("LocalPlayers", "PlayerController", "Pawn", "PlayerState")) for t in toks) else 1
        key = (pref, len(toks))
        if best is None or key < best[0]:
            best = (key, toks)
    if not best:
        return None
    fn["chain"] = dict(hops=best[1][1:], text=".".join(best[1]))
    try:
        json.dump(cache, open(ueitems.cache_path(dump), "w", encoding="utf-8"))
    except OSError:
        pass
    return fn["chain"]


def fnv1a(b):
    h = 0x811c9dc5
    for x in b:
        h = ((h ^ x) * 0x1000193) & 0xffffffff
    return h


def name_ref(e, idx):
    """(header, FNV-1a of the characters) of the dump's FName `idx`, for name_routine: two constants per name, whatever its length.
    (0, idx) when the name table cannot be walked: that index is valid for the dump's session only.
    ponytail: 32-bit hash, one in ~10^4 items could hit another name of the same header; compare the characters if one ever does."""
    import ueitems_pe
    raw = ueitems_pe.name_raw(e, idx)
    if not raw:
        print(f"   warning: name {e.pool.name(idx)} is fixed to this dump's session (name table not searchable)")
        return 0, idx
    return int.from_bytes(raw[:2], "little"), fnv1a(raw[2:])


def name_routine(L, e):
    """asm subroutine {L}: esi = header, edi = hash -> r11d = this session's FName index (indices move between sessions, see
    ueitems_pe.name_raw), -1 if the name does not exist; esi = 0: r11d = edi. Walks the FNamePool, only runs when a cheat is switched on.
    Clobbers rax, rcx, rdx, r8-r11."""
    head = [f"{L}:", "test esi, esi", f"jnz {L}w", "mov r11d, edi", "ret", f"{L}w:"]
    import ueitems_pe
    if not ueitems_pe.name_raw(e, 0):                         # no walkable table in this game: only fixed indices reach this
        return head + ["mov r11d, -1", "ret"]
    p = e.pool
    st = p.stride
    return head + ["push r12", "push r13", "lea r8, [GNames]", "mov r9d, dword ptr [r8+8]", "xor r10d, r10d",
                   f"{L}b:", "cmp r10d, r9d", f"jg {L}no",
                   "mov rax, qword ptr [r8+r10*8+0x10]", "test rax, rax", f"jz {L}x",
                   f"mov edx, {(1 << p.block_bits) * st:#x}", "cmp r10d, r9d", f"jne {L}c", "mov edx, dword ptr [r8+0xc]", f"{L}c:", "add rdx, rax",
                   f"{L}e:", "cmp rax, rdx", f"jae {L}x",
                   "movzx ecx, word ptr [rax]", "cmp ecx, esi", f"jne {L}a",
                   "shr ecx, 6", "test esi, 1", f"jz {L}h", "add ecx, ecx", f"{L}h:",
                   "lea r12, [rax+2]", "mov r11d, 0x811c9dc5",
                   f"{L}l:", "movzx r13d, byte ptr [r12]", "xor r11d, r13d", "imul r11d, r11d, 0x1000193", "inc r12", "dec ecx", f"jnz {L}l",
                   "cmp r11d, edi", f"je {L}f", "mov ecx, esi",
                   f"{L}a:", "mov r11d, ecx", "shr r11d, 6", f"jz {L}x",                  # length 0: the rest of the block is unused
                   "test ecx, 1", f"jz {L}s", "add r11d, r11d", f"{L}s:", f"add r11d, {2 + st - 1}", f"and r11d, {-st & 0xffffffff:#x}", "add rax, r11", f"jmp {L}e",
                   f"{L}f:", "sub rax, qword ptr [r8+r10*8+0x10]"] + ([f"shr rax, {st.bit_length() - 1}"] if st > 1 else []) + \
                  ["mov r11d, r10d", f"shl r11d, {p.block_bits}", "or r11d, eax", f"jmp {L}r",
                   f"{L}x:", "inc r10d", f"jmp {L}b",
                   f"{L}no:", "mov r11d, -1",
                   f"{L}r:", "pop r13", "pop r12", "ret"]


def object_routine(L, meta, e):
    """asm subroutine {L}: r11d = FName index, ecx = FName number -> rax = first live object of that name, 0 if none (walks the GObjects
    array). Clobbers rdx, r13-r15."""
    lay, isz, ooff = meta["gobjects_layout"], meta["fuobjectitem_size"], meta.get("fuobjectitem_object_offset", 0)
    per = e.arr.per_chunk
    if len(lay) != 5 or per & (per - 1):
        sys.exit("this game's GObjects array layout is not supported by the ProcessEvent cheat export yet")
    return [f"{L}:", "lea r14, [GObjects]", f"mov r15d, dword ptr [r14+{lay[2]:#x}]", f"mov r13, qword ptr [r14+{lay[0]:#x}]", "test r13, r13", f"jz {L}z",
            "xor r14d, r14d",
            f"{L}o:", "cmp r14d, r15d", f"jge {L}z",
            "mov eax, r14d", f"shr eax, {per.bit_length() - 1}", "mov rax, qword ptr [r13+rax*8]", "test rax, rax", f"jz {L}n",
            f"mov edx, r14d", f"and edx, {per - 1:#x}", f"imul rdx, rdx, {isz}", f"mov rax, qword ptr [rax+rdx+{ooff:#x}]" if ooff else "mov rax, qword ptr [rax+rdx]",
            "test rax, rax", f"jz {L}n",
            f"cmp dword ptr [rax+{e.O['UObject_Name']:#x}], r11d", f"jne {L}n",
            f"cmp dword ptr [rax+{e.O['UObject_Name'] + 4:#x}], ecx", f"jne {L}n", "ret",
            f"{L}n:", "inc r14d", f"jmp {L}o",
            f"{L}z:", "xor eax, eax", "ret"]


# frame of a give routine: the rule's values behind the parameter buffer
PE_FRAME = 0x98          # return address + this = 16-aligned at the ProcessEvent call
PE_COUNT, PE_HDR, PE_HASH, PE_NUM = 0x78, 0x7c, 0x80, 0x84


def give_routine(L, e, fn, chain, meta):
    """asm subroutine {L} for one give function, shared by all its rules: edx = amount, esi/edi/ecx = the item (name_ref + FName number, or
    edi = its number for an int id). Finds the live object (pointer chain from GWorld, or the class by name for static functions) and the
    UFunction (class chain), builds the parameter buffer and calls ProcessEvent (re-entering the hook with the flag already at 2, so it
    passes straight through)."""
    import uecheat
    from ueobj import CF
    O = e.O
    owner = e.find(fn["owner"], CF["Class"])
    ufn = e.find_in_outer(fn["name"], fn["owner"])
    if not owner or not ufn:
        sys.exit(f"{fn['owner']}::{fn['name']} is not in this dump (scan again)")
    size = max([p["off"] + p["size"] for p in fn["params"]] + [8])
    if size > PE_MAX or any(p["role"] == "pawn" for p in fn["params"]):
        sys.exit(f"{fn['owner']}::{fn['name']}: parameter layout not supported by the cheat export (size {size:#x}"
                 f"{', needs the player pawn' if any(p['role'] == 'pawn' for p in fn['params']) else ''}); Give now works")
    out_l = f"{L}out"

    def name_of(idx):
        hdr, h = name_ref(e, idx)
        return [f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", "call pegname", "cmp r11d, -1", f"je {out_l}"]
    out = [f"{L}:", f"sub rsp, {PE_FRAME:#x}", f"mov dword ptr [rsp+{PE_COUNT:#x}], edx", f"mov dword ptr [rsp+{PE_HDR:#x}], esi",
           f"mov dword ptr [rsp+{PE_HASH:#x}], edi", f"mov dword ptr [rsp+{PE_NUM:#x}], ecx"]
    if chain is None:                                         # static / `this` never read: class by name, `this` = GWorld (ignored)
        out += name_of(e.i32(owner + O["UObject_Name"])) + ["xor ecx, ecx", "call pegobj", "test rax, rax", f"jz {out_l}",
                                                            "mov r13, rax", "mov rbx, [GWorld]", "test rbx, rbx", f"jz {out_l}"]
    else:
        out += ["mov rcx, [GWorld]", "test rcx, rcx", f"jz {out_l}"]
        uecheat.path_code(e, uecheat.cls_of(e, "World"), chain["hops"], "rcx", out_l, out)
        out += ["test rcx, 7", f"jnz {out_l}", "test rcx, rcx", f"jz {out_l}", "mov rbx, rcx", f"mov r13, [rbx+{O['UObject_Class']:#x}]"]
    out += name_of(e.i32(ufn + O["UObject_Name"]))
    out += ["mov r14d, 0x4000",                               # UFunction: walk the class chain
            f"{L}cl:", "test r13, r13", f"jz {out_l}",
            f"mov r12, [r13+{O['UStruct_Children']:#x}]",
            f"{L}fn:", "test r12, r12", f"jz {L}sp", "dec r14d", f"jz {out_l}",
            f"cmp dword ptr [r12+{O['UObject_Name']:#x}], r11d", f"je {L}ok",
            f"mov r12, [r12+{O['UField_Next']:#x}]", f"jmp {L}fn",
            f"{L}sp:", f"mov r13, [r13+{O['UStruct_SuperStruct']:#x}]", f"jmp {L}cl",
            f"{L}ok:"]
    for o in range(0, (size + 7) & ~7, 8):                    # zero the parameter buffer
        out.append(f"mov qword ptr [rsp+{PE_BUF + o:#x}], 0")
    for p in fn["params"]:
        r, o = p["role"], PE_BUF + p["off"]
        if r == "id":
            if p["type"] not in ("UClass", "UObject", "FName"):
                out += [f"mov eax, dword ptr [rsp+{PE_HASH:#x}]", f"mov dword ptr [rsp+{o:#x}], eax"]
            else:
                out += [f"mov esi, dword ptr [rsp+{PE_HDR:#x}]", f"mov edi, dword ptr [rsp+{PE_HASH:#x}]", "call pegname", "cmp r11d, -1", f"je {out_l}"]
                if p["type"] == "FName":
                    out += [f"mov dword ptr [rsp+{o:#x}], r11d", f"mov eax, dword ptr [rsp+{PE_NUM:#x}]", f"mov dword ptr [rsp+{o + 4:#x}], eax"]
                else:
                    out += [f"mov ecx, dword ptr [rsp+{PE_NUM:#x}]", "call pegobj", "test rax, rax", f"jz {out_l}", f"mov qword ptr [rsp+{o:#x}], rax"]
        elif r == "count":
            if p["ptype"] not in ("IntProperty", "UInt32Property", "Int64Property"):
                sys.exit(f"{fn['name']}: amount parameter is {p['ptype']}, only integers are supported in the cheat export")
            out += [f"mov eax, dword ptr [rsp+{PE_COUNT:#x}]"] + ([f"mov dword ptr [rsp+{o:#x}], eax"] if p["size"] <= 4 else ["movsxd rax, eax", f"mov qword ptr [rsp+{o:#x}], rax"])
        elif r == "world":
            out += ["mov rax, [GWorld]", f"mov qword ptr [rsp+{o:#x}], rax"]
        elif r == "full":
            out.append(f"mov dword ptr [rsp+{o:#x}], 0x3f800000")          # 1.0f
        elif r == "pc":                                                       # the local PlayerController, as the hook walks it
            out += ["mov rcx, [GWorld]", "test rcx, rcx", f"jz {out_l}"]
            uecheat.path_code(e, uecheat.cls_of(e, "World"), ["OwningGameInstance", "LocalPlayers[0]", "PlayerController"], "rcx", out_l, out)
            out.append(f"mov qword ptr [rsp+{o:#x}], rcx")
    out += ["mov rdi, rbx", "mov rsi, r12", f"lea rdx, [rsp+{PE_BUF:#x}]", "movabs rax, ProcessEvt", "call rax",
            f"{out_l}:", f"add rsp, {PE_FRAME:#x}", "ret"]
    return out


def item_args(e, it, fn):
    """Lines that load esi/edi/ecx for give_routine: the item as name_ref + FName number, or as its number for an int id."""
    idp = next((p for p in fn["params"] if p["role"] == "id"), None)
    if not idp or not it:
        return ["xor esi, esi", "xor edi, edi", "xor ecx, ecx"]
    if idp["type"] in ("UClass", "UObject"):
        ca = it.get("class_addr") or sys.exit(f"{it['id']} has no class in the catalog (scan again)")
        idx, num = e.i32(ca + e.O["UObject_Name"]), e.i32(ca + e.O["UObject_Name"] + 4) or 0
    elif idp["type"] == "FName":
        idx, num = it["idx"], it["num"]
    else:
        return ["xor esi, esi", f"mov edi, {it['numid'] & 0xffffffff:#x}", "xor ecx, ecx"]
    hdr, h = name_ref(e, idx)
    return [f"mov esi, {hdr:#x}", f"mov edi, {h:#x}", f"mov ecx, {num:#x}"]


def pe_block(e, dump, cache, specs, meta):
    """Inventory rules for a game whose give function is called through ProcessEvent: per rule only its flag test and values, the walk
    itself is shared (give_routine per function, name_routine, object_routine): 19 rules took 13194 bytes one by one (Alien) and 1399 this
    way (Valor Mortis), small enough for code cave pools. -> (asm lines, RegisterSymbol lines)"""
    g = cache["give"]
    cat = {i["id"].lower(): i for i in cache["catalog"]}
    meta.get("process_event") or sys.exit("offsets.json has no process_event")
    meta.get("gobjects") or sys.exit("offsets.json has no gobjects")
    saved = ["rsi", "rdi", "r8", "r9", "r10", "r11"]
    code = [f"push {r}" for r in saved] + ["sub rsp, 8"] + [f"push {r}" for r in ("rbx", "r12", "r13", "r14", "r15")] + ["sub rsp, 0x78"]
    routines = {}                                             # (owner, name) -> [label, function, chain]
    for k, iid, _ in specs:
        kind = iid.lower() if iid.startswith("$") else "item"                # $money / $expand / an item id
        fns = [x for x in ([g.get(kind[1:])] + list(g.get(kind[1:] + "_alts") or []) if kind != "item"
                           else [g["item"]] + list(g.get("item_alts") or [])) if x]
        fns or sys.exit({"$money": "this game has no money function", "$expand": "this game has no inventory expansion function"}.get(
            kind, "no give function in the scan"))
        it = None if kind != "item" else (cat.get(iid.lower()) or sys.exit(f"item rule: {iid} is not in the item catalog of this dump (scan again?)"))
        fn = chain = None
        for f in fns:
            if f.get("via") != "pe":
                continue
            free = f["static"] or f.get("this_unused")
            ch = None if free else pe_instance_chain(e, dump, cache, f)
            if free or ch:
                fn, chain = f, ch
                break
        fn or sys.exit(f"no pointer chain from GWorld reaches a {fns[0]['owner']} in this dump (take the dump in-game, in the world, not in a menu)")
        key = (fn["owner"], fn["name"])
        if key not in routines:
            routines[key] = [f"peg{len(routines)}", fn, chain]
            if chain:
                print(f"   {fn['owner']}::{fn['name']}: {chain['text']}")
        lim = item_limit(it)
        code += [f"cmp byte ptr [Flag{k}], 1", f"jne pe{k}_end", f"mov byte ptr [Flag{k}], 2", f"mov edx, dword ptr [Val{k}]"] + \
                ([f"mov eax, {lim}", "cmp edx, eax", "cmovg edx, eax"] if lim else []) + item_args(e, it, fn) + \
                [f"call {routines[key][0]}", f"pe{k}_end:"]
    code.append("jmp pegskip")
    for label, fn, chain in routines.values():
        code += give_routine(label, e, fn, chain, meta)
    code += name_routine("pegname", e) + object_routine("pegobj", meta, e) + ["pegskip:"]
    code += ["add rsp, 0x78"] + [f"pop {r}" for r in ("r15", "r14", "r13", "r12", "rbx")] + ["add rsp, 8"] + [f"pop {r}" for r in reversed(saved)]
    return code, [f"RegisterSymbol( ProcessEvt, ModuleBase+{meta['process_event']:#x} )"] + gnames_symbol(code, meta)


def rule_entries(cache, specs):
    """specs = [(k, id, value)] from rules "item:ITEM_ID" -> ([(k, idx, num)], money k or None)"""
    cat = {i["id"].lower(): i for i in cache["catalog"]}
    entries, money = [], None
    for k, iid, _ in specs:
        if iid.lower() == "$money":
            money = k
            continue
        it = cat.get(iid.lower()) or sys.exit(f"item rule: {iid} is not in the item catalog of this dump (scan again?)")
        entries.append((k, it["idx"], it["num"]))
    return entries, money


# ---- stand-alone .TLH ----------------------------------------------------------------------------------------------------------
def parse(argv):
    a = dict(cand=0, cat=None, match=None, skip=None, amount=99, money=True, chain=None)
    pos, it = [], iter(argv)
    for x in it:
        if x == "--cand": a["cand"] = int(next(it))
        elif x == "--cat": a["cat"] = {c.lower() for c in next(it).split(",")}
        elif x == "--match": a["match"] = next(it).lower()
        elif x == "--skip": a["skip"] = next(it).lower()
        elif x == "--amount": a["amount"] = int(next(it))
        elif x == "--no-money": a["money"] = False
        elif x == "--chain": a["chain"] = next(it)
        else: pos.append(x)
    if len(pos) != 2:
        sys.exit(__doc__)
    return pos, a


def pick_items(cache, a):
    out = []
    for it in cache["catalog"]:
        cat = it["cat"].lower()
        if a["cat"] is None and cat in ("invalid", "money"):
            continue
        if a["cat"] is not None and cat not in a["cat"]:
            continue
        if a["match"] and a["match"] not in it["id"].lower():
            continue
        if a["skip"] and a["skip"] in it["id"].lower():
            continue
        out.append(it)
    return out


def default_amount(it, want):
    lim = it.get("lim_bag") or 0
    return 1 if lim <= 1 else min(want, lim)


def main(argv):
    import ueobj, uecheat
    (dump, out), a = parse(argv)
    cache = ueitems.load_cache(dump)
    e = ueobj.open_engine(dump, log=lambda *x: None)
    meta = json.load(open(os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "offsets.json")))
    if not meta.get("gworld") or not meta.get("process_event"):
        sys.exit("offsets.json lacks gworld/process_event (rerun uegen with the console reachable)")
    ch = resolve_chain(e, dump, cache, a["cand"], a["chain"])
    items = pick_items(cache, a)
    mems, cheats, specs = [], [], []
    k = 0
    if a["money"]:
        k += 1
        mems += [f"GetMem( Flag{k}, 1 )", f"GetMem( Val{k}, 4 )"]
        cheats.append(("Money", "Sets the money (edit the value)", k, 999999))
        specs.append((k, "$money", 999999))
    for it in items:
        k += 1
        mems += [f"GetMem( Flag{k}, 1 )", f"GetMem( Val{k}, 4 )"]
        lim = it.get("lim_bag")
        cheats.append((it["id"], it["cat"] + (f", bag limit {lim}" if lim else ""), k, default_amount(it, a["amount"])))
        specs.append((k, it["id"], 0))
    entries, money = rule_entries(cache, specs)
    syms = [f"RegisterSymbol( GWorld, ModuleBase+{meta['gworld']:#x} )"] + ([static_symbol(cache, ch)] if ch["static"] else [])
    pe = e.d.base + meta["process_event"]
    asm = "\n".join(mems + syms + ["", "pushfq", "push rax", "push rcx", "push rdx", ""] + world_block(e, ch, entries, money)
                    + ["done:", "pop rdx", "pop rcx", "pop rax", "popfq"] + uecheat.site_instructions(e, pe))
    info = json.load(open(dump[:-4] + ".json")) if dump.endswith(".bin") else {}
    cusa = int(info["title_id"][4:]) if info.get("title_id", "")[4:].isdigit() else 0
    aob = "-".join(f"{b:02X}" for b in uecheat.unique_aob(e, pe))
    game = uecheat.xml_escape(os.path.basename(out).split("_")[0])
    x = ['<?xml version="1.0" encoding="utf-8" standalone="no"?>',
         f'<Trainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" Game="{game}" '
         f'Creator="ueitems" CUSA="{cusa}" Version="{uecheat.tlh_version(info.get("version"))}" Process="eboot.bin" Legacy="1">',
         '  <StartUP Name="Item hook (ProcessEvent)" Description="' + uecheat.xml_escape(ch.get("text", "static inventory")) + '">',
         '    <Entry>', '      <Section>0</Section>', f'      <AOB>{aob}</AOB>', '      <Type>Injection</Type>',
         f'      <Assembly>{uecheat.xml_escape(asm)}</Assembly>', '    </Entry>', '  </StartUP>']
    for name, desc, k, v in cheats:
        x.append(f'  <Cheat Name="{uecheat.xml_escape(name)}" Description="{uecheat.xml_escape(desc)}">')
        for sym, typ, value in ((f"Flag{k}", "Boolean", 0), (f"Val{k}", "Integer", v)):
            x += ['    <Entry>', '      <Section>0</Section>', f'      <Symbol>{sym}</Symbol>', '      <Type>Value</Type>',
                  f'      <ValueType>{typ}</ValueType>', f'      <Value>{value}</Value>', '    </Entry>']
        x.append('  </Cheat>')
    x.append('</Trainer>')
    open(out, "w", encoding="utf-8-sig", newline="\n").write("\n".join(x) + "\n")
    print(f"wrote {out}: {len(items)} items{' + money' if a['money'] else ''}, hook at base+{meta['process_event']:#x}, asm {len(asm.splitlines())} lines")


if __name__ == "__main__":
    main(sys.argv[1:])
