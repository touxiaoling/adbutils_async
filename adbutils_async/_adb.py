import asyncio
import os
import typing
from typing import Union
import weakref

from ._utils import adb_path, async_run
from ._proto import DeviceEvent, AdbCmd
from .errors import AdbError, AdbTimeout


async def _check_server(host: str, port: int) -> bool:
    """Returns if server is running"""
    try:
        reader, writer = await asyncio.streams.open_connection(host, port)
        return True
    except Exception:
        return False
    finally:
        writer.close()
        await writer.wait_closed()


class AdbConnection(object):
    def __init__(self, host, port):
        self.__host = host
        self.__port = port
        self.__reader, self.__writer = None, None
        self.timeout = 600
        self._finalizer = None

    @property
    def writer(self):
        return self.__writer

    @property
    def reader(self):
        return self.__reader

    def settimeout(self, n):
        self.timeout = n

    async def _safe_connect(self):
        try:
            return await asyncio.open_connection(self.__host, self.__port)
        except ConnectionRefusedError:
            # 20s should enough for adb start
            await async_run((adb_path(), "start-server"), timeout=20.0)
            return await asyncio.open_connection(self.__host, self.__port)

    async def safe_connect(self):
        self.__reader, self.__writer = await self._safe_connect()
        self._finalizer = weakref.finalize(self, self.__writer.close)

    @property
    def closed(self) -> bool:
        return not self.__writer.is_closing()

    async def close(self):
        if self._finalizer is not None:
            self._finalizer()
            await self.__writer.wait_closed()

    async def __aenter__(self):
        if self.__writer is None:
            await self.safe_connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def send(self, data: bytes) -> int:
        self.__writer.write(data)
        await self.__writer.drain()
        return len(data)

    async def read(self, n: int) -> bytes:
        try:
            async with asyncio.timeout(self.timeout):
                return await self.__reader.readexactly(n)
        except asyncio.TimeoutError:
            raise AdbTimeout("adb read timeout")

    async def check_okay(self):
        match await self.read(4):
            case AdbCmd.OKAY:
                return
            case AdbCmd.FAIL:
                raise AdbError(await self.read_string_block())
            case data:
                raise AdbError(f"Unknown data: {data}")

    async def send_command(self, cmd: str):
        cmd_bytes = cmd.encode("utf-8")
        await self.send(f"{len(cmd_bytes):04x}".encode("utf-8") + cmd_bytes)
        await self.check_okay()

    async def read_string(self, n) -> str:
        data = await self.read(n)
        return data.decode("utf-8", errors="replace")

    async def read_string_block(self) -> str:
        """
        Raises:
            AdbError
        """
        if not (length := await self.read(4)):
            raise AdbError("connection closed")
        size = int(length, 16)
        return await self.read_string(size)

    async def read_until_close(self, encoding: str | None = "utf-8") -> Union[str, bytes]:
        """
        read until connection close
        :param encoding: default utf-8, if pass None, return bytes
        """
        try:
            async with asyncio.timeout(self.timeout):
                content = await self.__reader.read()
        except asyncio.TimeoutError:
            raise AdbTimeout("adb read timeout")
        return content.decode(encoding, errors="replace") if encoding else content


class BaseClient(object):
    def __init__(self, host: str = None, port: int = None, socket_timeout: float = None):
        if not host:
            host = os.environ.get("ANDROID_ADB_SERVER_HOST", "127.0.0.1")
        if not port:
            port = int(os.environ.get("ANDROID_ADB_SERVER_PORT", 5037))
        self.__host = host
        self.__port = port
        self.__socket_timeout = socket_timeout

    @property
    def host(self) -> str:
        return self.__host

    @property
    def port(self) -> int:
        return self.__port

    async def make_connection(self, timeout: float = None) -> AdbConnection:
        """connect to adb server

        Raises:
            AdbTimeout
        """
        timeout = timeout or self.__socket_timeout
        try:
            _conn = AdbConnection(self.__host, self.__port)
            await _conn.safe_connect()
            if timeout:
                _conn.settimeout(timeout)
            return _conn
        except TimeoutError:
            raise AdbTimeout("connect to adb server timeout")

    async def server_version(self):
        """40 will match 1.0.40
        Returns:
            int
        """
        async with await self.make_connection() as c:
            await c.send_command("host:version")
            return int(await c.read_string_block(), 16)

    async def server_kill(self):
        """
        adb kill-server

        Send host:kill if adb-server is alive
        """
        if await _check_server(self.__host, self.__port):
            async with await self.make_connection() as c:
                await c.send_command("host:kill")

    async def wait_for(self, serial: str = None, transport: str = "any", state: str = "device", timeout: float = 60):
        """Same as wait-for-TRANSPORT-STATE
        Args:
            serial (str): device serial [default None]
            transport (str): {any,usb,local} [default any]
            state (str): {device,recovery,rescue,sideload,bootloader,disconnect} [default device]
            timeout (float): max wait time [default 60]

        Raises:
            AdbError, AdbTimeout
        """
        async with await self.make_connection(timeout=timeout) as c:
            cmds = []
            if serial:
                cmds.extend(["host-serial", serial])
            else:
                cmds.append("host")
            cmds.append("wait-for-" + transport + "-" + state)
            await c.send_command(":".join(cmds))
            await c.check_okay()

    async def connect(self, addr: str, timeout: float = None) -> str:
        """adb connect $addr
        Args:
            addr (str): adb remote address [eg: 191.168.0.1:5555]
            timeout (float): connect timeout

        Returns:
            content adb server returns

        Raises:
            AdbTimeout

        Example returns:
            - "already connected to 192.168.190.101:5555"
            - "unable to connect to 192.168.190.101:5551"
            - "failed to connect to '1.2.3.4:4567': Operation timed out"
        """
        async with await self.make_connection(timeout=timeout) as c:
            await c.send_command("host:connect:" + addr)
            return await c.read_string_block()

    async def disconnect(self, addr: str, raise_error: bool = False) -> str:
        """adb disconnect $addr
        Returns:
            content adb server returns

        Raises:
            when raise_error set to True
                AdbError("error: no such device '1.2.3.4:5678')

        Example returns:
            - "disconnected 192.168.190.101:5555"
        """
        try:
            async with await self.make_connection() as c:
                await c.send_command("host:disconnect:" + addr)
                return await c.read_string_block()
        except AdbError:
            if raise_error:
                raise

    async def track_devices(self):
        """
        Report device state when changes

        Args:
            limit_status: eg, ['device', 'offline'], empty means all status

        Returns:
            Iterator[DeviceEvent], DeviceEvent.status can be one of ['device', 'offline', 'unauthorized', 'absent']

        Raises:
            AdbError when adb-server was killed
        """
        orig_devices = []

        async with await self.make_connection() as c:
            await c.send_command("host:track-devices")
            while True:
                output = await c.read_string_block()
                curr_devices = self._output2devices(output)
                for event in self._diff_devices(orig_devices, curr_devices):
                    yield event
                orig_devices = curr_devices

    def _output2devices(self, output: str):
        devices = []
        for line in output.splitlines():
            fields = line.strip().split("\t", maxsplit=1)
            if len(fields) != 2:
                continue
            serial, status = fields
            devices.append(DeviceEvent(None, serial, status))
        return devices

    def _diff_devices(self, orig: typing.List[DeviceEvent], curr: typing.List[DeviceEvent]):
        for d in set(orig).difference(curr):
            yield DeviceEvent(False, d.serial, "absent")
        for d in set(curr).difference(orig):
            yield DeviceEvent(True, d.serial, d.status)
