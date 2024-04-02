#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Created on Fri May 06 2022 10:33:39 by codeskyblue"""

from __future__ import annotations

import abc
import datetime
import io
import json
import os
import pathlib
import re
import socket
import stat
import struct
import subprocess
import threading
import time
import typing
from typing import Optional, Union
import asyncio

import apkutils2
import httpx
from deprecation import deprecated
from PIL import Image

from PIL import UnidentifiedImageError

from retry import retry

from ._adb import AdbConnection, BaseClient
from ._proto import (
    ShellReturn,
    ForwardItem,
    ReverseItem,
    Network,
    FileInfo,
    WindowSize,
    RunningAppInfo,
    AppInfo,
    StrOrPathLike,
    AdbCmd,
)
from ._utils import APKReader, ReadProgress, StopEvent, adb_path, append_path, get_free_port, humanize, list2cmdline
from ._version import __version__
from .errors import AdbError, AdbInstallError

_DISPLAY_RE = re.compile(
    r".*DisplayViewport{.*?valid=true, .*?orientation=(?P<orientation>\d+), .*?deviceWidth=(?P<width>\d+), deviceHeight=(?P<height>\d+).*"
)

_DEFAULT_SOCKET_TIMEOUT = 600  # 10 minutes


class BaseDevice:
    """Basic operation for a device"""

    def __init__(self, client: BaseClient, serial: str = None, transport_id: int = None):
        self._client = client
        self._serial = serial
        self._transport_id: int = transport_id
        self._properties = {}  # store properties data

        if not serial and not transport_id:
            raise AdbError("serial, transport_id must set atleast one")

        self._prepare()

    def _prepare(self):
        """rewrite in sub class"""

    @property
    def serial(self) -> str:
        return self._serial

    async def open_transport(self, command: str = None, timeout: float = _DEFAULT_SOCKET_TIMEOUT) -> AdbConnection:
        # connect has it own timeout
        c = await self._client.make_connection()
        if timeout:
            c.settimeout(timeout)

        if command:
            if self._transport_id:
                await c.send_command(f"host-transport-id:{self._transport_id}:{command}")
            elif self._serial:
                await c.send_command(f"host-serial:{self._serial}:{command}")
            else:
                raise RuntimeError
        else:
            if self._transport_id:
                await c.send_command(f"host:transport-id:{self._transport_id}")
            elif self._serial:
                # host:tport:serial:xxx is also fine, but receive 12 bytes
                # recv: 4f 4b 41 59 14 00 00 00 00 00 00 00              OKAY........
                # so here use host:transport
                await c.send_command(f"host:transport:{self._serial}")
            else:
                raise RuntimeError
        return c

    async def _get_with_command(self, cmd: str) -> str:
        c = await self.open_transport(cmd)
        return await c.read_string_block()

    async def get_state(self) -> str:
        """return device state {offline,bootloader,device}"""
        return await self._get_with_command("get-state")

    async def get_serialno(self) -> str:
        """return the real device id, not the connect serial"""
        return await self._get_with_command("get-serialno")

    async def get_devpath(self) -> str:
        """example return: usb:12345678Y"""
        return await self._get_with_command("get-devpath")

    async def get_features(self) -> str:
        """
        Return example:
            'abb_exec,fixed_push_symlink_timestamp,abb,stat_v2,apex,shell_v2,fixed_push_mkdir,cmd'
        """
        return await self._get_with_command("features")

    async def info(self) -> dict:
        return {
            "serialno": await self.get_serialno(),
            "devpath": await self.get_devpath(),
            "state": await self.get_state(),
        }

    def __repr__(self):
        return "AdbDevice(serial={})".format(self.serial)

    @property
    def sync(self) -> "Sync":
        return Sync(self._client, self.serial)

    @property
    def prop(self) -> "Property":
        return Property(self)

    def adb_output(self, *args, **kwargs):
        """Run adb command use subprocess and get its content

        Returns:
            string of output

        Raises:
            EnvironmentError
        """
        cmds = [adb_path(), "-s", self._serial] if self._serial else [adb_path()]
        cmds.extend(args)
        try:
            return subprocess.check_output(cmds, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT).decode("utf-8")
        except subprocess.CalledProcessError as e:
            if kwargs.get("raise_error", True):
                raise EnvironmentError("subprocess", cmds, e.output.decode("utf-8", errors="ignore"))

    async def shell(
        self,
        cmdargs: Union[str, list, tuple],
        stream: bool = False,
        timeout: Optional[float] = _DEFAULT_SOCKET_TIMEOUT,
        encoding: str | None = "utf-8",
        rstrip=True,
    ) -> typing.Union[AdbConnection, str, bytes]:
        """Run shell inside device and get it's content

        Args:
            rstrip (bool): strip the last empty line (Default: True)
            stream (bool): return stream instead of string output (Default: False)
            timeout (float): set shell timeout
            encoding (str): set output encoding (Default: utf-8), set None to make return bytes
            rstrip (bool): strip the last empty line, only work when encoding is set

        Returns:
            string of output when stream is False
            AdbConnection when stream is True

        Raises:
            AdbTimeout

        Examples:
            shell("ls -l")
            shell(["ls", "-l"])
            shell("ls | grep data")
        """
        if isinstance(cmdargs, (list, tuple)):
            cmdargs = list2cmdline(cmdargs)
        if stream:
            timeout = None
        c = await self.open_transport(timeout=timeout)
        await c.send_command("shell:" + cmdargs)
        if stream:
            return c
        output = await c.read_until_close(encoding=encoding)
        if encoding:
            return output.rstrip() if rstrip else output
        return output

    async def shell2(
        self,
        cmdargs: Union[str, list, tuple],
        timeout: Optional[float] = _DEFAULT_SOCKET_TIMEOUT,
        encoding: str | None = "utf-8",
        rstrip=False,
    ) -> ShellReturn:
        """
        Run shell command with detail output
        Args:
            cmdargs (str | list | tuple): command args
            timeout (float): set shell timeout, seconds
            encoding (str): set output encoding (Default: utf-8), set None to make return bytes
            rstrip (bool): strip the last empty line, only work when encoding is set

        Returns:
            ShellOutput

        Raises:
            AdbTimeout
        """
        if isinstance(cmdargs, (list, tuple)):
            cmdargs = list2cmdline(cmdargs)
        assert isinstance(cmdargs, str)
        MAGIC = "X4EXIT:"
        newcmd = cmdargs + f"; echo {MAGIC}$?"
        output = await self.shell(newcmd, timeout=timeout, encoding=encoding, rstrip=True)
        rindex = output.rfind(MAGIC if encoding else MAGIC.encode())
        if rindex == -1:  # normally will not possible
            raise AdbError("shell output invalid", newcmd, output)
        returncoode = int(output[rindex + len(MAGIC) :])
        output = output[:rindex]
        if rstrip and encoding:
            output = output.rstrip()
        return ShellReturn(command=cmdargs, returncode=returncoode, output=output)

    async def forward(self, local: str, remote: str, norebind: bool = False):
        args = ["forward"]
        if norebind:
            args.append("norebind")
        args.append(local + ";" + remote)
        await self.open_transport(":".join(args))

    async def forward_port(self, remote: Union[int, str]) -> int:
        """forward remote port to local random port"""
        if isinstance(remote, int):
            remote = "tcp:" + str(remote)
        async for f in self.forward_list():
            if f.serial == self._serial and f.remote == remote and f.local.startswith("tcp:"):  # yapf: disable
                return int(f.local[len("tcp:") :])
        local_port = get_free_port()
        await self.forward("tcp:" + str(local_port), remote)
        return local_port

    async def forward_list(self):
        c = await self.open_transport("list-forward")
        content = await c.read_string_block()
        for line in content.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            yield ForwardItem(*parts)

    async def reverse(self, remote: str, local: str, norebind: bool = False):
        """
        Args:
            serial (str): device serial
            remote, local (str):
                - tcp:<port>
                - localabstract:<unix domain socket name>
                - localreserved:<unix domain socket name>
                - localfilesystem:<unix domain socket name>
            norebind (bool): fail if already reversed when set to true

        Raises:
            AdbError
        """
        args = ["forward"]
        if norebind:
            args.append("norebind")
        args.append(local + ";" + remote)
        await self.open_transport(":".join(args))

    async def reverse_list(self):
        c = await self.open_transport()
        await c.send_command("reverse:list-forward")
        content = await c.read_string_block()
        for line in content.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            yield ReverseItem(*parts[1:])

    def push(self, local: str, remote: str) -> str:
        return self.adb_output("push", local, remote)

    async def create_connection(self, network: Network, address: Union[int, str]) -> socket.socket:
        """
        Used to connect a socket (unix of tcp) on the device

        Returns:
            socket object

        Raises:
            AssertionError, ValueError
        """
        c = await self.open_transport()
        match network:
            case Network.TCP:
                assert isinstance(address, int)
                await c.send_command("tcp:" + str(address))
            case Network.UNIX | Network.LOCAL_ABSTRACT:
                assert isinstance(address, str)
                await c.send_command("localabstract:" + address)
            case Network.LOCAL_FILESYSTEM | Network.LOCAL | Network.DEV | Network.LOCAL_RESERVED:
                await c.send_command(network + ":" + address)
            case _:
                raise ValueError("Unsupported network type", network)
        c._finalizer.detach()
        return c

    async def root(self):
        """restart adbd as root

        Return example:
            cannot run as root in production builds
        """
        # Ref: https://github.com/Swind/pure-python-adb/blob/master/ppadb/command/transport/__init__.py#L179
        c = await self.open_transport()
        await c.send_command("root:")
        return await c.read_until_close()

    async def tcpip(self, port: int):
        """restart adbd listening on TCP on PORT

        Return example:
            restarting in TCP mode port: 5555
        """
        c = await self.open_transport()
        await c.send_command("tcpip:" + str(port))
        return await c.read_until_close()

    async def logcat(
        self,
        file: StrOrPathLike = None,
        clear: bool = False,
        re_filter: typing.Union[str, re.Pattern] = None,
        command: str = "logcat -v time",
    ) -> StopEvent:
        """
        Args:
            file (str): file path to save logcat
            clear (bool): clear logcat before start
            re_filter (str | re.Pattern): regex pattern to filter logcat
            command (str): logcat command, default is "logcat -v time"

        Example usage:
            >>> evt = device.logcat("logcat.txt", clear=True, re_filter=".*python.*")
            >>> asyncio.sleep(10)
            >>> evt.stop()
        """
        if re_filter:
            if isinstance(re_filter, str):
                re_filter = re.compile(re_filter)
            assert isinstance(re_filter, re.Pattern)

        if clear:
            await self.shell("logcat --clear")

        def _filter_func(line: str) -> bool:
            if re_filter is None:
                return True
            return re_filter.search(line) is not None

        def _copy2file(stream: AdbConnection, fdst: typing.TextIO, event: StopEvent, filter_func: typing.Callable[[str], bool]):
            try:
                fsrc = stream.conn.makefile("r", encoding="UTF-8", errors="replace")
                while not event.is_stopped():
                    line = fsrc.readline()
                    if not line:
                        break
                    if filter_func(line):
                        fdst.write(line)
                        fdst.flush()
            finally:
                fsrc.close()
                stream.close()
                event.done()

        event = StopEvent()
        stream = self.shell(command, stream=True)
        fdst = pathlib.Path(file).open("w", encoding="UTF-8")
        threading.Thread(name="logcat", target=_copy2file, args=(stream, fdst, event, _filter_func), daemon=True).start()
        return event


