# Craft Anything — Complete Guide
### PS5 · Unreal Engine 5 · Palworld (PPSA23126 · 01.102.999)

**Tools used:** UEDumper (`ps4-unreal-dumper`: `ps4ue.py`, `uegen.py`, `ueobj.py`, `uelive.py`, `uecall.py`, `uecheat.py`, `uediff.py`, `peek.py`) + **Reaper Studio (RS)** as the patch container.

> **Read this first:** RS is only where the finished patch lives. Everything that actually creates a cheat happens **before** RS: dumping the game, reading the SDK, finding the real address of the code, understanding the structure, and choosing a safe place to patch. This guide teaches that part — with the full "Craft Anything" patch as the worked example.

---

## Contents

1. [The method in one picture](#1-the-method-in-one-picture)
2. [Dumping the game](#2-dumping-the-game)
3. [Reading the dump and the SDK](#3-reading-the-dump-and-the-sdk)
4. [Finding real game addresses (and the addresses to breakpoint)](#4-finding-real-game-addresses-and-the-addresses-to-breakpoint)
5. [Case study: how Craft Anything was designed](#5-case-study-how-craft-anything-was-designed)
6. [From hook to patch: AOBs, caves and the ReapPatch format](#6-from-hook-to-patch-aobs-caves-and-the-reappatch-format)
7. [Testing and troubleshooting](#7-testing-and-troubleshooting)
8. [Doing it again for another cheat or game](#8-doing-it-again-for-another-cheat-or-game)
9. [Appendices](#appendix-a--fpalitemrecipe-layout)

---

## 1. The method in one picture

Every UE cheat is built the same way:

```text
  [1] DUMP                   [2] READ                      [3] FIND
 ps4ue.py  ───────────►  SDK headers + GObjects  ───────────►  the code that
 (dump.bin / dump.json)  (names, structs, offsets,             implements the
                          function entry points)               behaviour
                                                                   │
                                                                   ▼
  [6] PATCH                  [5] DESIGN                     [4] VERIFY
 Reaper Studio  ◄────────  pick the smallest seam   ◄────────  UELive / uecall
 source + cave + label     (branch, store, call,              live read/write
                            return value, struct copy)
```

The four kinds of "addresses" you will deal with:

| Kind | Example | Stable? | What it is |
|---|---|---|---|
| **VA** (virtual address) | `0x301E57D` | No (changes with game updates) | Address in the running process |
| **Module offset / "native"** | `0x2C1E57D` | Only per game build | `VA − 0x400000` for this title |
| **Struct offset** | `Material1_Id = 0x24` | **Yes** (per SDK) | Field offset inside a class/struct |
| **Heap pointer** | `0x2E4A1C080` | No (changes every boot) | Address of a live object instance |

Two rules that save you hours:

1. **Struct offsets and class layouts are stable.** Heap addresses are not. Never hardcode a heap address in a patch.
2. **Code addresses move when the game updates.** Never hardcode a VA either — patches locate code with **byte patterns (AOB)**.

---

## 2. Dumping the game

The dumper runs on your PC, reads the PS5's memory through `ps4debug`/GoldHEN **once**, and then works completely offline on `dump.bin`. There are no per-game profiles — every engine offset is measured from the dump.

### 2.1 What you need

- PS5 with GoldHEN + `ps4debug` (or `ps4debug-ng`) reachable over the network.
- The game **at a loaded level** — the pause menu is fine. The main menu has no level objects / player characters.
- On the PC: Python 3.12, `pip install ps4debug` (optional `capstone` for `uediff.py --xref`, `numpy` for `uediff.py`).

### 2.2 Dump + generate the SDK

```bat
cd tools
python ps4ue.py 192.168.50.2
```

This produces (inside `../dumps/<TitleID>/`):

| File | What it is | What you use it for |
|---|---|---|
| `dump.bin`, `dump.json` | Memory image (zero pages omitted) + region index | Disassembly, AOB scanning, offline analysis |
| `dump.bin.engine.json` | Cached finder results | Makes later runs take seconds |
| `SDK/GameDefines.hpp` | GNames, GObjects, GWorld, ProcessEvent, all reflection offsets | The "root" of the SDK |
| `SDK/SDK_HEADERS/*_classes.hpp` | One header per package: class fields + every UFunction (with `native 0x...` addresses) | **The most important folder** |
| `SDK/SDK_HEADERS/*_structs.hpp` | Exact struct layouts: field offsets + sizes | Reading/writing data correctly |
| `SDK/SDK_HEADERS/*_parameters.hpp` | Per-function parameter structs, including out-parameters | Understanding function calls |
| `SDK/offsets.json` | The same values, machine readable | Used by the live tools |
| `SDK/GObjects.txt` | Every object with address and full name | Finding classes, functions, widgets |
| `SDK/ReClass.rcnet` | ReClass.NET project (UWorld hangs off GWorld) | Interactive structure browsing |
| `rpc_stub.txt` | Address of the RPC stub | `uecall` / `ue4gt` |

Need the SDK again later (or from a shared dump)? No console needed:

```bat
python ps4ue.py ../dumps/PPSA23126/dump.bin
```

### 2.3 dump.json — the map of the memory image

Example from our dump (the first lines):

```text
executable        start=00400000 end=06578000 off=00000000 prot=5
executable        start=06578000 end=082D0000 off=06178000 prot=1
executable        start=082D0000 end=08804000 off=07ED0000 prot=3
libc.prx          start=80000000 end=80118000 off=08404000 prot=5
...
```

Each region has `start` (VA), `end` (VA), `offset` (offset inside `dump.bin`) and `prot` (5 = r-x, 1 = r--, 3 = rw-).

**The base rule for this title:** `GameDefines.hpp` says *"engine globals and reflection offsets (relative to eboot base 0x400000)"*, and the generated code adds it:

```cpp
#define UE_MODULE_BASE 0x400000ULL
#define UE_ADDR(off)   (UE_MODULE_BASE + (off))
```

So:

```text
VA = 0x400000 + "native" offset
```

The main executable starts at `0x400000`, and because the first region is stored at file offset `0`, the first executable region of `dump.bin` maps almost 1:1: `file offset 0 → VA 0x400000`. That is why you can load `dump.bin` in a disassembler with base `0x400000` and see the game code. For all other regions use the `start/end/offset` triple from `dump.json`.

**Heap vs. code:**
- Blueprint-generated classes, CDOs, UFunction objects: fixed enough to navigate.
- Live widget instances, player controllers, models: addresses change every boot. `uelive.py` re-reads them live from GObjects — that is why it always asks for the dump: to know the class/offset layout, not the old addresses.

---

## 3. Reading the dump and the SDK

### 3.1 The three header types — how they connect

**`*_classes.hpp`** lists every class, its inheritance, its fields, and every function:

```cpp
// int32_t CalcMaxProductableNum();  // flags 0x14020401  native 0x2410e40
// void GetRequiredMaterialInfos(TArray<struct FPalStaticItemIdAndNum>* RequiredMaterialInfos, bool OneUnit);  // flags 0x04420401  native 0x2410ea0
```

The `native 0x...` value is the **function entry point** (more on this in §4.2).

**`*_structs.hpp`** gives the layout. For `FPalItemRecipe`:

```cpp
struct FPalItemRecipe {
    struct FName   Product_Id;          // 0x0008
    int32_t        Product_Count;       // 0x0010
    float          WorkAmount;          // 0x0014
    ...
    struct FName   Material1_Id;        // 0x0024
    int32_t        Material1_Count;     // 0x002c
    ...
};                                      // 0x0088 total
```

This layout is the whole cheat. Every field is just a fixed offset from the start of the row. If you zero `Material1_Id` at `+0x24`, the game's own UI treats that slot as "None".

**`*_parameters.hpp`** gives the exact parameter struct of every UFunction — including **out-parameters**:

```cpp
struct UWBP_IngameMenu_WorkSpace_C_Update_Recipe_Detail_Params {
    TArray<struct FPalStaticItemIdAndNum> CallFunc_GetRequiredMaterialInfos_RequiredMaterialInfos; // 0x0008
};
```

Out-parameters are the secret to this cheat (see §4.5).

### 3.2 Searching the SDK

You do not need to read 100 000 lines. Search by feature name:

```bat
findstr /i "craft recipe" SDK\SDK_HEADERS\Pal_classes.hpp
findstr /i "WorkSpace"    SDK\SDK_HEADERS\WBP_IngameMenu_WorkSpace_classes.hpp
findstr /i "Productable"  SDK\SDK_HEADERS\*.hpp
findstr /i "RequiredMaterial" SDK\SDK_HEADERS\*.hpp
```

And in `GObjects.txt`:

```bat
findstr /i "WBP_IngameMenu_WorkSpace" SDK\GObjects.txt
findstr /i "PalItemUtility"           SDK\GObjects.txt
findstr /i "ItemRecipe"               SDK\GObjects.txt
```

**The trick that saves the most time:** the BP class headers list every function a blueprint calls, with `CallFunc_` names. For example, `WBP_IngameMenu_WorkSpace_C` contains:

- `CallFunc_CalcMaxProductableNum_ReturnValue` → the quantity limit
- `CallFunc_GetProductItemRequiredMaterialInfos_OutRequiredMaterialInfos` → the utility array
- `CallFunc_GetRequiredMaterialInfos_RequiredMaterialInfos` → the model array
- `CallFunc_BP_FindRow_ReturnValue` (`FPalItemRecipe`) → the recipe row

So a single grep tells you exactly **which native functions feed the craft UI**. That is how the whole feature was mapped before a single byte was patched.

### 3.3 The functions that matter for Craft Anything

| Symbol | `native` | VA | Role |
|---|---|---|---|
| `UPalUIProductSettingModel::CalcMaxProductableNum()` | `0x2410e40` | `0x2810E40` | Thunk → **worker `0x301DF50`**; computes how many you can craft |
| `UPalUIProductSettingModel::GetRequiredMaterialInfos(TArray*, bool)` | `0x2410ea0` | `0x2810EA0` | Thunk → **worker `0x301E540`**; builds the detail model's material array |
| `UPalItemUtility::GetProductItemRequiredMaterialInfos(...)` | `0x21c5d50` | `0x25C5D50` | Builds the utility array (**H2**) and the sibling map builder (**H3**) |
| `UPalMasterDataTableAccess_ItemRecipe::BP_FindRow(FName, bool*)` | `0x22636c0` | `0x26636C0` | Returns a **copy of the recipe row** — the source of the material rows (**H4**) |
| Shared requirement-list builder | — | `0x2CD2D30` | Fills the material entries (**H1** replaces its call) |
| BP script thunk (all BP functions) | `0x30854e0` | `0x34854E0` | The interpreter entry for blueprint bytecode — **cannot** be used to hook an individual BP event |

---

## 4. Finding real game addresses (and the addresses to breakpoint)

This is the question everyone asks: *"I have the SDK, now what do I actually open in the disassembler?"*

### 4.1 From SDK value to VA

Take the `native` value from the header and add `0x400000`:

| SDK value | VA | What is there |
|---|---|---|
| `ProcessEvent_Offset = 0x3086340` | `0x3486340` | `UObject::ProcessEvent` |
| `native 0x2410e40` (`CalcMaxProductableNum`) | `0x2810E40` | Exec thunk (marshals params) |
| `native 0x2410ea0` (`GetRequiredMaterialInfos`) | `0x2810EA0` | Exec thunk |
| `native 0x22636c0` (`BP_FindRow`) | `0x26636C0` | Whole function |
| `native 0x21c5d50` (`GetProductItemRequiredMaterialInfos`) | `0x25C5D50` | Whole function |
| — | `0x301DF50` | `CalcMaxProductableNum` implementation |
| — | `0x301E540` | `GetRequiredMaterialInfos` implementation |

### 4.2 What `native` really points to: thunk vs. implementation

Every UFunction has an entry pointer; in this SDK it is the value at `UFunction + 0xD8` (`OFF_UFunction_ExecFunction`). For **native** (C++) UFunctions it usually points to a small **exec thunk** that:

1. sets up a local parameter frame,
2. reads the parameters from the FFrame (`call 0x34840d0` or `call 0x3484100`),
3. calls the real C++ implementation.

Example — `CalcMaxProductableNum` thunk at `0x2810E40`:

```asm
0x2810E40  push rbp
0x2810E41  mov  rbp, rsp
0x2810E44  push rbx
0x2810E46  mov  rax, qword ptr [rsi+0x20]   ; FFrame state
0x2810E4A  mov  rbx, rdx                   ; return storage
...
0x2810E59  call 0x301DF50                  ; ← the real worker
0x2810E5E  mov  dword ptr [rbx], eax       ; write return value
0x2810E66  ret
```

So when you want to change the *limit*, you do not patch the thunk — you patch **`0x301DF50`**, the function that actually computes it.

`BP_FindRow` is the other flavor: the whole body is in the same function (`0x26636C0`), including the parameter marshalling, the row copy, and the return.

> **Rule of thumb:** disassemble the `native` address. If the first instructions look like parameter plumbing (`[rsi+0x20]`, calls to `0x34840d0`/`0x3484100`) and then a `call` follows — follow that call. If it already contains the game logic — that is your worker.

### 4.3 Finding a function whose address you *don't* have

Three reliable ways, all offline:

**(a) Xrefs by struct offset** — "show me every instruction that touches offset `0x24`":

```bat
python uediff.py --xref dump.bin --disp 0x24 --write --mnem mov,movups,vmovups
```

`uediff.py` decodes the executable region with Capstone and prints instruction addresses + the instruction text. Narrow it with `--disp`, `--write` (writes only) and `--mnem`.

**(b) Call-site hunting** — if you know a worker is called from a thunk, or a helper (`0x2CD2D30`) is called from several places, search for the call encoding:

```text
E8 <rel32>     ; call target = next_instruction + rel32
```

You can compute `rel32 = target − (site + 5)` and search the dump for the 5-byte pattern.

**(c) GObjects + function objects** — `GObjects.txt` gives the address of every `UFunction` object. Read `+0xD8` at that address in the dump and you get its entry point (the same value the headers print as `native`). This is also the mechanical way the SDK generator itself resolves those numbers.

**(d) The parameter headers tell you the shape** — before hooking, always read the `*_parameters.hpp` struct for that function. If it has an out TArray/TMap, there is a good chance you can empty the result instead of fighting the builder.

### 4.4 Disassembling the dump

Any disassembler works. What matters is loading the **right bytes at the right base**:

1. Open `dump.json`, find the executable region. In our dump the main `.text` is `VA 0x400000 .. 0x6578000` at file offset `0`.
2. Load that slice into IDA/Ghidra/Rizin (or RS's disassembler) with **base = 0x400000**.
3. Jump to `VA = 0x400000 + native`.
4. For functions in other modules (`libc.prx`, etc.) use their `start/end/offset` region values.

The goal of disassembly is **not** to read the whole function. It is to find an instruction boundary where you can:

- replace an instruction (or a few) with a jump to your own code,
- and return to a known point with the same register/stack state.

### 4.5 The FFrame rule (why we can empty returned containers)

When a BP event calls a native function with an **out parameter**, the native thunk stores the *address of the caller's out variable* inside the FFrame. The code then does something like:

```asm
mov rdx, qword ptr [rbx+0x38]   ; rbx = FFrame*, +0x38 = out-param destination
lea rcx, [rbp-0x40]             ; local fallback used when no property path
test rdx, rdx
cmovne rcx, rdx                 ; rcx = caller's real out storage (or local)
mov dword ptr [rcx+8], 0        ; +8 = Num of TArray / TMap → count = 0
```

`TArray` and `TMap` in this engine start with `Data/…` and have `Num` at `+8`. **Zeroing that `Num` empties the container without freeing anything and without touching the builder.** The function still returns normally; the caller simply receives an empty array/map.

This is exactly what the `CraftV2UtilArrCave` and `CraftV2UtilMapCave` do (see §5.4).

### 4.6 Live verification with UELive

The dump tells you *what the code looks like*; UELive tells you *what the game is doing right now*.

```bat
:: list all live instances of a class (including subclasses)
python uelive.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 PalItemUtility

:: read a property on all instances
python uelive.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 PlayerController FOV

:: write a property (test / experiment)
python uelive.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 PlayerController FOV 120

:: call a UFunction through ProcessEvent and read back out params / return
python uecall.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 GameplayStatics SetGlobalTimeDilation WorldContextObject=0x... TimeDilation=0.3

:: find ready-to-use property paths and current values
python uecheat.py ..\dumps\PPSA23126\dump.bin --find Damage

:: raw memory read / write / watch (the writing instruction)
python peek.py read  0x2E4A1C080 --host 192.168.50.2
python peek.py write 0x2E4A1C080 100 --host 192.168.50.2 --type float
python peek.py trace 0x2E4A1C080 --host 192.168.50.2
```

How to use these while developing a cheat:

- **Confirm the struct offsets** — read the fields of a live `FPalItemRecipe` (via the model/row) and compare them to the layout.
- **Confirm the trigger** — read a field, perform the action in game, read again.
- **Confirm your hook runs** — temporarily patch a site to write a known value (or to `ret` early) and observe the UI; or use `peek.py trace` to see which instruction writes a watched value.
- **Do not** call functions that create objects from `uecall` (widgets, SpawnActor) — those crash. Use `ue4gt.py` for game-thread calls.

---

## 5. Case study: how Craft Anything was designed

### 5.1 What the player does, and what the game does

```text
open craft menu
  └─ WBP_IngameMenu_WorkSpace: FilteringRecipe()      builds the recipe list
select a recipe
  └─ OnClickedRecipeSlot()
       ├─ BP_FindRow(recipe id)                       ← returns a copy of FPalItemRecipe
       ├─ GetProductItemRequiredMaterialInfos(...)    ← utility TArray + TMap
       └─ builds MaterialDetails map, then the detail panel
set quantity
  └─ CalcMaxProductableNum()                          ← how many are possible
press Start Production
  └─ CanStartProduction()  → client gate → server request
       └─ server: validate materials, consume, start production
```

### 5.2 The seams (what was changed and why)

| # | Site (VA) | SDK / symbol | Patch | Effect |
|---|---|---|---|---|
| 1 | `0x301DF50` | `CalcMaxProductableNum` implementation | `mov eax,0x7FFFFFFF; ret` | Quantity is always selectable |
| 2 | `0x2FECC20` | Start-production client gate | `test al,al` + `nop nop` (kills `jne`) | Start request is sent even when the UI reports missing materials |
| 3 | `0x2D30F44` | Server requirement builder | `xor esi,esi` instead of `imul esi,r12d` (store unchanged) | Server-side required quantities = 0 |
| 4 | `0x2D310BD` | Missing-material entry gate | `je` → `nop nop` | A required material with zero owned count is accepted |
| 5 | `0x2D323B0` | Workability function | `jmp` to replacement that returns `RemainProductNum > 0` | Batch production stays active until the remaining count hits 0 |
| 6 | `0x2CD2E87` | Shared requirement-list builder | store `0` instead of the computed count | Slots are always white/available (never red) |
| 7 | `0x301E57D` | `GetRequiredMaterialInfos` worker (`0x301E540`) | skip builder call + zero out-array `Num` (**H1**) | Detail model's material array is empty |
| 8 | `0x25C5ED4` | Utility array tail (**H2**) | empty the caller's out array | Slot/detail arrays from the utility are empty |
| 9 | `0x25C5CBA` | Utility map tail (**H3**) | empty the caller's out map | Tooltip map is empty |
| 10 | `0x266383D` | `BP_FindRow` row funnel (**H4**) | zero `Material1..5` ids and counts on the returned row | **No material rows at all** (V3 fix included) |

"White icons" comes from #3/#4/#6 (required = 0 → availability checks pass). "No material rows" comes from #7–#10. Those are two different problems — that distinction is what created V2 → V3.

### 5.3 Hook #1 — the quantity limit

`CalcMaxProductableNum` is a small function; the entire body can be replaced by a constant return:

```asm
0x301DF50  push rbp                ; ← hook site (AOB starts here)
0x301DF51  mov  rbp, rsp
...
```

Patch:

```asm
mov eax, 0x7FFFFFFF
ret
```

Because the replacement returns immediately, the original prologue is skipped and no stack is set up — which is fine: `ret` goes straight back to the caller. This is the simplest and safest kind of patch: a **whole-function replacement with `ret`**.

### 5.4 Hooks #7–#9 — emptying the returned containers

#### H1 — the model worker (`0x301E540`, site `0x301E57D`)

The worker builds its out array by calling the shared builder:

```asm
0x301E55A  call 0x34CB550
...
0x301E574  mov  rcx, r14            ; r14 = TArray<FPalStaticItemIdAndNum>* (out)
0x301E577  mov  rdx, qword ptr [rsi]
0x301E57A  mov  rsi, rax
0x301E57D  call 0x2CD2D30           ; ← fills the caller's array
0x301E582  test r15d, r15d
0x301E585  jne  0x301E5C4
...
```

Patch: replace the 5-byte `call` with a jump to a cave that sets `Num = 0` and resumes at `0x301E582`:

```asm
; CraftV2ModelArrCave
mov dword ptr [r14+8], 0            ; TArray.Num = 0
jmp CraftV2ModelArrReturn           ; = 0x301E57D + 5 = 0x301E582
```

The original `call` is never executed, so nothing is built. `r14` is still the out-array pointer.

#### H2 — the utility array tail (`0x25C5ED4`)

Right after the utility built the array and freed its temporaries:

```asm
0x25C5ECF  call 0x2CD2D30
0x25C5ED4  mov  rdi, qword ptr [rbp-0x40]   ; ← hook site
0x25C5ED8  test rdi, rdi
0x25C5EDB  je   0x25C5EE2
0x25C5EDD  call 0x31BFAB0
0x25C5EE2  ...
```

Here the returned container is not `[rbp-0x40]` — that is only a local fallback. The caller's real out storage is reachable through the FFrame:

```asm
; CraftV2UtilArrCave
mov  rdx, qword ptr [rbx+0x38]      ; rbx = FFrame*, +0x38 = caller's out variable
lea  rcx, [rbp-0x40]
test rdx, rdx
cmovne rcx, rdx                     ; rcx = caller out (or local fallback)
mov  dword ptr [rcx+8], 0           ; Num = 0
mov  rdi, qword ptr [rbp-0x40]      ; re-do the replaced instructions
test rdi, rdi
jmp CraftV2UtilArrReturn            ; = site + 7 = the `je`
```

#### H3 — the utility map tail (`0x25C5CBA`)

Same idea, for the `TMap<FName,int32>`:

```asm
; CraftV2UtilMapCave
mov  rdx, qword ptr [rbx+0x38]
lea  rcx, [rbp-0x70]
test rdx, rdx
cmovne rcx, rdx
mov  dword ptr [rcx+8], 0           ; TMap.Num = 0
mov  dword ptr [rbp-0x28], 0        ; re-do the replaced instruction
jmp  CraftV2UtilMapReturn           ; = site + 7
```

### 5.5 H4 — the row funnel that caused the V2 bug (`0x266383D`)

`BP_FindRow` returns a **copy** of the recipe row. The copy is written to the caller's return struct (`rbx`) by vector moves:

```asm
0x26637C0  mov eax, dword ptr [rbp-0x48]
0x26637C3  mov dword ptr [rbx+0x68], eax        ; CraftExpRate
0x26637C6  vmovups ymm0, [rbp-0xA8]
0x26637CE  vmovups ymm1, [rbp-0x88]
0x26637D6  vmovups ymm2, [rbp-0x68]
0x26637DB  vmovups [rbx+0x48], ymm2             ; Material4/Material5/Energy...
0x26637E0  vmovups [rbx+0x28], ymm1             ; Material1_Count..Material3
0x26637E5  vmovups [rbx+0x08], ymm0             ; Product_Id/Product_Count/...
...
0x266383D  mov rax, qword ptr [r12]             ; ← hook site (stack cookie check)
0x2663841  cmp rax, qword ptr [rbp-0x28]
0x2663845  jne 0x2663857
0x2663847  add rsp, 0xA0
...
0x2663856  ret
```

At `0x266383D` the full row is already in `[rbx]`. Zero the material ids **and** counts, then re-execute the two replaced instructions and jump to the `jne`:

```asm
; CraftV2RowCave  (V3 — note the first movups)
pxor xmm0, xmm0
movups xmmword ptr [rbx+0x24], xmm0     ; Material1_Id (0x24) .. Material2_Id head
movups xmmword ptr [rbx+0x2c], xmm0     ; Material1_Count + Material2
movups xmmword ptr [rbx+0x3c], xmm0     ; Material3 + Material4 head
movups xmmword ptr [rbx+0x4c], xmm0     ; Material4_Count + Material5
mov    dword ptr [rbx+0x5c], 0          ; Material5_Count
mov    dword ptr [rbx+0x60], 0          ; EnergyType
mov    dword ptr [rbx+0x64], 0          ; EnergyAmount
mov    rax, qword ptr [r12]             ; re-do the replaced instructions
cmp    rax, qword ptr [rbp-0x28]
jmp    CraftV2RowReturn                 ; = site + 8 = 0x2663857 (the jne)
```

`Product_Id`, `Product_Count`, `WorkAmount`, `WorkableAttribute`, `UnlockItemID` and `CraftExpRate` are preserved — that is why the recipe still exists, still shows the correct product, and blueprints still work.

#### Why V2 showed exactly one material row

V2's cave started at `[rbx+0x2c]` — it zeroed `Material1_Count` but **not** `Material1_Id` (`0x24`). The detail panel builds a row for every material slot whose **id is not `None`**; counts only decide whether the icon is red. So V2 left exactly one real id (`Material1`), and the panel displayed exactly one row. V3 adds the `movups [rbx+0x24], xmm0` and the last row disappears.

**Lesson:** when a UI list still shows something, do not guess — walk the data source. Here the row copy from `BP_FindRow` was still feeding the panel through `OnClickedRecipeSlot`, even though the utility array/map were empty.

### 5.6 Why not hook the Blueprint graph itself?

Every Blueprint function in this build has the same `native` value: `0x30854e0` → `0x34854E0` (the script interpreter thunk). Hooking it would intercept *all* Blueprint execution. That is why the cheat hooks the **native helpers** the blueprint calls — they have unique code, unique patterns, and clear semantics.

---

## 6. From hook to patch: AOBs, caves and the ReapPatch format

### 6.1 What makes a good hook site

- **Instruction boundary** — never jump into the middle of an instruction.
- **Enough room** — a `jmp rel32` needs 5 bytes. For a 2-byte branch, `jne → nop nop` is easier than a cave.
- **Known state** — after parameter marshalling, before the result is consumed. Know which registers hold what (`rbx` = FFrame in the thunks, `r14` = out array, `rbx` = return struct in `BP_FindRow`).
- **Stack balanced** — if you skip a prologue, do not rely on its stack frame; if you jump into a cave, return to an instruction that expects the exact same state.
- **Local effect** — prefer a seam that only affects the menu/feature you are changing.

### 6.2 AOB rules (the pattern is the address)

An AOB (Array Of Bytes) is the byte pattern RS scans for at enable time. The patch never stores a VA, so it survives ASLR and (usually) minor rebuilds.

Rules used by every patch here:

1. **The pattern must be unique in the whole dump.** Verify by scanning `dump.bin`; if there are 2+ hits, extend it.
2. **Cover the bytes you replace** and add a few neighbours for uniqueness. The pattern may be **longer** than the write: e.g. `CraftV2ModelArrSource` finds with 10 bytes but only overwrites 5.
3. **Avoid relocation-dependent bytes** (relative call/jmp targets, RIP-relative displacements) *inside the part you actually write* — if the target moves, your cave still works, but a hardcoded relative target in the pattern would stop matching after an update.
4. **Mask** — one byte per pattern byte. In all patches here every byte is `01` (must match). RS/GoldHEN scanners also support wildcard bytes; the safest approach is exact bytes.
5. **Keep the pattern close to the site.** A pattern that starts before the site is fine; the write happens at the pattern start in this format, so put the pattern start exactly at your hook instruction.

Verified patterns (all unique in this dump):

| Label | VA | Bytes | Len |
|---|---|---|---|
| `CraftMaxProductable` | `0x301DF50` | `55 48 89 E5 41 57 41 56 41 55 41 54 53 48 83 EC 68 4C 8B 2D 18 81 2A 05` | 24 |
| `CraftClientRequestGate` | `0x2FECC20` | `84 C0 75 39 48 8D 7D C8 4C 89 F6 E8 E0 39 D4 FF` | 16 |
| `CraftZeroServerRequirements` | `0x2D30F44` | `41 0F AF F4 89 74 10 08 48 83 C2 0C` | 12 |
| `CraftMissingMaterialEntryGate` | `0x2D310BD` | `45 84 F6 44 8B B5 D4 FD FF FF 0F 84` | 12 |
| `CraftWorkableSource` | `0x2D323B0` | `55 48 89 E5 41 57 41 56 41 54 53 48 83 EC 20 4C 8B 25 BA 3C 59 05 48 89` | 24 |
| `CraftZeroMaterialCounts` | `0x2CD2E87` | `0F 4C FE 89 7C 10 08 48 83 C2 0C` | 11 |
| `CraftV2ModelArrSource` | `0x301E57D` | `E8 AE 47 CB FF 45 85 FF 75 3D` | 10 |
| `CraftV2UtilArrSource` | `0x25C5ED4` | `48 8B 7D C0 48 85 FF 74 05 E8 CE 9B BF 00` | 14 |
| `CraftV2UtilMapSource` | `0x25C5CBA` | `C7 45 D8 00 00 00 00 48 8B 7D D0 48 85 FF 74 05 E8 E1 9D BF 00 C7 45 98 00 00 00 00 83 7D 9C` | 31 |
| `CraftV2RowSource` | `0x266383D` | `49 8B 04 24 48 3B 45 D8 75 10 48 81 C4 A0 00 00 00 5B 41 5C 41 5E 41 5F 5D C3 E8 A4 7D F0 03 0F 0B` | 33 |

> AOB verification: load `dump.bin`, search the pattern, confirm exactly **one** hit and that it equals the expected VA. Do this after every game update — patterns are per-build.

### 6.3 The ReapPatch format

A `.ReapPatch` is a JSON list of **Variables**. Each variable is one of four building blocks. The fields:

| Field | Meaning |
|---|---|
| `label` | Unique name; also the symbol used inside assembly |
| `type` | Value type (see table below) |
| `defaultValue` | Code/values when the patch is **disabled** (or the cave body for caves) |
| `newValue` | Code/values when the patch is **enabled** |
| `offsetFinder` | `{sectionId, UniqueAOB, Mask, ptr}` — where to apply |
| `offsetFinderPtr` | Pointer-chain finder (not used in these patches) |
| `baseId`, `offset` | Computed-address label: `address(baseId) + offset` |
| `isAlwaysEnabled`, `isStatic` | Leave `false` as in the working patches |
| `editor` | UI editor hint for value variables, e.g. `{"editType":1,"varType":2}` |
| `comment`, `DisplayName` | Documentation shown in the app |

`type` values (0-based `VarType`): `0 Bool, 1 Int16, 2 Int32, 3 Int64, 4 UInt16, 5 UInt32, 6 UInt64, 7 Float, 8 Double, 9 String, 10 Aob, 11 ASM, 12 Dictionary`.

The four building blocks:

#### (a) Source variable — the hook

```json
{
  "label": "CraftV2RowSource",
  "type": 11,
  "defaultValue": "mov rax, qword ptr [r12]\r\ncmp rax, qword ptr [rbp-0x28]\r\n",
  "newValue": "jmp CraftV2RowCave\r\nnop\r\nnop\r\nnop\r\n",
  "offsetFinder": { "sectionId": 0, "UniqueAOB": "…", "Mask": "…", "ptr": 0 },
  "baseId": "0", "offset": 0
}
```

- `defaultValue` = the exact original instructions at the site (this is what disable writes back — and what RS assembles to verify the site).
- `newValue` = the replacement, assembled by RS at the found site address.
- The replacement must fit inside the pattern (here 8 write bytes inside a 33-byte finder).

#### (b) Cave variable — your own code

```json
{
  "label": "CraftV2RowCave",
  "type": 11,
  "defaultValue": "pxor xmm0, xmm0\r\nmovups xmmword ptr [rbx+0x24], xmm0\r\n...\r\njmp CraftV2RowReturn\r\n",
  "newValue": null,
  "offsetFinder": null,
  "baseId": "", "offset": 0
}
```

- No finder → RS allocates executable memory for it (`Alloc`).
- The code is assembled once and referenced by its `label`.
- Use only relative jumps to labels; avoid absolute data addresses (they are ASLR-dependent) — if you need one, use a type-6 label (see below) or a register-indirect load.

#### (c) Return/address label — `source + N`

```json
{
  "label": "CraftV2RowReturn",
  "type": 6,
  "defaultValue": 0,
  "newValue": null,
  "baseId": "CraftV2RowSource",
  "offset": 8
}
```

`type 6` (UInt64) + `baseId` = another variable's label + `offset` → RS computes `address(label) + offset`. Use it as the jump-back target. The offset is **how many bytes of the site your replacement consumed**:

| Source | Replacement | Offset |
|---|---|---|
| `CraftV2ModelArrSource` (`call`, 5 bytes) | `jmp cave` (5) | **5** |
| `CraftV2UtilArrSource` (`mov`+`test`, 7) | `jmp cave` + 2 nops (7) | **7** |
| `CraftV2UtilMapSource` (`mov`, 7) | `jmp cave` + 2 nops (7) | **7** |
| `CraftV2RowSource` (`mov`+`cmp`, 8) | `jmp cave` + 3 nops (8) | **8** |

If the offset is wrong, the cave returns into the middle of an instruction — instant crash. This is the #1 cause of "the game freezes when I open the menu".

#### (d) Toggle/value variable — optional

Used by other patches in the same project (e.g. Infinite Stamina / High Crit):

```json
{
  "label": "CritBonus",
  "type": 2,
  "defaultValue": 0,
  "newValue": 1,
  "editor": { "editType": 1, "varType": 2 },
  "offsetFinder": null,
  "baseId": "", "offset": 0
}
```

RS allocates storage for it. Code can read it directly:

```asm
cmp dword ptr [CritBonus], 1
jne original_code
```

This is how you make a cheat toggleable without a separate patch file.

### 6.4 What V3 contains (anatomy of the delivered patch)

V3 is V2 plus **one line** in the H4 cave: `movups xmmword ptr [rbx+0x24], xmm0`.

V3 variable list (20 variables):

| Group | Variables |
|---|---|
| Quantity | `CraftMaxProductable` |
| Client gate | `CraftClientRequestGate` |
| Server | `CraftZeroServerRequirements` |
| Missing-material gate | `CraftMissingMaterialEntryGate` |
| Workability | `CraftWorkableSource` → `CraftWorkableTarget` (function replacement, no return label) |
| Shared builder | `CraftZeroMaterialCounts` |
| H1 model array | `CraftV2ModelArrSource` + `Cave` + `Return` |
| H2 utility array | `CraftV2UtilArrSource` + `Cave` + `Return` |
| H3 utility map | `CraftV2UtilMapSource` + `Cave` + `Return` |
| H4 recipe row | `CraftV2RowSource` + `Cave` + `Return` |

Why `CraftWorkableTarget` has no return label: it is a **function replacement**. The source jumps to it, and it ends with `ret` — it never comes back to the hook site:

```asm
; CraftWorkableTarget
cmp dword ptr [rdi+0x29C], 0     ; RemainProductNum
setg al
ret
```

`rdi` is the original function's `this`; the prologue was skipped, so the stack is untouched and `ret` goes straight to the original caller.

### 6.5 Enable / disable semantics

- **Enable** — RS scans each `offsetFinder`, assembles each `newValue` at the found address, assembles each cave and resolves labels, then writes.
- **Disable** — RS assembles each `defaultValue` back at the same address. Your code cave is left allocated but unreachable — harmless.
- **Overlapping patches** — never enable two patches that touch the same site. The craft patches overlap by design (V1/V2/V3 are alternatives); enable **one** at a time.

---

## 7. Testing and troubleshooting

### 7.1 The correct test procedure

1. Enable **only one** craft patch.
2. **Restart the game** (or at least fully close and reopen the craft menu) — the UI caches recipe/material data built before your patch.
3. Open the craft menu, select a recipe, check:
   - quantity can be set without materials,
   - recipe slots stay white,
   - Start Production works,
   - the detail panel shows no material rows,
   - the item keeps producing until the count ends.
4. Disable, restart, confirm the game is back to normal.

### 7.2 Symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| Game freezes the moment the menu opens | Return offset wrong — cave jumps into the middle of an instruction | Offset = bytes consumed by the replacement (5/7/8 as listed) |
| Crash / freeze only when clicking a recipe | Cave clobbered a register used after the site | Save/restore or use only caller-saved regs; check the disassembly around the site |
| Nothing changes at all | AOB not found (wrong build / typo) or the site is not on the path you think | Re-scan the pattern in the dump; verify unique hit; check the call path |
| Rows still show, but icons are white | The UI is reading the **row struct**, not the emptied arrays | Hook the row funnel (H4) and zero the ids |
| Exactly **one** material row remains | `Material1_Id` at `0x24` not cleared (the V2 bug) | Zero `[row+0x24]` as in V3 |
| Icons red / recipe grayed out | Required counts not zeroed (or got re-filled later) | Zero counts in the shared builder and in the row copy |
| Start button does nothing | Client gate still branches away | Neuter the `jne` (`nop nop`) |
| Production stops after one item | Workability returns false when RemainProductNum hits 0 | Workable replacement with `> 0` check |
| Everything breaks after a game update | AOBs changed | Re-dump, re-find sites, rebuild patterns |

### 7.3 Debugging a patch that "does nothing"

1. **Verify the pattern**: does it hit exactly once in the current dump? Is the VA the same as when you built it?
2. **Verify the default bytes**: assemble `defaultValue` at the site and compare with the dump. If it differs, the site moved or the text has a typo.
3. **Verify the replacement encoding**: it must fit; `jmp` to a cave label must be encodable (always is, rel32).
4. **Verify the call path**: does the function actually run when you do the action? Add a temporary "marker" patch (e.g. write a distinctive value to a known heap field) and watch it with `peek.py`/`uelive.py`.
5. **Bisect variables**: enable the patch, then remove/disable one variable group at a time. In RS each variable is individually toggleable — use that.

---

## 8. Doing it again for another cheat or game

The full checklist:

```text
[ ] Dump + SDK (ps4ue.py)                      → dump.bin, SDK headers, GObjects
[ ] Find the feature by name in the SDK        → classes/structs/parameters
[ ] Map the data flow from BP CallFunc_ lists  → which native functions feed it
[ ] Get the native entry, disassemble the thunk→ find the worker
[ ] Read the struct layout                     → which offsets matter
[ ] Verify live with uelive/uecall/peek        → confirm the field and trigger
[ ] Choose the seam                           → branch / store / call / return / row copy
[ ] Find a stable instruction boundary         → AOB candidate
[ ] Verify AOB uniqueness in the dump          → exactly one hit
[ ] Write the cave + replacement               → registers, stack, return offset
[ ] Build the ReapPatch (source/cave/return)   → RS
[ ] Test: one patch, restart, verify, disable  → iterate
```

Where to look for common cheats:

| Cheat | Likely seam |
|---|---|
| Infinite HP / damage | HP setter, damage calculation, `TakeDamage` path |
| Infinite stamina / hunger | consumption store in the tick/update function |
| Craft / build anything | requirement builders, max-productable, row copies, validation gates |
| XP / capture rate | multiplier store before the add, or the return of the rate getter |
| Item duplication | inventory add/remove/transfer functions |

The pattern is always the same: **find the data, find the code that writes it, pick the smallest write that produces the behaviour.**

---

## Appendix A — `FPalItemRecipe` layout

From `Pal_structs.hpp` (size `0x88`):

| Offset | Field | Type |
|---|---|---|
| `0x08` | `Product_Id` | `FName` (8) |
| `0x10` | `Product_Count` | `int32` |
| `0x14` | `WorkAmount` | `float` |
| `0x18` | `WorkableAttribute` | enum/byte |
| `0x1C` | `UnlockItemID` | `FName` (8) |
| `0x24` | `Material1_Id` | `FName` (8) |
| `0x2C` | `Material1_Count` | `int32` |
| `0x30` | `Material2_Id` | `FName` (8) |
| `0x38` | `Material2_Count` | `int32` |
| `0x3C` | `Material3_Id` | `FName` (8) |
| `0x44` | `Material3_Count` | `int32` |
| `0x48` | `Material4_Id` | `FName` (8) |
| `0x50` | `Material4_Count` | `int32` |
| `0x54` | `Material5_Id` | `FName` (8) |
| `0x5C` | `Material5_Count` | `int32` |
| `0x60` | `EnergyType` | enum/byte |
| `0x64` | `EnergyAmount` | `int32` |
| `0x68` | `CraftExpRate` | `float` |
| `0x70` | `DenyRecipeChain` | array/struct |
| `0x80` | `Editor_RowNameHash` | `uint32` |

Key insight: `Material1_Id` is at `0x24`, **not** `0x2c`. `0x2c` is `Material1_Count`. V2 zeroed from `0x2c`, which left the id — and the panel renders rows by id.

## Appendix B — Hook reference (this build)

| Label | VA | AOB len | Written len | Cave? | Purpose |
|---|---|---|---|---|---|
| `CraftMaxProductable` | `0x301DF50` | 24 | 6 | no | `mov eax,0x7FFFFFFF; ret` |
| `CraftClientRequestGate` | `0x2FECC20` | 16 | 4 | no | kill `jne` |
| `CraftZeroServerRequirements` | `0x2D30F44` | 12 | 12 | no | store 0 as required count |
| `CraftMissingMaterialEntryGate` | `0x2D310BD` | 12 | 12 | no | kill `je` |
| `CraftWorkableSource` | `0x2D323B0` | 24 | 5 | target | replace function; `ret` in target |
| `CraftZeroMaterialCounts` | `0x2CD2E87` | 11 | 11 | no | store 0 in shared builder |
| `CraftV2ModelArrSource` | `0x301E57D` | 10 | 5 | H1 | skip builder, `Num=0` |
| `CraftV2UtilArrSource` | `0x25C5ED4` | 14 | 7 | H2 | empty out array |
| `CraftV2UtilMapSource` | `0x25C5CBA` | 31 | 7 | H3 | empty out map |
| `CraftV2RowSource` | `0x266383D` | 33 | 8 | H4 | zero row materials |

## Appendix C — Command cheat sheet

```bat
:: dump + SDK (console must be running the game)
python ps4ue.py 192.168.50.2

:: rebuild SDK from an existing dump
python ps4ue.py ..\dumps\PPSA23126\dump.bin

:: dump only
python ps4dump.py --host 192.168.50.2 -o ..\dumps\PPSA23126\dump

:: SDK only
python uegen.py ..\dumps\PPSA23126\dump.bin ..\dumps\PPSA23126\SDK

:: live: list / read / write properties
python uelive.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 <Class> [Property [Value]]

:: live: call a UFunction, read out params and return
python uecall.py ..\dumps\PPSA23126\dump.bin 192.168.50.2 <Class> <Function> [obj=0xADDR] [Param=Value ...]

:: live: find property paths and current values
python uecheat.py ..\dumps\PPSA23126\dump.bin --find Damage

:: offline: find code that touches a struct offset
python uediff.py --xref ..\dumps\PPSA23126\dump.bin --disp 0x24 --write --mnem mov,movups,vmovups

:: raw memory
python peek.py read|write|freeze|watch|trace 0xADDR [value] --host 192.168.50.2 [--type float]
```

## Appendix D — Glossary

| Term | Meaning |
|---|---|
| **VA** | Virtual address in the running process |
| **Native / module offset** | Address relative to the module base (`0x400000` here) — `VA − 0x400000` |
| **UObject / UClass / UFunction** | Unreal's reflection objects: instance, class, function description |
| **CDO** | Class Default Object — the template instance of a class |
| **GObjects** | Global array of all UObject instances |
| **FPalItemRecipe** | The recipe row struct (`0x88` bytes) |
| **FFrame** | The interpreter frame used when a BP calls a native function; out-param destination lives at `+0x38` |
| **Exec thunk** | Small native wrapper that reads BP parameters and calls the real C++ function |
| **AOB** | Array of bytes — the unique pattern used to locate code |
| **Mask** | Per-byte match mask for the AOB scan |
| **Cave** | Extra code block allocated in executable memory, reached by a `jmp` from the hook |
| **Return label** | `type 6` variable = `address(source) + offset`, used to jump back |
| **Num** | Element count at `+8` in `TArray`/`TMap`; zeroing it empties the container |

---

### Final notes for distribution

- Ship **V3 only** for the clean UI; keep V1/V2 as documentation of the iterations.
- One craft patch at a time — overlapping sites.
- Always tell users to **restart the game** after enabling, or at least reopen the craft menu.
- Include the game version in the patch name/description (`PPSA23126 01.102.999`); a game update invalidates the AOBs even though the concept stays the same.

*Tools: ps4-unreal-dumper (UEDumper/UELive) by TetzkatLipHoka; Reaper Studio patch format by Team Reaper.*
