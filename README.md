# ps4-unreal-dumper

Universal Unreal Engine dumper for PS4 games (UE3, UE4, UE5). Runs on the PC, reads the game's memory
through ps4debug (GoldHEN) once, then works offline on the dump file. There are no per-game profiles:
every engine offset is measured from the dump.

Tested: Jedi Fallen Order (UE 4.21), Hogwarts Legacy (UE 4.27 custom), The Quarry (UE 4.27),
Lollipop Chainsaw RePop (UE 5.2), Days Gone (UE 4.11), SAO Last Recollection (UE4), FF7 Remake (UE 4.18),
Fatal Bullet (UE 4.15), WWE 2K Battlegrounds (UE4), Dishonored Definitive Edition (UE3), Thief (UE3),
Borderlands 2 / Pre-Sequel (UE3 Gearbox), Mass Effect Legendary Edition 1-3 (UE3 with BioWare name pool,
detected automatically), Tiny Tina's Wonderlands (UE 4.20), High on Life (UE5), Duskfade (PS5, UE 5.1+).

## Requirements

- Python 3.12, `pip install ps4debug` (PyPI). Optional `capstone` for code analysis, `numpy` for `uediff`.
- PS4 with GoldHEN and ps4debug (or ps4debug-ng) running, reachable over the network.
- The game must have a level loaded. **The pause menu is fine**: a dump taken in the pause menu yields the
  same offsets, classes and objects as one taken in the running scene. Only the main menu lacks level
  objects and player characters.

## Quick start

```
cd tools
python ps4ue.py 192.168.x.y                    # dump + SDK + ReClass project into ../dumps/<TitleID>/
python ps4ue.py ../dumps/CUSA31820/dump.bin    # SDK + ReClass from an existing dump
```

Output in `dumps/<TitleID>/`:

| File | Content |
|---|---|
| `dump.bin`, `dump.json` | memory image (zero blocks omitted) and region index |
| `dump.bin.engine.json` | cache of the finder results; every later run takes seconds instead of minutes |
| `SDK/GameDefines.hpp` | GNames, GObjects, GWorld, ProcessEvent index and address, all reflection offsets |
| `SDK/SDK_HEADERS/*_classes.hpp`, `*_structs.hpp`, `*_parameters.hpp` | SDK in CodeRed layout, one set per package |
| `SDK/offsets.json` | the same values machine-readable, used by the live tools |
| `SDK/GObjects.txt` | every object with address and full name |
| `SDK/ReClass.rcnet` | ReClass.NET project (needs a fork with a PS4Debug plugin); UWorld hangs off GWorld |
| `rpc_stub.txt` | address of the RPC stub in the process, reused by `uecall` |

Heap addresses of instances (PlayerController, Pawn, HUD, ...) change on every game start. Classes, offsets
and the SDK stay valid, but live access to instances needs a fresh dump. The tools detect stale addresses
and abort with a message instead of crashing the game.

The dump is not atomic: objects the game frees during the dump are still listed in GObjects while their
memory is already reused. The object iterator skips such slots (`obj->Index != slot`) and `uegen` requires an
Outer and a resolvable class for every UStruct/UEnum/UClass. Without these filters Days Gone produced
structs of 1.8 GB. Old headers stay in the output folder on regeneration, delete `SDK/` first if in doubt.

## Tools

| Tool | Purpose |
|---|---|
| `ps4dump.py --host IP -o ../dumps/<ID>/dump` | dump only (~100 MB/s) |
| `uefind.py` / `ue3find.py` | GNames/GObjects finders (UE4/5 and UE3), picked automatically by `ueobj` |
| `ueobj.py` | object model on top of the dump, measures all offsets |
| `uegen.py dump.bin outdir` | SDK generator, also finds GWorld and the ProcessEvent vtable index |
| `uerc.py dump.bin out.rcnet [--roots World,Player --depth N]` | ReClass export; use `--roots` to keep it small, ReClass does not load >10k classes in usable time |
| `il2rc.py dump.cs out.rcnet [--images Assembly-CSharp] [--roots A,B --depth N] [--module x.prx \| --base 0x400000]` | ReClass export from Il2CppDumper output (Unity); shares the writer `rcwrite.py` with `uerc` |
| `uelive.py dump.bin IP Class [Property [Value]]` | read or write a property on all live instances (including subclasses); without property: list |
| `uecall.py dump.bin IP Class Function [obj=0xADDR] [Param=Value ...]` | call a UFunction through ProcessEvent via the ps4debug RPC stub; out parameters and ReturnValue are read back |
| `ue3gt.py dump.bin IP <0xADDR\|Class\|Class.Property> Function [Param=Value ...]` | **UE3:** run a script function on the game thread (see below) |
| `ue4gt.py dump.bin IP <0xADDR\|Class> Function [Param=Value\|local:Local ...] carrier=Class.ExecuteUbergraph_X [then=...]` | **UE4/5:** run a function on the game thread through a ticking Blueprint Ubergraph. Under Git Bash set `MSYS_NO_PATHCONV=1`, otherwise `/Game/` paths get rewritten |
| `uecheat.py dump.bin out.TLH ["Name=Path=Value" \| "Name=button=Path.Function"]` | generates a trainer file for a separate in-game cheat menu PRX (not part of this repo) |
| `ps4prx.py IP load\|unload\|list ...` | load, unload or list PRX modules in the running game (ps4debug-ng + GoldHEN FTP) |

