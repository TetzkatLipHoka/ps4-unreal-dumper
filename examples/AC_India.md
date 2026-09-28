# Example: cheats without a PRX, Assassin's Creed Chronicles India (CUSA03440, UE3)

From "game is running on the console" to "God mode on, all trophies of the episode unlocked", using only the
Python tools and ps4debug-ng. No PRX, no compiler. The pattern is the same for every UE3 game, only class
and function names change.

## 0. Requirements

- Console with GoldHEN + ps4debug-ng, game started, level loaded (pause menu is fine).
- PC: Python 3.12 with `pip install ps4debug`, working directory `tools/`.
- Heap addresses from a dump are valid for **this** game start only. After a restart: dump again.

## 1. Dump + SDK

```bash
python ps4ue.py 192.168.x.y
```

Writes `dumps/<TitleID>/` with `dump.bin`, `dump.json`, `SDK/` (GObjects.txt, SDK_HEADERS/*.hpp,
GameDefines.hpp, offsets.json, ReClass.rcnet). About 5 minutes for 3.7 GB. The log ends with what was found:

```
GNames base+0x2b7d140  GObjects base+0x2bfd6d0  objects 149536
UObject: index 0x38 name 0x48 class 0x50 outer 0x40
GWorld base+0x2c412a8
ProcessEvent vtable index 64
9 packages, 576 enums, 1154 structs, 2861 classes
```

Collections sharing one title ID (the three AC Chronicles episodes) always land in `CUSA03440`, so rename the
folder before the next dump (`CUSA03440_China`, `CUSA03440_India`).

## 2. Browse the SDK

Interesting things are functions with the `exec` flag (0x200) in a CheatManager, or anything with a telling name:

```bash
grep -n -i "godmode\|unlockachievement\|unlockall" dumps/CUSA03440_India/SDK/SDK_HEADERS/ACCGame_classes.hpp
grep -n "^class .*Cheat" dumps/CUSA03440_India/SDK/SDK_HEADERS/*_classes.hpp
```

Functions are listed as comments inside the class, with flags and native address:

```
// void God();                              // flags 0x00020202  native 0x376a0   <- script (0x376a0 = generic script stub)
// bool UnlockAchievement(int32_t Id);      // flags 0x00020400  native 0x1c6c470 <- real native (own address)
```

Enums (e.g. `EeACCAchievementName`) are in `*_structs.hpp`. Property offsets follow every field
(`bGodMode : 1; // 0x0280 ... [0x02]` = byte at +0x280, bit 0x02).

## 3. Find live instances

`GObjects.txt` lists every object with its address. `Default__*` are class default objects, usually not wanted:

```bash
grep " ACCCheatManager \| ACCUbiService \| ACCGameInfo \| ACCPlayerController " dumps/CUSA03440_India/SDK/GObjects.txt | grep -v Default__
```

```
[0001FED4] {0x202b587a0} ACCPlayerController E2_L01_P.TheWorld.PersistentLevel.ACCPlayerController_1
[0001FED8] {0x206901ce0} ACCCheatManager    E2_L01_P.TheWorld.PersistentLevel.ACCPlayerController_1.ACCCheatManager_1
[000178D1] {0x205003400} ACCUbiService      Transient.ACCGameEngine_0.ACCUbiService_1
[0001FC9E] {0x203236ca0} ACCGameInfo        E2_L01_P.TheWorld.PersistentLevel.ACCGameInfo_1
```

Here the CheatManager already exists (dev cheats compiled in). Dishonored needs `PlayerController.CheatClass`
set plus `AddCheats` first, see the README.

## 4. Call a script function on the game thread: `ue3gt.py`

`ue3gt` replaces the bytecode of `HUD.PostRender` (runs every frame on the game thread) for half a second with
`Context(obj) VirtualFunction Name(<constants>); Return` and restores the original afterwards. This is the safe
path for anything that creates objects or runs script. Natives work too, the VM dispatches them normally.

```bash
python ue3gt.py ../dumps/CUSA03440_India/dump.bin 192.168.x.y 0x206901ce0 God
```

```
carrier ACCHUD E2_L01_P.TheWorld.PersistentLevel.ACCHUD_1 :: PostRender script @ 0x204847760 -> God() on 0x206901ce0
restored: True
```

Instead of the address you can pass `Class` or `Class.Property` (e.g. `PlayerController.CheatManager`), the
instance is then taken from the dump.

Check without looking at the game: `bGodMode` is at `AController + 0x280`, bit 0x02:

```python
import asyncio, ps4debug
async def main():
    dbg = ps4debug.PS4Debug("192.168.x.y")
    pid = next(p.pid for p in await dbg.get_processes() if p.name.rstrip("\0") == "eboot.bin")
    b = (await dbg.read_memory(pid, 0x202b587a0 + 0x280, 1))[0]
    print("bGodMode:", bool(b & 0x02))
asyncio.run(main())
```

## 5. Parameters

Parameters are named as in the SDK declaration and embedded as bytecode constants (int, float, byte, bool,
object, name, string; no structs):

```bash
python ue3gt.py ../dumps/CUSA03440_India/dump.bin 192.168.x.y 0x205003400 UnlockAchievement Id=0
```

Trap: for script functions the generator also lists **local variables** as parameters
(`UnlockAchievement(EeACCAchievementName AchievementName, UACCAchievement* Achievement)`, `Achievement` is a
local). Just leave them out, the VM zero-fills the rest.

## 6. The trophy path (what did not work and why)

- `ACCUbiService.UnlockAchievement(Id)` runs but only sets `ACCUPlaySaveData.AchievementUnlocked[55]`. No trophy.
- The real path is `ACCAchievementManager.UnlockAchievement(AchievementName)` (script). It calls
  `OnlinePlayerInterface.UnlockAchievement(...)` in the NP subsystem, which is the PSN trophy.
- The manager is not a separate entry in `GObjects.txt`, it hangs off GameInfo at `+0x670`. An 8-byte read
  gives the address (here 0x20325e1c0).
- The manager only knows the achievements of the **running episode** (`AchievementsList` at `+0x60`, per entry
  `AchievementName` +0x60, `Id` +0x64, `bUnlocked` +0x70). Foreign names resolve to None and nothing happens.
  India = names 19-36 plus collection achievement 54; China 0-18; Russia 37-53 (enum `EeACCAchievementName`).

```bash
python ue3gt.py ../dumps/CUSA03440_India/dump.bin 192.168.x.y 0x20325e1c0 UnlockAchievement AchievementName=19
for n in $(seq 20 36) 54; do
  python ue3gt.py ../dumps/CUSA03440_India/dump.bin 192.168.x.y 0x20325e1c0 UnlockAchievement AchievementName=$n
  sleep 1
done
```

About 1 s per call (carrier 0.5 s + opening the dump from cache). The trophies popped on the console.

## 7. Which tool for what

| Goal | Tool |
|---|---|
| read/write a property on all instances of a class | `uelive.py <dump> <ip> Class Property [Value]` |
| script/native function with side effects (object creation, script) | `ue3gt.py` (game thread) |
| pure computation, UE4/UE5 | `uecall.py` (ProcessEvent on the RPC thread) |
| permanent, with menu, code patches | a PRX (separate project) |

Do not call UE3 natives with their own address through `uecall`/ProcessEvent: their exec stubs read parameters
from the bytecode stream and crash without a script frame. Use `ue3gt` (bytecode) instead.
