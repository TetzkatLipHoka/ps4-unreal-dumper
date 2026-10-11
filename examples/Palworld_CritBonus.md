# Example: lock crit-catch rate without a PRX, Palworld (PPSA23126, UE4, PS5)

From "game is running on the console" to "every critical catch shows ~62%", using only the
Python tools and ps4debug. No PRX, no compiler. Ends with a Reaper patch built from the same
finding.

## 0. Requirements

- PS5 + ps4debug running, Palworld started, world loaded.
- PC: Python 3.12 with `pip install ps4debug`, working directory `tools/`.
- Heap addresses (like `0x1a410df4a0`) are valid for **this** game start only. After a restart
  or crash: the values reset and old addresses are dead. Offsets (like `+0x1574`) stay valid.

## 1. Dump + SDK

```bash
python ps4ue.py 192.168.50.2
```

Writes `dumps/PPSA23126/` with `dump.bin`, `dump.json`, `SDK/` (GObjects.txt, SDK_HEADERS/*.hpp,
GameDefines.hpp, offsets.json). About 5.7 GB. The log ends with what was found:

```
GNames base+0x8226f00  GObjects base+0x8262c98  objects 270097
GWorld base+0x83d5c90
ProcessEvent vtable index 77 at base+0x3086340
```

The live tools (`uelive`, `uecheat`, `peek`) only need `dump.bin` plus `SDK/offsets.json`.

## 2. Browse the SDK for the catch logic

Search the native classes for anything about capture level and rate:

```bash
grep -n "CaptureLevel\|CalcCaptureRate\|CaptureSphere" dumps/PPSA23126/SDK/SDK_HEADERS/Pal_classes.hpp
```

```
TMap<EPalCaptureSphereLevelType, int32>  CaptureSphereLevelMap;   // 0x14c8
float  AddCaptureLevelPerStatusPoint;                             // 0x1d1c
int32_t  CriticalCaptureBonus;                                    // 0x1574
// int32_t GetCaptureLevelBySphereType(EPalCaptureSphereLevelType SphereType);  // native
// float CalcCaptureRate(captureItemLevel, targetHandle, throwHandle, bIsSneakBonus);
```

Thrown balls carry their level with them. The Blueprints show where:

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalCaptureBodyBase_C
```

```
+0x02d0 IntProperty   CaptureLevel      <- the ball's level (body family)
+0x0320 BoolProperty  IsCriticalCapture <- crit flag
```

(The flying ball uses `+0x438` instead — same name, different family. You don't need either yet.)

## 3. The trick: skip the ball, edit the permanent object

A thrown ball lives ~2 seconds. A full dump takes ~a minute. So forget the ball.

The settings object `BP_PalGameSetting_C` lives forever under the GameInstance. One write
there applies to every future throw. And `uelive` can only write simple numbers
(Int/Float/Bool) — Arrays and Maps are read-only to it. So pick simple-number candidates:

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C | grep -i "capture\|sphere\|sneak"
```

```
+0x14c8 MapProperty   CaptureSphereLevelMap    <- per-ball levels (Map: NOT writable, skip)
+0x1574 IntProperty   CriticalCaptureBonus     <- crit bonus (writable!)
+0x1d1c FloatProperty AddCaptureLevelPerStatusPoint  <- writable, try it
```

## 4. Test live (OTF): read, write, throw, observe

Read the current value (also proves the object is alive):

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C AddCaptureLevelPerStatusPoint
```

```
1 instance(s) in dump
  0x1a410df4a0 ...BP_PalGameSetting_C_2147481308: 0.5
```

Write it huge, throw a cheap ball, watch the rate:

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C AddCaptureLevelPerStatusPoint 100000
```

```
-> written, readback 100000.0
```

Result here: **no effect** — rate stayed normal per ball. Wrong knob. Revert it (clean desk):

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C AddCaptureLevelPerStatusPoint 0.5
```

Next candidate:

```bash
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C CriticalCaptureBonus
# -> 2
python uelive.py dump.bin 192.168.50.2 BP_PalGameSetting_C CriticalCaptureBonus 100000
# -> written, readback 100000
```

Result: **critical catches locked at 61.9–62%**. Right knob. Revert to normal (`2`)
so you can test the real cheat next.

Rule of thumb: one write, one throw, one observation. Log what worked and what didn't —
the revert keeps the game clean for the next test.

## 5. Make it permanent: the TLH trainer file

`uecheat.py` builds a `.TLH` trainer: an injection on `ProcessEvent` (runs thousands of
times per frame) that re-writes your value every frame, plus editable Value cells.
Problem: stock `uecheat` only walks **from the player pawn**, and nothing on the pawn
points back to the settings object (dead end: `Pawn -> Controller -> Player` has no
GameInstance link). The settings object hangs off the other direction:

```
GWorld -> World.OwningGameInstance (+0x1b8) -> GameSetting (+0x498) -> CriticalCaptureBonus (+0x1574)
```

So the generator needs World-rooted values (a ~15-line addition, same hop logic).
The `:PalGameInstance` downcast is needed because `OwningGameInstance` is typed as the
base `GameInstance`, which doesn't know `GameSetting`:

```bash
python uecheat_world.py dump.bin crit.TLH "CritBonus=World.OwningGameInstance:PalGameInstance.GameSetting.CriticalCaptureBonus=100000"
```

```
CritBonus: world...CriticalCaptureBonus (IntProperty +0x1574) = 100000
wrote crit.TLH: hook at base+0x3086340 (AOB 32 bytes), GWorld base+0x83d5c90, 1 cheats
```

Inside the TLH is the pointer resolve plus the write logic — the same two things any
GoldHEN/Reaper cheat needs:

```
mov rcx, [GWorld]
mov rcx, [rcx+0x1b8]      ; OwningGameInstance
mov rcx, [rcx+0x498]      ; GameSetting
mov edx, [Val1]
mov [rcx+0x1574], edx     ; CriticalCaptureBonus = 100000
```

## 6. Convert the TLH to a ReapPatch

A ReapPatch is JSON: a Source (original bytes + AOB finder, replaced by `jmp Target`),
a Target (your code), an Original (original bytes + jump back), a Return address var,
and optional toggle vars (same style as e.g. `Infinite_HP`).

Two traps that crashed the game once (learned the hard way):

1. **No `mov rcx,[0x87D5C90]`.** In 64-bit that encodes RIP-relative, not absolute —
   it reads a garbage pointer and writes to garbage. Use an immediate plus indirect:
   `mov rax,0x87D5C90` then `mov rax,[rax]`.
2. **No pushes, no local labels.** At a function-entry hook, `RAX` is free scratch
   (callers can't rely on it), so a pure RAX walk needs no stack and no labels.
   Null checks jump straight back to Original — safe, because the body overwrites RAX.

The working Target:

```
cmp dword ptr [CritBonus],1
jne CritOriginal
mov rax,0x87D5C90
mov rax,qword ptr [rax]          ; GWorld (eboot base is fixed 0x400000, so this address never changes)
test rax,rax
jz CritOriginal
mov rax,qword ptr [rax+0x1B8]    ; OwningGameInstance
test rax,rax
jz CritOriginal
mov rax,qword ptr [rax+0x498]    ; GameSetting
test rax,rax
jz CritOriginal
mov dword ptr [rax+0x1574],100000
jmp CritOriginal
```

It hooks the same ProcessEvent-entry AOB as the TLH, so it runs every frame just like
the trainer did. No heap addresses anywhere — reboot-proof.

## 7. Which tool for what

| Goal | Tool |
|---|---|
| list a class's properties (works offline) | `uelive.py <dump> <ip> Class` |
| read/write Int/Float/Bool live (OTF test) | `uelive.py <dump> <ip> Class Property [Value]` |
| raw memory, freeze, watch, find the writing instruction | `peek.py read\|write\|freeze\|watch\|trace ADDR --host IP` |
| trainer file with pointer resolves + write logic | `uecheat.py` (pawn paths) / World-rooted copy for settings |
| permanent toggleable patch | ReapPatch (this chapter) / GoldHEN cheat |
| function that creates objects (spawn, widgets) | `ue4gt.py` (game thread), never plain RPC |
