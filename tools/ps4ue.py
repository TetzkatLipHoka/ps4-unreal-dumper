"""One-shot: dump the running Unreal game from a PS4 (ps4debug) and generate the SDK.

    python ps4ue.py 192.168.x.y                 # dump + SDK into ../dumps/<TITLE_ID>/
    python ps4ue.py ../dumps/CUSA48704/dump.bin  # SDK only, from an existing dump
    python ps4ue.py 192.168.x.y BP_CH_Main_Juliet  # extra args: blueprint packages to include in ReClass.rcnet
"""
import asyncio, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "..", "dumps")


async def dump_console(host):
    import ps4debug, ps4dump
    dbg = ps4debug.PS4Debug(host)
    import ueobj
    pid = await ueobj.game_pid(dbg)          # eboot.bin, or by title id (Mass Effect LE: LTCG-BioGame.elf)
    info = await dbg.get_process_info(pid)
    out = os.path.join(DUMPS, info.title_id)
    os.makedirs(out, exist_ok=True)
    dump = os.path.join(out, "dump")
    await ps4dump.dump(host, pid, dump, [], 16 << 30)
    return dump + ".bin"


def main(target, extra_pkgs=()):
    dump = target if target.endswith(".bin") else asyncio.run(dump_console(target))
    out = os.path.join(os.path.dirname(dump), "SDK")
    subprocess.check_call([sys.executable, os.path.join(HERE, "uegen.py"), dump, out])
    subprocess.check_call([sys.executable, os.path.join(HERE, "uerc.py"), dump, os.path.join(out, "ReClass.rcnet")] + extra_pkgs)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