class Property:
    def __init__(self, d: BaseDevice):
        self._d = d

    def __str__(self):
        return f"product:{self.name} model:{self.model} device:{self.device}"

    async def get(self, name: str, cache=True) -> str:
        if cache and name in self._d._properties:
            return self._d._properties[name]
        value = self._d._properties[name] = (await self._d.shell(["getprop", name])).strip()
        return value

    async def name(self):
        return await self.get("ro.product.name", cache=True)

    async def model(self):
        return await self.get("ro.product.model", cache=True)

    async def device(self):
        return await self.get("ro.product.device", cache=True)


class Sync:
    def __init__(self, adbclient: BaseClient, serial: str):
        self._adbclient = adbclient
        self._serial = serial

    async def _prepare_sync(self, path: str, cmd_raw: bytes):
        c = await self._adbclient.make_connection()
        await c.send_command(":".join(["host", "transport", self._serial]))
        await c.send_command("sync:")
        # {COMMAND}{LittleEndianPathLength}{Path}
        path = path.encode("utf-8")
        path_len = len(path)
        await c.send(cmd_raw + struct.pack("<I", path_len) + path)
        return c

    async def exists(self, path: str) -> bool:
        finfo = await self.stat(path)
        return finfo.mtime is not None

    async def stat(self, path: str) -> FileInfo:
        async with await self._prepare_sync(path, AdbCmd.STAT) as c:
            assert AdbCmd.STAT == await c.read(4)
            mode, size, mtime = struct.unpack("<III", await c.read(12))
            # when mtime is 0, windows will error
            mdtime = datetime.datetime.fromtimestamp(mtime) if mtime else None
            return FileInfo(mode, size, mdtime, path)

    async def iter_directory(self, path: str):
        async with await self._prepare_sync(path, b"LIST") as c:
            while 1:
                if AdbCmd.DONE == await c.read(4):
                    break
                mode, size, mtime, namelen = struct.unpack("<IIII", c.read(16))
                name = await c.read_string(namelen)
                try:
                    mtime = datetime.datetime.fromtimestamp(mtime)
                except OSError:  # bug in Python 3.6
                    mtime = datetime.datetime.now()
                yield FileInfo(mode, size, mtime, name)

    async def list(self, path: str) -> typing.List[str]:
        return list(await self.iter_directory(path))

    async def push(
            self,
            src: typing.Union[pathlib.Path, str, bytes, bytearray, typing.BinaryIO],
            dst: typing.Union[pathlib.Path, str],
            mode: int = 0o755,
            check: bool = False) -> int:  # yapf: disable
        # IFREG: File Regular
        # IFDIR: File Directory
        if isinstance(src, pathlib.Path):
            src = src.open("rb")
        elif isinstance(src, str):
            src = pathlib.Path(src).open("rb")
        elif isinstance(src, (bytes, bytearray)):
            src = io.BytesIO(src)
        else:
            if not hasattr(src, "read"):
                raise TypeError("Invalid src type: %s" % type(src))

        if isinstance(dst, pathlib.Path):
            dst = dst.as_posix()
        path = dst + "," + str(stat.S_IFREG | mode)
        total_size = 0
        async with await self._prepare_sync(path, b"SEND") as c:
            r = src if hasattr(src, "read") else open(src, "rb")
            try:
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        mtime = int(datetime.datetime.now().timestamp())
                        await c.send(b"DONE" + struct.pack("<I", mtime))
                        break
                    await c.send(b"DATA" + struct.pack("<I", len(chunk)))
                    await c.send(chunk)
                    total_size += len(chunk)
                if (status_msg := await c.read(4)) != AdbCmd.OKAY:
                    raise AdbError(status_msg)
            finally:
                if hasattr(r, "close"):
                    r.close()
        if check:
            file_size = (await self.stat(dst)).size
            if total_size != file_size:
                raise AdbError("Push not complete, expect pushed %d, actually pushed %d" % (total_size, file_size))
        return total_size

    async def iter_content(self, path: str):
        async with await self._prepare_sync(path, b"RECV") as c:
            while True:
                match await c.read(4):
                    case AdbCmd.DATA:
                        chunk_size = struct.unpack("<I", await c.read(4))[0]
                        chunk = await c.read(chunk_size)
                        if len(chunk) != chunk_size:
                            raise RuntimeError("read chunk missing")
                        yield chunk
                    case AdbCmd.DONE:
                        break
                    case AdbCmd.FAIL:
                        str_size = struct.unpack("<I", await c.read(4))[0]
                        error_message = await c.read_string(str_size)
                        raise AdbError(error_message, path)
                    case cmd:
                        raise AdbError("Invalid sync cmd", cmd)

    async def read_bytes(self, path: str) -> bytes:
        chunks = [chunk async for chunk in self.iter_content(path)]
        return b"".join(chunks)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """read content of a file"""
        return (await self.read_bytes(path)).decode(encoding=encoding)

    async def pull(self, src: str, dst: typing.Union[str, pathlib.Path], exist_ok: bool = False) -> int:
        """
        Pull file or directory from device:src to local:dst

        Returns:
            total file size pulled
        """
        src_file_info = await self.stat(src)
        is_src_file = src_file_info.mode & stat.S_IFREG != 0

        if is_src_file:
            return await self.pull_file(src, dst)
        else:
            return await self.pull_dir(src, dst, exist_ok)

    async def pull_file(self, src: str, dst: typing.Union[str, pathlib.Path]) -> int:
        """
        Pull file from device:src to local:dst

        Returns:
            file size
        """
        if isinstance(dst, str):
            dst = pathlib.Path(dst)
        with dst.open("wb") as f:
            size = 0
            async for chunk in self.iter_content(src):
                f.write(chunk)
                size += len(chunk)
            return size

    async def pull_dir(self, src: str, dst: typing.Union[str, pathlib.Path], exist_ok: bool = True) -> int:
        """Pull directory from device:src into local:dst

        Returns:
            total files size pulled
        """

        async def rec_pull_contents(src: str, dst: typing.Union[str, pathlib.Path], exist_ok: bool = True) -> int:
            s = 0
            items = [i async for i in self.iter_directory(src)]

            items = list(filter(lambda i: i.path != "." and i.path != "..", items))

            dirs = list(filter(lambda f: stat.S_IFDIR & f.mode != 0, items))
            files = list(filter(lambda f: stat.S_IFREG & f.mode != 0, items))

            for dir in dirs:
                new_src: str = append_path(src, dir.path)
                new_dst: pathlib.Path = pathlib.Path(append_path(dst, dir.path))
                os.makedirs(new_dst, exist_ok=exist_ok)
                s += await rec_pull_contents(new_src, new_dst, exist_ok=exist_ok)

            for file in files:
                new_src: str = append_path(src, file.path)
                new_dst: str = append_path(dst, file.path)
                s += await self.pull_file(new_src, new_dst)

            return s

        if isinstance(dst, str):
            dst = pathlib.Path(dst)
        os.makedirs(dst, exist_ok=exist_ok)

        return await rec_pull_contents(src, dst, exist_ok=exist_ok)


