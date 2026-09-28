# Example: open the built-in debug menu without a PRX, Days Gone (CUSA09175, UE 4.11)

From "game is running" to "Bend's own cheat menu is open on the PS4", using only the Python tools and
ps4debug-ng. No PRX, no compiler. Counterpart to `AC_India.md` (UE3): same pattern, UE4 bytecode and a
different carrier.

## 0. Requirements

- Console with GoldHEN + ps4debug-ng, Days Gone started, **world loaded and not paused** (the pause menu stops
  Blueprint events, step 5 would then do nothing).
- PC: Python 3.12 with `pip install ps4debug`, working directory `tools/`.
- Heap addresses are valid for **this** game start only. After a restart or crash: dump again.

## 1. Dump + SDK

```bash
python ps4ue.py 192.168.x.y
```

Days Gone is big: 5-6 GB file (16 GB mapped, mostly mirrors and zero reservations), about 10 minutes to dump,
a few more minutes for the SDK. The log shows what was found:

```
names: NameArray                                   <- TNameEntryArray (UE4 < 4.23)
GObjects 0x67b8a98 (base+0x63b8a98)  objects 445622  item size 16     <- flat FFixedUObjectArray (4.8-4.19)
UObject: flags 0x8 index 0xc class 0x10 name 0x18 outer 0x20
UFunction::FunctionFlags 0x88 ExecFunction 0xb0
GWorld 0x68fb5d0 (base+0x64fb5d0)
ProcessEvent vtable index 61 at base+0x2469540
2462 packages, 1034 enums, 1848 structs, 5917 classes
```

The live tools (`uelive`, `uecall`, `ue4gt`) only need `dump.bin` plus `SDK/offsets.json`.

## 2. Browse the SDK

```bash
grep -h -o "^class [A-Za-z_0-9]* : public [A-Za-z_0-9]*" ../dumps/CUSA09175/SDK/SDK_HEADERS/*_classes.hpp | grep -i "debug\|cheat"
```

```
class AGameCheatMenu : public AActor          // ToggleCheatMenu(), HideCheatMenu(), DebugMenus[], GameDebugOptions[]
class AGameCheatMenuBP_C : public AGameCheatMenu
class AGameDebugMenu : public AActor          // AddWeaponToInventory(EInventoryWeaponID), DebugFunctionNames[]
class AWorldDebugMenu : public AGameDebugMenu // MapRenderSectors, ZoomToLocation, SetOrthographic
class UBendCheatManager : public UCheatManager
```

Who owns the instance? Grep for the type:

```bash
grep -n "AGameCheatMenu\*" ../dumps/CUSA09175/SDK/SDK_HEADERS/BendGame_classes.hpp
```

```
// class AGameCheatMenu* GetGameCheatMenu();  // flags 0x54020401  native 0x1d645c0    <- in ABendGameMode
```

The stock UE4 cheats (`God`, `Ghost`, `Fly`, `Walk`, `Slomo`, `Teleport`, `ToggleDebugCamera`, ...) are in
`Engine_classes.hpp` under `UCheatManager`.

## 3. Find live instances

In this release build the CheatManager already exists (UE4 only creates it when cheats are allowed):

```bash
python uelive.py ../dumps/CUSA09175/dump.bin 192.168.x.y BendPlayerController CheatManager
python uelive.py ../dumps/CUSA09175/dump.bin 192.168.x.y BendGameMode NumPlayers
```

```
  0x24f3b2b00 ...PersistentLevel.BendDefaultPlayerController_C_0: 9913453440    <- CheatManager = 0x24ee34b80
  0x60b0f4500 ...PersistentLevel.BendDefaultGameMode_C_0: 1                     <- GameMode address
```

(`uelive` prints pointers in decimal; `python -c "print(hex(9913453440))"`.)

## 4. Simple cheats through ProcessEvent: `uecall.py`

`uecall` calls `obj->ProcessEvent(Function, Params)` on the ps4debug RPC thread. Enough for everything that
**does not create objects**:

```bash
python uecall.py ../dumps/CUSA09175/dump.bin 192.168.x.y CheatManager God obj=0x24ee34b80
python uelive.py ../dumps/CUSA09175/dump.bin 192.168.x.y BendPlayerPawn bCanBeDamaged
```

