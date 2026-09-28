"""Load / unload / list PRX modules in a running game through ps4debug-ng (CMD_PROC_PRX_LOAD/UNLOAD/LIST, needs
GoldHEN resident). The module file is uploaded first over GoldHEN's FTP server (port 2121).

    python ps4prx.py 192.168.x.y load ../PS4/odyssey-game/ORBIS_Release/odyssey-game.sprx [dest=/data/odyssey-game.sprx] [proc=eboot.bin]
    python ps4prx.py 192.168.x.y unload 0x80000004 [proc=eboot.bin]
    python ps4prx.py 192.168.x.y list
"""
import asyncio, ftplib, os, struct, sys
import ps4debug
from ps4debug import ResponseCode

CMD_PRX_LOAD, CMD_PRX_UNLOAD, CMD_PRX_LIST = 0xBDAA0011, 0xBDAA0012, 0xBDAA0013
FTP_PORT = 2121


def upload(host, local, dest):
    ftp = ftplib.FTP()
    ftp.connect(host, FTP_PORT, timeout=30)
    ftp.login()
    with open(local, "rb") as f:
        ftp.storbinary(f"STOR {dest}", f)
    ftp.quit()
    print(f"uploaded {os.path.getsize(local)} bytes -> {dest}")


def cstr(s, n):
    return s.encode()[:n - 1].ljust(n, b"\0")


async def main(host, op, *args):
    kw = dict(a.split("=", 1) for a in args if "=" in a)
    pos = [a for a in args if "=" not in a]
    proc = kw.get("proc", "eboot.bin")
    dbg = ps4debug.PS4Debug(host)
    async with dbg.pool.get_socket() as (reader, writer):
        if op == "load":
            local = pos[0]
            dest = kw.get("dest", "/data/" + os.path.basename(local))
            upload(host, local, dest)
            st = await dbg.send_command(CMD_PRX_LOAD, cstr(proc, 32) + cstr(dest, 100), reader=reader, writer=writer)
            if st != ResponseCode.SUCCESS:
                sys.exit(f"PRX load failed ({st}); GoldHEN resident? path on console correct?")
            handle = struct.unpack("<I", await reader.readexactly(4))[0]
            print(f"loaded {dest} into {proc}: handle {handle:#x}")
        elif op == "unload":
            st = await dbg.send_command(CMD_PRX_UNLOAD, cstr(proc, 32) + struct.pack("<I", int(pos[0], 0)), reader=reader, writer=writer)
            print("unload:", st)
        elif op == "list":
            pid = next((p.pid for p in await dbg.get_processes() if p.name.rstrip("\0") == proc), None)
            if pid is None:
                sys.exit(f"no process named {proc} on the console (game not running?)")
            st = await dbg.send_command(CMD_PRX_LIST, struct.pack("<I", pid), reader=reader, writer=writer)
            if st != ResponseCode.SUCCESS:
                sys.exit(f"PRX list failed ({st})")
            count = struct.unpack("<I", await reader.readexactly(4))[0]
            for _ in range(count):
                e = await reader.readexactly(284)
                handle, name, text, tsize, data, dsize = struct.unpack("<I256sQIQI", e)
                nm = name.split(b"\0")[0].decode(errors="replace")
                print(f"  {handle:#010x}  {nm:40}  text {text:#x}+{tsize:#x}  data {data:#x}+{dsize:#x}")
        else:
            sys.exit(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    import ueobj
    ueobj.run(main(*sys.argv[1:]))
