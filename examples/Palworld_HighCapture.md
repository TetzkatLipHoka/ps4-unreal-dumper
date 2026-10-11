# Example: every ball catches like the best ball, Palworld (PPSA23126, UE4, PS5)

Goal: aim shows 100% with any sphere, no crit involved. Method: find the table that maps
each ball tier to its level, overwrite the levels live to prove it, then freeze it with
a Reaper patch. Only the Python tools and ps4debug — no PRX, no compiler.

## 0. Requirements

- PS5 with GoldHEN + ps4debug, Palworld world loaded.
- PC: Python 3.12, `pip install ps4debug`, working directory `tools/`.
- Two kinds of addresses: **offsets** (like `+0x14C8`, same forever for this game version)
  and **heap addresses** (like `0x1a4103bfc0`, change every game start).

## 1. Dump, then grep the SDK for the ball table

```bash
python ps4ue.py 192.168.50.2
grep -n "CaptureSphereLevelMap\|GetCaptureLevelBySphereType" dumps/PPSA23126/SDK/SDK_HEADERS/Pal_classes.hpp
```

```
TMap<EPalCaptureSphereLevelType, int32>  CaptureSphereLevelMap;   // 0x14c8
// int32_t GetCaptureLevelBySphereType(EPalCaptureSphereLevelType SphereType);
```

So every ball tier looks its level up in one map at `GameSetting + 0x14C8`.
The tiers themselves:

```bash
grep -A14 "enum class EPalCaptureSphereLevelType" dumps/PPSA23126/SDK/SDK_HEADERS/Pal_structs.hpp
```

```
Undefined = 0, Normal = 1, Mega = 2, Giga = 3, Master = 4, Tera = 5,
Legend = 6, Ultimate = 7, Exotic = 8, Ancient_1 = 9, Ancient_2 = 10
```

## 2. Read the live table (proves the map is real)

The settings object lives under the GameInstance and never despawns. Its live address
comes from the `GObjects` index for this boot (indices change per boot — always
re-read them from the fresh `SDK/GObjects.txt`, never reuse old ones):

```
[00015971] ... BP_ThrowCaptureObjectBase_C ...   <- example: index 0x15971 THIS boot
```

Read 80 bytes at `GameSetting + 0x14C8`. A `TMap` starts with its pairs array:
`Data` (pointer), `Num`, `Max`. Here `Num = 10` — ten tier entries. Each entry is
16 bytes: `key, level` plus housekeeping, so levels sit at `+4, +20, +36, ...`:

```
slot0 key=1 level=7      <- Normal ball = level 7 (this is why aim shows ~37%)
slot1 key=2 level=14
slot2 key=3 level=20
slot3 key=5 level=27
slot4 key=4 level=33
slot5 key=6 level=38
...
slot9 key=10 level=64
```

Now you know exactly why a base ball shows a low percent: its level is 7.

## 3. Prove it live: write 200000, aim, observe

Overwrite the 10 level values (keys untouched) and re-read:

```
all levels: [200000, 200000, ... (10x)]
```

Aim at the same pal with the same base ball: the rate jumps to 100%.
Keep the crit bonus at 0 during this test so no crits pollute the observation —
100% here comes purely from ball level. This is the whole proof; everything
after is just making it permanent.

## 4. Make it permanent: ReapPatch (not a static edit!)

Heap moves every boot, so you cannot patch the numbers in place. Instead hook a
function that runs constantly (`ProcessEvent` entry — verified unique 32-byte AOB)
and re-write the table on every call:

```
GWorld [0x87D5C90]            <- fixed: eboot base 0x400000 never moves on PS5
  -> +0x1B8 OwningGameInstance
  -> +0x498 GameSetting
  -> guard: Pairs.Num == 10, else do nothing (never corrupt an unexpected layout)
  -> Pairs.Data + 4, +20, ... +148 = 200000   (10 unrolled writes)
```

Assembly rules that one crash taught us (64-bit!):

- Only `RAX` (free scratch at function entry) — no pushes, nothing to unbalance.
- `mov rax,0x87D5C90` + `mov rax,[rax]` — never `mov rcx,[0x...]`, which encodes
  RIP-relative and reads garbage.
- No local labels — null checks jump straight back to the Original bytes.

Result: `high capture chance.ReapPatch` with an on/off `HighCap` toggle. Enable it,
aim with any ball: 100%, no crits.

## 5. Which tool for what

| Goal | Tool |
|---|---|
| dump + SDK | `ps4ue.py <ip>` |
| find tables/offsets/enums | `grep` in `SDK/SDK_HEADERS/` |
| read/write a simple number live | `uelive.py <dump> <ip> Class Property [Value]` |
| raw bytes (maps, arrays) | `peek.py` / small `ps4debug` scripts |
| permanent toggleable patch | ReapPatch (this chapter) |