```
  0x29a724000 ...PersistentLevel.BendDefaultPlayerPawn_C_0: False      <- God on
```

Same for fetching the menu instance (a pure getter):

```bash
python uecall.py ../dumps/CUSA09175/dump.bin 192.168.x.y BendGameMode GetGameCheatMenu obj=0x60b0f4500
```

```
  ReturnValue              ObjectProperty   = 9921051648        <- 0x24f573c00
```

**Not** through `uecall`: `ToggleCheatMenu`. It creates widgets, and object creation outside the game thread
crashes the game (measured: `SYSTEM_WRITE_ADDRESS_WRAPAROUND` in the allocator, thread `rpcstub`). Step 5 is for that.

## 5. Call a function on the game thread: `ue4gt.py`

`ue4gt` is the UE4 counterpart of `ue3gt`. For half a second it replaces the start of a Blueprint Ubergraph that
runs every frame with

```
JumpIfNot(flag) -> T ; Return           (already ran -> do nothing)
T: flag = True
Context(obj) FinalFunction Target(Args) ; Return
```

and restores the original afterwards. `flag` is a bool local of the Ubergraph on the persistent frame, so the
call runs **exactly once** and not 15 times per half second (matters for toggles).

The carrier (`carrier=Class.Ubergraph`) must run every frame and should have a single instance. In Days Gone
the PlayerController Blueprint fits: its axis event `WepaonFireAxis` fires every frame even without input.
The storm manager has a `ReceiveTick` but only ticks during a storm (`executed: False`).

Proof with something visible first:

```bash
python ue4gt.py ../dumps/CUSA09175/dump.bin 192.168.x.y 0x24ee34b80 Slomo T=0.3 carrier=BendDefaultPlayerController_C.ExecuteUbergraph_BendDefaultPlayerController
```

```
carrier BendDefaultPlayerController_C ...BendDefaultPlayerController_C_0 :: ExecuteUbergraph_BendDefaultPlayerController script @ 0x202091f60 (58604 bytes), once-flag UniqueObjectNameForCooking_InRange_ @ 0x24e94bf10 -> Slomo(T=0.3) on 0x24ee34b80
patched: True  restored: True  executed: True
```

Deacon moves in slow motion; `uelive ... WorldSettings TimeDilation` shows 0.3. Back with `Slomo T=1`. Then the menu:

```bash
python ue4gt.py ../dumps/CUSA09175/dump.bin 192.168.x.y 0x24f573c00 ToggleCheatMenu carrier=BendDefaultPlayerController_C.ExecuteUbergraph_BendDefaultPlayerController
```

Bend's cheat menu is open. Calling again closes it (toggle), `HideCheatMenu` closes it explicitly.

`executed: False` means the carrier did not run within the half second, almost always because the game is
paused or in a menu. The bytecode is restored regardless (`restored: True`).

## 6. What ue4gt assumes (measured on 4.11, not guaranteed for other versions)

- `EX_Context` = expression, `u32` skip, 8-byte RValue pointer, no extra byte (UE3 has `u16` skip + one byte).
- Natives are called as `EX_FinalFunction` with a UFunction pointer.
- Constants as in UE3: `FloatConst 0x1E`, `IntConst 0x1D`, `ByteConst 0x24`, `True/False 0x27/0x28`,
  `ObjectConst 0x20`. Strings are not supported.
- The Ubergraph starts with `EX_ComputedJump 0x4C`; during the 0.5 s all events of this Blueprint are swallowed
  (controller input for a blink). Afterwards everything is as before.

## 7. Which tool for what

| Goal | Tool |
|---|---|
| read/write a property | `uelive.py <dump> <ip> Class Property [Value]` |
| function without object creation (God, getters, math) | `uecall.py` (ProcessEvent on the RPC thread) |
| function with side effects (widgets, spawn, script), UE4 | `ue4gt.py` (game thread through an Ubergraph) |
| same, UE3 | `ue3gt.py` (game thread through HUD.PostRender) |
| permanent, with menu, code patches | a PRX (separate project) |
