# coding: utf-8
#

from __future__ import print_function

import os
import typing
import asyncio

from deprecation import deprecated

from ._adb import AdbConnection
from ._adb import BaseClient as _BaseClient
from ._device import AdbDevice, Sync
from ._proto import *
from ._utils import adb_path, StopEvent
from ._version import __version__
from .errors import *


class AdbClient(_BaseClient):
    def sync(self, serial: str) -> Sync:
        return Sync(self, serial)

    async def list(self) -> typing.List[AdbDeviceInfo]:
        """
        Returns:
            list of device info, including offline
        """
        infos = []
        async with await self.make_connection() as c:
            await c.send_command("host:devices")
            await c.check_okay()
            output = await c.read_string_block()
            for line in output.splitlines():
                parts = line.strip().split("\t")
                if len(parts) != 2:
                    continue
                infos.append(AdbDeviceInfo(serial=parts[0], state=parts[1]))
        return infos

    async def iter_device(self):
        """
        Returns:
            iter only AdbDevice with state:device
        """
        for info in await self.list():
            if info.state != "device":
                continue
            yield AdbDevice(self, serial=info.serial)

    async def device_list(self) -> typing.List[AdbDevice]:
        return list(await self.iter_device())

    async def device(self, serial: str = None, transport_id: int = None) -> AdbDevice:
        if serial:
            return AdbDevice(self, serial=serial)

        if transport_id:
            return AdbDevice(self, transport_id=transport_id)

        serial = os.environ.get("ANDROID_SERIAL")
        if not serial:
            ds = await self.device_list()
            if len(ds) == 0:
                raise AdbError("Can't find any android device/emulator")
            if len(ds) > 1:
                raise AdbError("more than one device/emulator, please specify the serial number")
            return ds[0]
        return AdbDevice(self, serial)


adb = AdbClient()
device = adb.device


async def _main():
    print("server version:", await adb.server_version())
    print("devices:", await adb.device_list())
    d = (await adb.device_list())[0]

    print(d.serial)
    async for f in adb.sync(d.serial).iter_directory("/data/local/tmp"):
        print(f)

    finfo = await adb.sync(d.serial).stat("/data/local/tmp")
    print(finfo)
    import io

    sync = adb.sync(d.serial)
    filepath = "/data/local/tmp/hi.txt"
    await sync.push(io.BytesIO(b"hi5a4de5f4qa6we541fq6w1ef5a61f65ew1rf6we"), filepath, 0o644)

    print("FileInfo", await sync.stat(filepath))
    async for chunk in sync.iter_content(filepath):
        print(chunk)
    # sync.pull(filepath)


if __name__ == "__main__":
    asyncio.run(_main())