class AdbDevice(BaseDevice):
    """provide custom functions for some complex operations"""

    def _prepare(self):
        self._record_client = None

    async def __screencap(self) -> Image.Image:
        def pic_load(raw_bytes: bytes):
            with io.BytesIO(raw_bytes) as pic_bytes:
                im = Image.open(pic_bytes)
                im.load()
            return im

        thread_id = threading.get_native_id()
        inner_tmp_path = f"/data/local/tmp/adbutils-tmp{thread_id}.png"
        await self.shell(["screencap", "-p", inner_tmp_path])
        try:
            raw_bytes = await self.sync.read_bytes(inner_tmp_path)
            return await asyncio.to_thread(pic_load, raw_bytes)
        finally:
            await self.shell(["rm", inner_tmp_path])

    async def screenshot(self) -> Image.Image:
        """not thread safe

        Note:
            screencap to file and pull is more stable then shell(stream=True)
            Ref: https://github.com/openatx/adbutils/pull/78
        """
        try:
            return await self.__screencap()
        except UnidentifiedImageError | AdbError:
            wsize = await self.window_size()
            return Image.new("RGB", wsize)  # return a blank image when screenshot is not allowed

    async def switch_screen(self, status: bool):
        """
        turn screen on/off

        Args:
            status (bool)
        """
        _key_dict = {
            True: "224",
            False: "223",
        }
        return await self.keyevent(_key_dict[status])

    async def switch_airplane(self, status: bool):
        """
        turn airplane-mode on/off

        Args:
            status (bool)
        """
        base_setting_cmd = ["settings", "put", "global", "airplane_mode_on"]
        base_am_cmd = ["am", "broadcast", "-a", "android.intent.action.AIRPLANE_MODE", "--ez", "state"]
        if status:
            base_setting_cmd += ["1"]
            base_am_cmd += ["true"]
        else:
            base_setting_cmd += ["0"]
            base_am_cmd += ["false"]

        # TODO better idea about return value?
        await self.shell(base_setting_cmd)
        return await self.shell(base_am_cmd)

    async def switch_wifi(self, status: bool) -> str:
        """
        turn WiFi on/off

        Args:
            status (bool)
        """
        arglast = "enable" if status else "disable"
        cmdargs = ["svc", "wifi", arglast]
        return await self.shell(cmdargs)

    async def keyevent(self, key_code: typing.Union[int, str]) -> str:
        """adb _run input keyevent KEY_CODE"""
        return await self.shell(["input", "keyevent", str(key_code)])

    def __is_percent(self, v):
        return isinstance(v, float) and v <= 1.0

    async def click(self, x, y) -> None:
        """
        simulate android tap

        Args:
            x, y: int
        """
        is_percent = self.__is_percent
        if any(map(is_percent, [x, y])):
            w, h = await self.window_size()
            x = int(x * w) if is_percent(x) else x
            y = int(y * h) if is_percent(y) else y
        x, y = map(str, [x, y])
        await self.shell(["input", "tap", x, y])

    async def swipe(self, sx, sy, ex, ey, duration: float = 1.0) -> None:
        """
        swipe from start point to end point

        Args:
            sx, sy: start point(x, y)
            ex, ey: end point(x, y)
        """
        is_percent = self.__is_percent
        if any(map(is_percent, [sx, sy, ex, ey])):
            w, h = await self.window_size()
            sx = int(sx * w) if is_percent(sx) else sx
            sy = int(sy * h) if is_percent(sy) else sy
            ex = int(ex * w) if is_percent(ex) else ex
            ey = int(ey * h) if is_percent(ey) else ey
        x1, y1, x2, y2 = map(str, [sx, sy, ex, ey])
        await self.shell(["input", "swipe", x1, y1, x2, y2, str(int(duration * 1000))])

    async def send_keys(self, text: str):
        """
        Type a given text

        Args:
            text: text to be type
        """
        escaped_text = self._escape_special_characters(text)
        return await self.shell(["input", "text", escaped_text])

    @staticmethod
    def _escape_special_characters(text: str):
        """
        A helper that escape special characters

        Args:
            text: str
        """
        escaped = text.translate(
            str.maketrans(
                {
                    "-": r"\-",
                    "+": r"\+",
                    "[": r"\[",
                    "]": r"\]",
                    "(": r"\(",
                    ")": r"\)",
                    "{": r"\{",
                    "}": r"\}",
                    "\\": r"\\\\",
                    "^": r"\^",
                    "$": r"\$",
                    "*": r"\*",
                    ".": r"\.",
                    ",": r"\,",
                    ":": r"\:",
                    "~": r"\~",
                    ";": r"\;",
                    ">": r"\>",
                    "<": r"\<",
                    "%": r"\%",
                    "#": r"\#",
                    "'": r"\\'",
                    '"': r'\\"',
                    "`": r"\`",
                    "!": r"\!",
                    "?": r"\?",
                    "|": r"\|",
                    "=": r"\=",
                    "@": r"\@",
                    "/": r"\/",
                    "_": r"\_",
                    " ": r"%s",  # special
                    "&": r"\&",
                }
            )
        )
        return escaped

    async def wlan_ip(self) -> str:
        """
        get device wlan ip

        Raises:
            AdbError
        """
        result = await self.shell(["ifconfig", "wlan0"])
        m = re.search(r"inet\s*addr:(.*?)\s", result, re.DOTALL)
        if m:
            return m.group(1)

        # Huawei P30, has no ifconfig
        result = self.shell(["ip", "addr", "show", "dev", "wlan0"])
        m = re.search(r"inet (\d+.*?)/\d+", result)
        if m:
            return m.group(1)

        # On VirtualDevice, might use eth0
        result = self.shell(["ifconfig", "eth0"])
        m = re.search(r"inet\s*addr:(.*?)\s", result, re.DOTALL)
        if m:
            return m.group(1)

        raise AdbError("fail to parse wlan ip")

    @retry(BrokenPipeError, delay=5.0, jitter=[3, 5], tries=3)
    async def install(
        self,
        path_or_url: str,
        nolaunch: bool = False,
        uninstall: bool = False,
        silent: bool = False,
        callback: typing.Callable[[str], None] = None,
        flags: list = ["-r", "-t"],
    ):
        """
        Install APK to device

        Args:
            path_or_url: local path or http url
            nolaunch: do not launch app after install
            uninstall: uninstall app before install
            silent: disable log message print
            callback: only two event now: <"BEFORE_INSTALL" | "FINALLY">
            flags (list): default ["-r", "-t"]

        Raises:
            AdbInstallError, BrokenPipeError
        """
        if re.match(r"^https?://", path_or_url):
            resp = await httpx.AsyncClient().get(path_or_url, stream=True)
            resp.raise_for_status()
            length = int(resp.headers.get("Content-Length", 0))
            r = ReadProgress(resp.raw, length)
            print("tmpfile path:", r.filepath())
        else:
            length = os.stat(path_or_url).st_size
            fd = open(path_or_url, "rb")
            r = ReadProgress(fd, length, source_path=path_or_url)

        def _dprint(*args):
            if not silent:
                print(*args)

        dst = "/data/local/tmp/tmp-%d.apk" % (int(time.time() * 1000))
        _dprint("push to %s" % dst)

        start = time.time()
        await self.sync.push(r, dst)

        # parse apk package-name
        apk = apkutils2.APK(r.filepath())
        package_name = apk.manifest.package_name
        main_activity = apk.manifest.main_activity
        if main_activity and main_activity.find(".") == -1:
            main_activity = "." + main_activity

        version_code = apk.manifest.version_code
        _dprint("packageName:", package_name)
        _dprint("mainActivity:", main_activity)
        _dprint("apkVersion: {}".format(apk.manifest.version_name))
        _dprint("Success pushed, time used %d seconds" % (time.time() - start))

        new_dst = "/data/local/tmp/{}-{}.apk".format(package_name, version_code)
        _dprint("Rename to {}".format(new_dst))
        await self.shell(["mv", dst, new_dst])

        dst = new_dst
        info = await self.sync.stat(dst)
        print("verify pushed apk, md5: %s, size: %s" % (r._hash, humanize(info.size)))
        assert info.size == r.copied

        if uninstall:
            _dprint("Uninstall app first")
            await self.uninstall(package_name)

        _dprint("install to android system ...")
        try:
            start = time.time()
            if callback:
                callback("BEFORE_INSTALL")

            await self.install_remote(dst, clean=True, flags=flags)
            _dprint("Success installed, time used %d seconds" % (time.time() - start))
            if not nolaunch:
                _dprint("Launch app: %s/%s" % (package_name, main_activity))
                await self.app_start(package_name, main_activity)

        except AdbInstallError as e:
            if e.reason in [
                "INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE",
                "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                "INSTALL_FAILED_VERSION_DOWNGRADE",
            ]:
                _dprint("uninstall %s because %s" % (package_name, e.reason))
                await self.uninstall(package_name)
                await self.install_remote(dst, clean=True, flags=flags)
                _dprint("Success installed, time used %d seconds" % (time.time() - start))
                if not nolaunch:
                    _dprint("Launch app: %s/%s" % (package_name, main_activity))
                    await self.app_start(package_name, main_activity)
                    # self.shell([
                    #     'am', 'start', '-n', package_name + "/" + main_activity
                    # ])
            elif e.reason == "INSTALL_FAILED_CANCELLED_BY_USER":
                _dprint("Catch error %s, reinstall" % e.reason)
                await self.install_remote(dst, clean=True, flags=flags)
                _dprint("Success installed, time used %d seconds" % (time.time() - start))
            else:
                # print to console
                print(
                    "Failure "
                    + e.reason
                    + "\n"
                    + "Remote apk is not removed. Manually install command:\n\t"
                    + "adb shell pm install -r -t "
                    + dst
                )
                raise
        finally:
            if callback:
                callback("FINALLY")

    async def install_remote(self, remote_path: str, clean: bool = False, flags: list = ["-r", "-t"]):
        """
        Args:
            remote_path: remote package path
            clean(bool): remove when installed, default(False)
            flags (list): default ["-r", "-t"]

        Raises:
            AdbInstallError
        """
        args = ["pm", "install"] + flags + [remote_path]
        output = await self.shell(args)
        if "Success" not in output:
            raise AdbInstallError(output)
        if clean:
            await self.shell(["rm", remote_path])

    async def uninstall(self, pkg_name: str):
        """
        Uninstall app by package name

        Args:
            pkg_name (str): package name
        """
        return await self.shell(["pm", "uninstall", pkg_name])

    async def getprop(self, prop: str) -> str:
        return await self.shell(["getprop", prop]).strip()

    async def list_packages(self) -> typing.List[str]:
        """
        Returns:
            list of package names
        """
        result = []
        output = await self.shell(["pm", "list", "packages"])
        for m in re.finditer(r"^package:([^\s]+)\r?$", output, re.M):
            result.append(m.group(1))
        return list(sorted(result))

    async def rotation(self) -> int:
        """
        Returns:
            int [0, 1, 2, 3]
        """
        for line in (await self.shell("dumpsys display")).splitlines():
            m = _DISPLAY_RE.search(line, 0)
            if not m:
                continue
            o = int(m.group("orientation"))
            return int(o)

        output = await self.shell("LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap -i")
        try:
            if output.startswith("INFO:"):
                output = output[output.index("{") :]
            data = json.loads(output)
            return data["rotation"] / 90
        except ValueError:
            pass

        raise AdbError("rotation get failed")

    async def _raw_window_size(self) -> WindowSize:
        output = await self.shell("wm size")
        o = re.search(r"Override size: (\d+)x(\d+)", output)
        m = re.search(r"Physical size: (\d+)x(\d+)", output)
        if o:
            w, h = o.group(1), o.group(2)
            return WindowSize(int(w), int(h))
        elif m:
            w, h = m.group(1), m.group(2)
            return WindowSize(int(w), int(h))

        for line in (await self.shell("dumpsys display")).splitlines():
            m = _DISPLAY_RE.search(line, 0)
            if not m:
                continue
            w = int(m.group("width"))
            h = int(m.group("height"))
            return WindowSize(w, h)
        raise AdbError("get window size failed")

    async def window_size(self) -> WindowSize:
        """
        Return screen (width, height)

        Virtual keyborad may get small d.info['displayHeight']
        """
        w, h = await self._raw_window_size()
        s, l = min(w, h), max(w, h)  # noqa: E741
        horizontal = await self.rotation() % 2 == 1
        return WindowSize(l, s) if horizontal else WindowSize(s, l)

    async def app_start(self, package_name: str, activity: str = None):
        """start app with "am start" or "monkey" """
        if activity:
            await self.shell(["am", "start", "-n", package_name + "/" + activity])
        else:
            await self.shell(["monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"])

    async def app_stop(self, package_name: str):
        """stop app with "am force-stop" """
        await self.shell(["am", "force-stop", package_name])

    async def app_clear(self, package_name: str):
        await self.shell(["pm", "clear", package_name])

    async def app_info(self, package_name: str) -> typing.Optional[AppInfo]:
        """
        Get app info

        Returns:
            None or AppInfo
        """
        output = await self.shell(["pm", "path", package_name])
        if "package:" not in output:
            return None

        apk_paths = output.splitlines()
        apk_path = apk_paths[0].split(":", 1)[-1].strip()
        sub_apk_paths = list(map(lambda p: p.replace("package:", "", 1), apk_paths[1:]))

        output = await self.shell(["dumpsys", "package", package_name])
        m = re.compile(r"versionName=(?P<name>[^\s]+)").search(output)
        version_name = m.group("name") if m else ""
        if version_name == "null":  # Java dumps "null" for null values
            version_name = None
        m = re.compile(r"versionCode=(?P<code>\d+)").search(output)
        version_code = m.group("code") if m else ""
        version_code = int(version_code) if version_code.isdigit() else None
        m = re.search(r"PackageSignatures\{.*?\[(.*)\]\}", output)
        signature = m.group(1) if m else None
        if not version_name and signature is None:
            return None
        m = re.compile(r"pkgFlags=\[\s*(.*)\s*\]").search(output)
        pkgflags = m.group(1) if m else ""
        pkgflags = pkgflags.split()

        time_regex = r"[-\d]+\s+[:\d]+"
        m = re.compile(f"firstInstallTime=({time_regex})").search(output)
        first_install_time = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None

        m = re.compile(f"lastUpdateTime=({time_regex})").search(output)
        last_update_time = datetime.datetime.strptime(m.group(1).strip(), "%Y-%m-%d %H:%M:%S") if m else None

        app_info = AppInfo(
            package_name=package_name,
            version_name=version_name,
            version_code=version_code,
            flags=pkgflags,
            first_install_time=first_install_time,
            last_update_time=last_update_time,
            signature=signature,
            path=apk_path,
            sub_apk_paths=sub_apk_paths,
        )
        return app_info

    async def is_screen_on(self):
        output = await self.shell(["dumpsys", "power"])
        return "mHoldingDisplaySuspendBlocker=true" in output

    async def open_browser(self, url: str):
        if not re.match("^https?://", url):
            url = "https://" + url
        await self.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])

    async def dump_hierarchy(self) -> str:
        """
        uiautomator dump

        Returns:
            content of xml

        Raises:
            AdbError
        """
        target = '/data/local/tmp/uidump.xml'
        output = await self.shell(
            f'rm -f {target}; uiautomator dump {target} && echo success')
        if 'ERROR' in output or 'success' not in output:
            raise AdbError("uiautomator dump failed", output)

        buf= await self.sync.read_bytes(target)
        xml_data = buf.decode("utf-8")
        if not xml_data.startswith('<?xml'):
            raise AdbError("dump output is not xml", xml_data)
        return xml_data

    @retry(AdbError, delay=0.5, tries=3, jitter=0.1)
    async def app_current(self) -> RunningAppInfo:
        """
        Returns:
            RunningAppInfo(package, activity, pid?)  pid can be 0

        Raises:
            AdbError
        """
        # Related issue: https://github.com/openatx/uiautomator2/issues/200
        # $ adb shell dumpsys window windows
        # Example output:
        #   mCurrentFocus=Window{41b37570 u0 com.incall.apps.launcher/com.incall.apps.launcher.Launcher}
        #   mFocusedApp=AppWindowToken{422df168 token=Token{422def98 ActivityRecord{422dee38 u0 com.example/.UI.play.PlayActivity t14}}}
        # Regexp
        #   r'mFocusedApp=.*ActivityRecord{\w+ \w+ (?P<package>.*)/(?P<activity>.*) .*'
        #   r'mCurrentFocus=Window{\w+ \w+ (?P<package>.*)/(?P<activity>.*)\}')
        _focusedRE = re.compile(r"mCurrentFocus=Window{.*\s+(?P<package>[^\s]+)/(?P<activity>[^\s]+)\}")
        m = _focusedRE.search(await self.shell(["dumpsys", "window", "windows"]))
        if m:
            return RunningAppInfo(package=m.group("package"), activity=m.group("activity"))

        # search mResumedActivity
        # https://stackoverflow.com/questions/13193592/adb-android-getting-the-name-of-the-current-activity
        package = None
        output = await self.shell(["dumpsys", "activity", "activities"])
        _recordRE = re.compile(
            r'mResumedActivity: ActivityRecord\{.*?\s+(?P<package>[^\s]+)/(?P<activity>[^\s]+)\s.*?\}')  # yapf: disable
        m = _recordRE.search(output)
        if m:
            package = m.group("package")

        # try: adb shell dumpsys activity top
        _activityRE = re.compile(r"ACTIVITY (?P<package>[^\s]+)/(?P<activity>[^/\s]+) \w+ pid=(?P<pid>\d+)")
        output = await self.shell(["dumpsys", "activity", "top"])
        ms = _activityRE.finditer(output)
        ret = None
        for m in ms:
            ret = RunningAppInfo(package=m.group("package"), activity=m.group("activity"), pid=int(m.group("pid")))
            if ret.package == package:
                return ret

        if ret:  # get last result
            return ret
        raise AdbError("Couldn't get focused app")

    async def remove(self, path: str):
        """rm device file"""
        await self.shell(["rm", path])

    async def rmtree(self, path: str):
        """rm -r directory"""
        await self.shell(["rm", "-r", path])


class AbstractScreenRecord:
    @abc.abstractmethod
    def is_recording(self) -> bool:
        """return whether recording"""

    @abc.abstractmethod
    def check_env(self) -> bool:
        """check if environment if valid"""

    @abc.abstractmethod
    def _start(self, filename: str):
        pass

    @abc.abstractmethod
    def _stop(self):
        pass

    def start_recording(self, filename: str):
        if self.is_recording():
            print("recording already running")
            return
        self._start(filename)

    def stop_recording(self):
        if not self.is_recording():
            print("recording alreay stopped")
            return
        self._stop()