Contributed extras (by Stoned):

| Tool | Purpose |
|---|---|
| `uediff.py A.bin B.bin [C.bin] --type float --rel dec[,inc] [--v0 100 --v1 75] [--owner Regex]` | compare two or more dumps like a "next scan" and resolve every hit to its UObject and property. Needs `numpy` |
| `uediff.py --xref dump.bin --disp 0x2A8 [--write] [--mnem mov,vmovss]` | list the instructions in the game image that access a struct offset. Needs `capstone` |
| `peek.py read\|write\|freeze\|watch\|trace 0xADDR [value] --host IP [--type float]` (or set `PS4_HOST`) | raw live memory access; `trace` sets a hardware write watchpoint and reports the writing instruction (experimental, needs `capstone`) |

Examples:

```
python uelive.py ../dumps/CUSA31820/dump.bin 192.168.x.y HumanoidSMG026 CustomTimeDilation 2.0
python uecall.py ../dumps/CUSA31820/dump.bin 192.168.x.y GameplayStatics SetGlobalTimeDilation WorldContextObject=0x1038e68080 TimeDilation=0.3
python uecall.py ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController FOV obj=0x205dea010 F=120
```

## RPC call or game thread?

`uecall` runs the function on a thread that ps4debug creates inside the process. That is enough for functions
that only read or write values (confirmed: Add_IntInt, SetGlobalTimeDilation, FOV). **Anything that creates
objects crashes there** (UE3 `new`, UE4 SpawnActor, widgets). For those there are `ue3gt` and `ue4gt`: they
replace, for half a second, the bytecode of an event the engine runs every frame (UE3: `HUD.PostRender`,
UE4: a Blueprint Ubergraph driven by ReceiveTick or an axis event) with the wanted call and then restore the
original bytes. Parameters are embedded as bytecode constants (float, int, byte, bool, object, name; string on
UE3 only). Return values are lost.

Both tools verify live that the target object still exists and the function is in its class chain before
patching. Calling an unknown function name would crash the script VM.

## UE3 notes

- Functions with an `iNative` number (opcode natives such as Abs, Sqrt, SetTimer) return immediately from
  ProcessEvent. `uecall` refuses them. Natives without a number (Func pointer) can be called.
- UWorld and ULevel have no reflection. `ue3find` measures PersistentLevel, WorldInfo and Actors and adds them
  to the ReClass export. Every streamed level package has its own `TheWorld`; GWorld is the one owning the
  PlayerController.
- Activating a CheatManager without an INI (Dishonored, class address from `GObjects.txt`):

```
python uelive.py ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController CheatClass 0x2680e10
python ue3gt.py  ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController AddCheats
python ue3gt.py  ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController.CheatManager Fly
python ue3gt.py  ../dumps/CUSA02231/dump.bin 192.168.x.y PlayerController.CheatManager Slomo T=0.3
```

  The console build nulls `CheatClass` on the instance, the default object still has it. `AddCheats` with
  `CheatClass == None` crashes the game (`new None`), so set it first.

## Walkthroughs

- [examples/AC_India.md](examples/AC_India.md): UE3, from dump to God mode and unlocked trophies without a PRX.
- [examples/DaysGone.md](examples/DaysGone.md): UE4, CheatManager via `uecall`, Bend's own debug menu opened on
  the game thread via `ue4gt`.

## Samples

`samples/` contains the generated SDKs (GameDefines.hpp, headers, offsets.json, GObjects.txt, ReClass project)
for 22 games, one 7z archive per game, named `<TitleID>-<Game> <Version>.7z`. UE3: Thief, Sherlock Holmes
(2), Life is Strange, Borderlands 2 / Pre-Sequel, Dishonored, THPS5, AC Chronicles, Mass Effect LE 1-3.
UE4/UE5: FF7 Remake, Days Gone, SAO Fatal Bullet / Last Recollection, Jedi Fallen Order, Hogwarts Legacy,
WWE 2K Battlegrounds, Tiny Tina's Wonderlands, The Quarry, High on Life.

## Crash diagnosis

Read the console UART: `ncat 192.168.x.y 3232`. A crash report contains registers and a backtrace; with the
addresses from `GameDefines.hpp` and the dump the location can be resolved offline.

## Credits

- [Dumper-7](https://github.com/Encryqed/Dumper-7) by Encryqed: the offset-finding heuristics in `ueobj.py`
  follow its OffsetFinder and were the reference for the UE4/UE5 object model.
- [ps4debug](https://github.com/jogolden/ps4debug) / ps4debug-ng and the `ps4debug` Python package.
- SDK output uses the CodeRed header layout.
- Stoned: `uediff.py` and `peek.py`.

## License

MIT, see [LICENSE](LICENSE).
