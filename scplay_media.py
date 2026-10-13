#!/usr/bin/env python3
"""Управление воспроизведением вне TUI для scplay.

Здесь три независимых механизма, каждый со своим запасным путём:

* :class:`ControlServer` — локальный сокет (Unix) или именованный канал (Windows).
  Это основа: `scplay --send next`, `scplay --status` и любые внешние скрипты
  управляют уже запущенным плеером. Работает на всех платформах.
* :class:`MacNowPlaying` — «сейчас играет» в Пункте управления macOS и
  медиа-клавиши, через MediaPlayer.framework (ctypes, без сторонних пакетов).
* :class:`MprisMedia` — MPRIS2 для Linux (dbus-next, ставится как extras).

Модуль намеренно не зависит от textual: всё общение с интерфейсом идёт через
обратный вызов ``dispatch``, который передаёт команду в поток приложения.

Ошибки любого из механизмов не должны ломать воспроизведение: сбой инициализации
или обновления глушится и попадает в отладочный лог.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

__all__ = [
    "PROTOCOL_VERSION",
    "COMMANDS",
    "ControlServer",
    "MediaMedia",
    "NullMedia",
    "MacNowPlaying",
    "MprisMedia",
    "make_media",
    "control_socket_path",
    "send_command",
    "format_status",
    "run_cli",
]

PROTOCOL_VERSION = 1

#: Команды, которые принимает запущенный scplay. Описание используется в --send-справке.
COMMANDS: dict[str, str] = {
    "status": "показать состояние (JSON)",
    "toggle": "пауза / продолжить",
    "play": "продолжить",
    "pause": "пауза",
    "next": "следующий трек",
    "prev": "предыдущий трек (или в начало)",
    "seek": "перейти к секунде: seek 42",
    "seek-rel": "сдвинуть: seek-rel -5",
    "volume": "задать громкость 0..130: volume 60",
    "volume-rel": "изменить громкость: volume-rel -5",
    "shuffle": "переключить шаффл",
    "repeat": "переключить повтор",
    "theme": "сменить тему",
    "lyrics": "открыть/закрыть вкладку текста",
    "queue": "показать очередь (JSON)",
    "quit": "выйти из scplay",
}


def _log(message: str) -> None:
    """Пишем в отладочный лог приложения, если он доступен."""
    try:  # импорт без цикла: scplay подгружает этот модуль уже после себя
        dbg = sys.modules.get("scplay")
        dbg_fn = getattr(dbg, "dbg", None) if dbg else None
        if dbg_fn:
            dbg_fn(f"медиа: {message}")
            return
    except Exception:
        pass
    print(f"scplay медиа: {message}", file=sys.stderr)


# --------------------------------------------------------------- путь к сокету


def runtime_dir() -> Path:
    """Каталог для служебного сокета: он должен принадлежать только нам."""
    for candidate in (
        os.environ.get("XDG_RUNTIME_DIR"),
        tempfile.gettempdir(),
    ):
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if path.is_dir() and hasattr(os, "getuid") and path.stat().st_uid == os.getuid():
                return path
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def control_socket_path() -> str:
    """Имя канала управления. Уникально для пользователя, чтобы сессии не мешали друг другу."""
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    name = f"scplay-{uid}"
    if os.name == "nt":
        return f"\\\\.\\pipe\\{name}-control"
    return str(runtime_dir() / f"{name}.ctl.sock")


# --------------------------------------------------------------- клиент


def send_command(command: str, timeout: float = 3.0) -> dict:
    """Отправляет команду запущенному scplay. Бросает OSError/RuntimeError при неудаче."""
    line = (command or "").strip()
    if not line:
        raise ValueError("пустая команда")
    address = control_socket_path()
    if os.name == "nt":
        with open(address, "r+b", buffering=0) as pipe:  # именованный канал
            pipe.write((line + "\n").encode("utf-8"))
            raw = pipe.readline()
    else:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(address)
            conn.sendall((line + "\n").encode("utf-8"))
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception as e:
        raise RuntimeError(f"непонятный ответ от scplay: {raw[:120]!r}") from e


def format_status(payload: dict) -> str:
    """Человекочитаемый вывод для `scplay --status`."""
    if not payload.get("ok"):
        return f"scplay не отвечает: {payload.get('error') or 'неизвестная ошибка'}"
    state = payload.get("state", "idle")
    title = payload.get("title") or "ничего не играет"
    who = payload.get("uploader") or ""
    pos, dur = payload.get("position") or 0, payload.get("duration") or 0
    lines = [
        f"{'▶' if state == 'playing' else '‖' if state == 'paused' else '■'} {title}"
        + (f" — {who}" if who else ""),
        f"состояние: {state}"
        + (f" · {int(pos // 60)}:{int(pos % 60):02d} / {int(dur // 60)}:{int(dur % 60):02d}" if dur else ""),
        f"громкость: {payload.get('volume')}% · шаффл: {'вкл' if payload.get('shuffle') else 'выкл'}"
        f" · повтор: {payload.get('repeat_name')}",
        f"очередь: {payload.get('queue_index', 0)}/{payload.get('queue_len', 0)}",
    ]
    if payload.get("next_ready"):
        lines.append("следующий трек подготовлен")
    return "\n".join(lines)


# --------------------------------------------------------------- сервер


class ControlServer:
    """Принимает команды в отдельном потоке и передаёт их в поток интерфейса.

    Второй экземпляр scplay на том же сокете не поднимется: путь занят, и это
    лучше, чем два плеера, дерущиеся за одно устройство вывода.
    """

    def __init__(self, dispatch, address: str | None = None):
        """dispatch(command: str) -> dict вызывается в потоке сервера."""
        self.dispatch = dispatch
        self.address = address or control_socket_path()
        self._stop = threading.Event()
        self._srv = None
        self._thread: threading.Thread | None = None
        self._owns_socket = False

    # --- запуск/остановка
    def start(self) -> str | None:
        """Возвращает None при успехе или текст ошибки.

        При ошибке объект остаётся в состоянии «не запущен»: иначе stop() снёс бы
        сокет уже работающего экземпляра scplay и оставил бы его без управления.
        """
        self._owns_socket = False
        if os.name == "nt":
            return self._start_pipe()
        try:
            if os.path.exists(self.address):
                # чужой живой сокет? пробуем достучаться, иначе сносим протухший
                if self._alive():
                    return f"scplay уже запущен (сокет {self.address})"
                os.remove(self.address)
            self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._srv.bind(self.address)
            os.chmod(self.address, 0o600)
            self._srv.listen(4)
            self._srv.settimeout(0.5)
        except OSError as e:
            self._close()
            return f"не удалось открыть канал управления: {e}"
        self._owns_socket = True
        self._thread = threading.Thread(target=self._serve, name="control-server", daemon=True)
        self._thread.start()
        _log(f"канал управления: {self.address}")
        return None

    def _alive(self) -> bool:
        """Есть ли по этому адресу работающий канал?

        Одного успешного connect мало: файл сокета может остаться от убитого
        процесса, тогда connect проходит, а отвечать некому — и scplay отказывался
        поднимать канал из-за «уже запущенного» плеера, которого нет.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                probe.connect(self.address)
                probe.sendall(b"ping\n")
                raw = b""
                while not raw.endswith(b"\n") and len(raw) < 4096:
                    chunk = probe.recv(1024)
                    if not chunk:
                        break
                    raw += chunk
                reply = json.loads(raw.decode("utf-8") or "{}")
                return bool(reply.get("pong"))
        except (OSError, ValueError):
            return False

    def _start_pipe(self) -> str | None:
        # На Windows один поток ждёт подключения через WaitNamedPipe-совместимый open.
        self._thread = threading.Thread(target=self._serve_pipe, name="control-server", daemon=True)
        self._thread.start()
        _log(f"канал управления: {self.address}")
        return None

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if os.name != "nt" and getattr(self, "_owns_socket", False):
            try:
                if os.path.exists(self.address):
                    os.remove(self.address)
            except OSError:
                pass
        self._owns_socket = False

    def _close(self) -> None:
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass
        self._srv = None

    # --- обслуживание
    #: Уровень/опция для получения pid клиента. Именованных констант в Python
    #: на macOS нет, поэтому берём числовые значения из <sys/un.h>.
    _MACOS_LOCAL_PEERPID = (0, 2, 4)

    def _peer_ok(self, conn: socket.socket) -> bool:
        """Только тот же пользователь: в канал нельзя пускать соседей по машине.

        Сама проверка вторична — сокет уже лежит в каталоге 0700 с правами 0600,
        — поэтому при любой неясности пропускаем.
        """
        if os.name == "nt" or not hasattr(os, "getuid"):
            return True
        try:
            if sys.platform == "darwin":
                level, opt, size = self._MACOS_LOCAL_PEERPID
                pid = struct.unpack_from("i", conn.getsockopt(level, opt, size))[0]
                if pid <= 1:
                    return True  # система не отдала pid — не мешаем CLI
                import subprocess as _sp

                out = _sp.run(["ps", "-o", "uid=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=3).stdout.strip()
                return not out or int(out) == os.getuid()
            raw = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
            return struct.unpack_from("III", raw)[0] == os.getuid()
        except (OSError, ValueError, struct.error) as e:
            # сокет уже защищён правами 0600 в каталоге 0700, поэтому непонятная
            # ошибка проверки не должна ломать управление
            _log(f"проверка клиента сокета пропущена: {e!r}")
            return True

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(3.0)
                if not self._peer_ok(conn):
                    _log("канал управления: отклонил клиента другого пользователя")
                    continue
                try:
                    raw = b""
                    while not raw.endswith(b"\n") and len(raw) < 4096:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        raw += chunk
                    reply = self._handle(raw.decode("utf-8", "replace").strip())
                except Exception as e:
                    reply = {"ok": False, "error": f"ошибка обработки: {e}"}
                try:
                    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def _serve_pipe(self) -> None:  # pragma: no cover - только Windows
        import ctypes
        from ctypes import wintypes

        PIPE_ACCESS_DUPLEX = 0x3
        PIPE_TYPE_MESSAGE = 0x4
        PIPE_READMODE_MESSAGE = 0x2
        PIPE_UNLIMITED_INSTANCES = 255
        k32 = ctypes.windll.kernel32
        while not self._stop.is_set():
            handle = k32.CreateNamedPipeW(
                self.address, PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE,
                PIPE_UNLIMITED_INSTANCES, 4096, 4096, 0, None,
            )
            if handle == -1:
                _log("не удалось создать именованный канал")
                return
            try:
                if not k32.ConnectNamedPipe(handle, None):
                    if k32.GetLastError() != 535:  # ERROR_PIPE_CONNECTED
                        continue
                buf = ctypes.create_string_buffer(4096)
                read = wintypes.DWORD()
                if k32.ReadFile(handle, buf, 4095, ctypes.byref(read), None):
                    reply = self._handle(buf.value.decode("utf-8", "replace").strip())
                else:
                    reply = {"ok": False, "error": "не удалось прочитать команду"}
                data = (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
                written = wintypes.DWORD()
                k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
                k32.FlushFileBuffers(handle)
            finally:
                k32.DisconnectNamedPipe(handle)
                k32.CloseHandle(handle)

    def _handle(self, line: str) -> dict:
        if not line:
            return {"ok": False, "error": "пустая команда"}
        if line.split()[0].lower() == "ping":
            # на этот запрос отвечает только живой процесс scplay: по нему
            # start() отличает работающий плеер от протухшего файла сокета
            return {"ok": True, "pong": True}
        try:
            return self.dispatch(line)
        except Exception as e:
            _log(f"команда {line!r} упала: {e!r}")
            return {"ok": False, "error": str(e)}


# --------------------------------------------------------------- медиа-сессии


class MediaMedia:
    """Общий интерфейс медиа-сессии (MPRIS / Пункт управления macOS).

    Все методы вызываются из потока интерфейса. Ошибки внутри не должны
    всплывать наружу: медиа-сессия — необязательная надстройка.
    """

    name = "нет"

    def start(self) -> str | None:
        """Возвращает None при успехе или текст ошибки (для лога)."""
        return None

    def stop(self) -> None:
        pass

    def set_track(self, title: str, artist: str, album: str, duration: float) -> None:
        pass

    def set_state(self, playing: bool, paused: bool) -> None:
        pass

    def set_position(self, position: float) -> None:
        pass


class NullMedia(MediaMedia):
    """Заглушка: медиа-сессия выключена или недоступна."""

    name = "выключено"


# --------------------------------------------------------------- macOS


class MacNowPlaying(MediaMedia):
    """«Сейчас играет» в Пункте управления macOS + медиа-клавиши.

    Реализовано через MediaPlayer.framework вызовами objc_msgSend (ctypes).
    Важная тонкость: методы MPNowPlayingInfoCenter нельзя вызывать из рабочих
    потоков — фреймворк бросает NSException, который ctypes не перехватывает и
    процесс падает. Поэтому все вызовы идут через :meth:`set_track` и т.п.,
    а приложение дёргает их только из потока интерфейса.
    """

    name = "macOS Now Playing"

    def __init__(self, on_command=None, on_position=None):
        """on_command(action: str) — пауза/дальше/назад; on_position(sec) — перемотка."""
        self.on_command = on_command
        self.on_position = on_position
        self._libs: list = []
        self._objc = None
        self._center = None
        self._commands = None
        self._blocks: list = []
        self._bufs: list = []
        self._title = ""
        self._artist = ""
        self._album = ""
        self._duration = 0.0
        self._position = 0.0
        self._playing = False
        self._last_position = -10.0

    # --- инфраструктура ctypes
    def _load(self) -> None:
        """Загружает фреймворки и готовит указатели objc_msgSend.

        objc_msgSend — вариадическая функция: типы аргументов не описаны в символе,
        и вызов её через обычный CFuncPtr в ctypes портит регистры (проверено:
        SIGSEGV). Поэтому берём адрес и приводим его к указателю конкретной
        сигнатуры — так же делает PyObjC.
        """
        import ctypes
        import ctypes.util

        for framework in ("MediaPlayer", "Foundation", "CoreFoundation"):
            path = ctypes.util.find_library(framework)
            if not path:
                raise RuntimeError(f"нет фреймворка {framework}")
            self._libs.append(ctypes.CDLL(path))
        objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        self._libs.append(objc)
        self._objc = objc
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        if not addr:
            raise RuntimeError("objc_msgSend не найден")
        ptr = ctypes.c_void_p
        # по одной сигнатуре на каждую форму вызова: так исключены ошибки ABI
        self._send0 = ctypes.CFUNCTYPE(ptr, ptr, ptr)(addr)
        self._send1 = ctypes.CFUNCTYPE(ptr, ptr, ptr, ptr)(addr)
        self._send2 = ctypes.CFUNCTYPE(ptr, ptr, ptr, ptr, ptr)(addr)
        self._send_dbl = ctypes.CFUNCTYPE(ctypes.c_double, ptr, ptr)(addr)
        # _NSConcreteStackBlock — глобальная переменная: чтобы блок был настоящим,
        # в его isa нужен адрес самой переменной, а не значение указателя
        self._stack_block_isa = ctypes.cast(
            ctypes.CDLL(None).__getattr__("_NSConcreteStackBlock"), ctypes.c_void_p
        ).value
        self._block_invoke_t = ctypes.CFUNCTYPE(ptr, ptr, ptr)
        self._send_block_fn = ctypes.CFUNCTYPE(ptr, ptr, ptr, ptr)(addr)
        for name in ("MPNowPlayingInfoCenter", "NSMutableDictionary", "NSString", "NSNumber"):
            if not self._cls(name):
                raise RuntimeError(f"нет класса {name}")

    def _make_block(self, func) -> tuple:
        """Собирает настоящий Objective-C блок вокруг питоновской функции.

        Подделать указатель нельзя: objc_msgSend ожидает объект блока с isa и
        invoke, иначе SIGSEGV (проверено). Возвращает (указатель на блок,
        объекты, которые нужно удержать от сборки мусора).

        `func` получает (block, event): первый аргумент invoke — сам блок.
        """
        import ctypes

        invoke = self._block_invoke_t(func)

        class Block(ctypes.Structure):
            _fields_ = [
                ("isa", ctypes.c_void_p),
                ("flags", ctypes.c_int),
                ("reserved", ctypes.c_int),
                ("invoke", self._block_invoke_t),
                ("descriptor", ctypes.c_void_p),
            ]

        class Descriptor(ctypes.Structure):
            _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]

        descriptor = Descriptor(0, ctypes.sizeof(Block))
        block = Block(
            isa=self._stack_block_isa,
            flags=0,
            reserved=0,
            invoke=invoke,
            descriptor=ctypes.cast(ctypes.pointer(descriptor), ctypes.c_void_p),
        )
        keep = (block, descriptor, invoke)
        return (ctypes.cast(ctypes.pointer(block), ctypes.c_void_p), keep)

    def _cls(self, name: str):
        return self._objc.objc_getClass(name.encode())

    def _sel(self, name: str):
        return self._objc.sel_registerName(name.encode())

    def _send_int(self, obj, selector: str, *args):
        """Метод, возвращающий целое/указатель (большинство методов NSObject)."""
        import ctypes

        sel = self._sel(selector)
        if len(args) == 0:
            return self._send0(obj, sel)
        if len(args) == 1:
            return self._send1(obj, sel, ctypes.c_void_p(args[0]))
        return self._send2(obj, sel, ctypes.c_void_p(args[0]), ctypes.c_void_p(args[1]))

    def _send_double(self, obj, selector: str):
        """Метод, возвращающий double (например NSTimeInterval у события)."""
        import ctypes

        return self._send_dbl(obj, self._sel(selector))

    def _send_block(self, obj, selector: str, block):
        """Метод с block-аргументом: addTargetWithHandler: и подобные."""
        return self._send_block_fn(obj, self._sel(selector), block)

    # Короткие синонимы для читаемости вызовов ниже
    def _send(self, obj, selector: str, *args):
        return self._send_int(obj, selector, *args)

    def _responds(self, obj, selector: str) -> bool:
        cls = self._send_int(obj, "class")
        return bool(self._send_int(cls, "instancesRespondToSelector:", self._sel(selector)))

    def _str(self, text: str):
        import ctypes

        ptr = ctypes.create_string_buffer(text.encode("utf-8"))
        self._bufs.append(ptr)  # строка должна жить до конца вызова
        if len(self._bufs) > 64:
            del self._bufs[:32]
        return self._send_int(
            self._cls("NSString"), "stringWithUTF8String:", ctypes.addressof(ptr)
        )

    def _num(self, value: float):
        return self._send_int(
            self._cls("NSNumber"),
            "numberWithDouble:",
            struct.unpack("<Q", struct.pack("<d", float(value)))[0],
        )

    def _dict(self):
        return self._send(self._cls("NSMutableDictionary"), "dictionary")

    # --- публикация
    def start(self) -> str | None:
        try:
            self._load()
            self._center = self._send(self._cls("MPNowPlayingInfoCenter"), "defaultCenter")
            if not self._center:
                return "MPNowPlayingInfoCenter недоступен"
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        self._enable_commands()
        self._publish()
        _log("macOS Now Playing: подключён")
        return None

    def _enable_commands(self) -> None:
        """Медиа-клавиши: MPRemoteCommandCenter + обработчики-блоки.

        Регистрация — единственное место, где сигнатуру селектора нельзя проверить
        заранее, поэтому каждая команда подключается обособленно: не получилось —
        останемся без этой клавиши, но с метаданными в Пункте управления.
        Вызывать только из главного потока: фреймворк кидает NSException.
        """
        try:
            if not self._cls("MPRemoteCommandCenter"):
                return
            self._commands = self._send_int(self._cls("MPRemoteCommandCenter"), "sharedCommandCenter")
            if not self._commands:
                return
            simple = {
                "togglePlayPauseCommand": "toggle",
                "playCommand": "play",
                "pauseCommand": "pause",
                "nextTrackCommand": "next",
                "previousTrackCommand": "prev",
            }
            for selector, action in simple.items():
                self._register(selector, self._command_block(action))
            self._register("changePlaybackPositionCommand", self._position_block())
        except Exception as e:
            _log(f"медиа-клавиши недоступны: {e!r}")

    def _register(self, selector: str, handler) -> None:
        try:
            cmd = self._send_int(self._commands, selector)
            if not cmd:
                return
            block_ptr, keep = self._make_block(handler)
            self._send_block(cmd, "addTargetWithHandler:", block_ptr)
            self._send_int(cmd, "setEnabled:", 1)
            self._blocks.append(keep)  # блок должен жить, пока жив mpv
        except Exception as e:
            _log(f"медиа-клавиша {selector}: {e!r}")

    def _command_block(self, action: str):
        def handler(_block, _event):
            try:
                if self.on_command:
                    self.on_command(action)
            except Exception as e:
                _log(f"медиа-команда {action}: {e!r}")
            return 0

        return handler

    def _position_block(self):
        def handler(_block, event):
            try:
                micros = self._send_double(event, "positionTime")
                if self.on_position and micros:
                    self.on_position(micros / 1_000_000.0)
            except Exception as e:
                _log(f"медиа-перемотка: {e!r}")
            return 0

        return handler

    def _publish(self) -> None:
        """Отправляет текущее состояние в систему. Только из главного потока!"""
        if not self._center:
            return
        import ctypes

        info = self._dict()
        for key, text in (
            ("title", self._title),
            ("artist", self._artist),
            ("album", self._album or "scplay"),
        ):
            if text:
                self._send(info, "setObject:forKey:", self._str(text), self._str(key))
        if self._duration:
            self._send(info, "setObject:forKey:", self._num(self._duration), self._str("playbackDuration"))
        self._send(
            info, "setObject:forKey:", self._num(max(self._position, 0.0)), self._str("elapsedPlaybackTime")
        )
        self._send(info, "setObject:forKey:", self._num(1.0 if self._playing else 0.0), self._str("playbackRate"))
        try:
            self._send(self._center, "setNowPlayingInfo:", info)
        except Exception as e:
            _log(f"Now Playing: не удалось обновить: {e!r}")

    def set_track(self, title: str, artist: str, album: str, duration: float) -> None:
        self._title, self._artist, self._album = title or "", artist or "", album or ""
        self._duration, self._position = float(duration or 0), 0.0
        self._last_position = -10.0
        self._publish()

    def set_state(self, playing: bool, paused: bool) -> None:
        self._playing = bool(playing and not paused)
        self._publish()

    def set_position(self, position: float) -> None:
        self._position = float(position or 0.0)
        if abs(self._position - self._last_position) < 1.0:
            return  # не дёргаем систему каждые 50 мс
        self._last_position = self._position
        self._publish()

    def stop(self) -> None:
        try:
            if self._center and self._responds(self._center, "setNowPlayingInfo:"):
                self._send(self._center, "setNowPlayingInfo:", None)
        except Exception as e:
            _log(f"Now Playing: не удалось очистить: {e!r}")


# --------------------------------------------------------------- MPRIS (Linux)


class MprisMedia(MediaMedia):
    """MPRIS2 для Linux: медиа-клавиши, панель в GNOME/KDE, `playerctl`.

    Требует пакет dbus-next (`pipx inject scplay dbus-next`). Всё работает в
    отдельном потоке со своим asyncio-циклом, команды уходят в интерфейс через
    on_command — тот же путь, что у канала управления.
    """

    name = "MPRIS (Linux)"
    BUS_NAME = "org.mpris.MediaPlayer2.scplay"
    OBJECT_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self, on_command=None, on_position=None):
        self.on_command = on_command
        self.on_position = on_position
        self._loop = None
        self._thread = None
        self._iface = None
        self._state = {"title": "", "artist": "", "album": "", "duration": 0.0,
                       "position": 0.0, "playing": False}

    # --- сборка интерфейсов
    def _build(self):
        from dbus_next.service import ServiceInterface, method, dbus_property, signal
        from dbus_next import Variant
        state = self._state
        outer = self

        class MediaPlayer2(ServiceInterface):
            def __init__(self):
                super().__init__("org.mpris.MediaPlayer2")

            @method()
            def Raise(self):
                return None

            @method()
            def Quit(self):
                if outer.on_command:
                    outer.on_command("quit")

            @dbus_property()
            def CanQuit(self) -> "b":
                return True

            @CanQuit.setter
            def CanQuit(self, value: "b"):
                pass

            @dbus_property()
            def CanRaise(self) -> "b":
                return False

            @CanRaise.setter
            def CanRaise(self, value: "b"):
                pass

            @dbus_property()
            def HasTrackList(self) -> "b":
                return False

            @HasTrackList.setter
            def HasTrackList(self, value: "b"):
                pass

            @dbus_property()
            def Identity(self) -> "s":
                return "scplay"

            @Identity.setter
            def Identity(self, value: "s"):
                pass

            @dbus_property()
            def DesktopEntry(self) -> "s":
                return "scplay"

            @DesktopEntry.setter
            def DesktopEntry(self, value: "s"):
                pass

            @dbus_property()
            def SupportedUriSchemes(self) -> "as":
                return []

            @SupportedUriSchemes.setter
            def SupportedUriSchemes(self, value: "as"):
                pass

            @dbus_property()
            def SupportedMimeTypes(self) -> "as":
                return ["audio/mpeg"]

            @SupportedMimeTypes.setter
            def SupportedMimeTypes(self, value: "as"):
                pass

        class Player(ServiceInterface):
            def __init__(self):
                super().__init__("org.mpris.MediaPlayer2.Player")

            def _cmd(self, action):
                if outer.on_command:
                    outer.on_command(action)

            @method()
            def Play(self):
                self._cmd("play")

            @method()
            def Pause(self):
                self._cmd("pause")

            @method()
            def PlayPause(self):
                self._cmd("toggle")

            @method()
            def Stop(self):
                self._cmd("pause")

            @method()
            def Next(self):
                self._cmd("next")

            @method()
            def Previous(self):
                self._cmd("prev")

            @method()
            def Seek(self, offset: "x"):
                if outer.on_position:
                    outer.on_position(state["position"] + offset / 1_000_000.0)

            @method()
            def SetPosition(self, track_id: "o", position: "x"):
                if outer.on_position:
                    outer.on_position(position / 1_000_000.0)

            @method()
            def OpenUri(self, uri: "s"):
                return None

            @signal()
            def Seeked(self, position: "x") -> "x":
                return position

            @dbus_property()
            def PlaybackStatus(self) -> "s":
                return "Playing" if state["playing"] else ("Paused" if state["title"] else "Stopped")

            @PlaybackStatus.setter
            def PlaybackStatus(self, value: "s"):
                pass

            @dbus_property()
            def Rate(self) -> "d":
                return 1.0

            @Rate.setter
            def Rate(self, value: "d"):
                pass

            @dbus_property()
            def MinimumRate(self) -> "d":
                return 1.0

            @MinimumRate.setter
            def MinimumRate(self, value: "d"):
                pass

            @dbus_property()
            def MaximumRate(self) -> "d":
                return 1.0

            @MaximumRate.setter
            def MaximumRate(self, value: "d"):
                pass

            @dbus_property()
            def Volume(self) -> "d":
                return 1.0

            @Volume.setter
            def Volume(self, value: "d"):
                pass

            @dbus_property()
            def Position(self) -> "x":
                return int(state["position"] * 1_000_000)

            @Position.setter
            def Position(self, value: "x"):
                pass

            @dbus_property()
            def CanGoNext(self) -> "b":
                return True

            @CanGoNext.setter
            def CanGoNext(self, value: "b"):
                pass

            @dbus_property()
            def CanGoPrevious(self) -> "b":
                return True

            @CanGoPrevious.setter
            def CanGoPrevious(self, value: "b"):
                pass

            @dbus_property()
            def CanPlay(self) -> "b":
                return True

            @CanPlay.setter
            def CanPlay(self, value: "b"):
                pass

            @dbus_property()
            def CanPause(self) -> "b":
                return True

            @CanPause.setter
            def CanPause(self, value: "b"):
                pass

            @dbus_property()
            def CanSeek(self) -> "b":
                return True

            @CanSeek.setter
            def CanSeek(self, value: "b"):
                pass

            @dbus_property()
            def CanControl(self) -> "b":
                return True

            @CanControl.setter
            def CanControl(self, value: "b"):
                pass

            @dbus_property()
            def Metadata(self) -> "a{sv}":
                track = f"/org/mpris/MediaPlayer2/track/{abs(hash(state['title'])) % 10**9}"
                out = {
                    "mpris:trackid": Variant("o", track),
                    "xesam:title": Variant("s", state["title"] or "scplay"),
                }
                if state["artist"]:
                    out["xesam:artist"] = Variant("as", [state["artist"]])
                if state["album"]:
                    out["xesam:album"] = Variant("s", state["album"])
                if state["duration"]:
                    out["mpris:length"] = Variant("x", int(state["duration"] * 1_000_000))
                return out

            @Metadata.setter
            def Metadata(self, value: "a{sv}"):
                pass

        return MediaPlayer2(), Player()

    # --- поток
    def start(self) -> str | None:
        # Проверяем совместимость до запуска потока: dbus-next ломается на импорте
        # в свежих Python (в 3.15 удалён typing.no_type_check_decorator).
        try:
            from dbus_next.service import ServiceInterface  # noqa: F401
        except ImportError as e:
            if "no_type_check_decorator" in str(e):
                return (
                    f"dbus-next несовместим с Python {sys.version_info.major}.{sys.version_info.minor} "
                    "(нужен Python ≤3.12 или свежий dbus-next)"
                )
            return "нет пакета dbus-next (pipx inject scplay dbus-next)"
        self._error = None
        try:
            self._thread = threading.Thread(target=self._run, name="mpris", daemon=True)
            self._thread.start()
            for _ in range(50):
                if self._iface is not None or not self._thread.is_alive():
                    break
                time.sleep(0.1)
            if self._iface is None:
                return self._error or "не удалось занять имя в D-Bus (нет сессии?)"
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        _log("MPRIS: интерфейс опубликован")
        return None

    def _run(self) -> None:
        import asyncio

        from dbus_next.aio import MessageBus

        async def serve():
            bus = await MessageBus().connect()
            mp2, player = self._build()
            bus.export(self.OBJECT_PATH, mp2)
            bus.export(self.OBJECT_PATH, player)
            await bus.request_name(self.BUS_NAME)
            self._iface = player
            await bus.wait_for_disconnect()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(serve())
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            _log(f"MPRIS: поток завершился: {e!r}")

    def _emit(self, name: str, payload) -> None:
        """Уведомляет D-Bus об изменении свойства (из потока интерфейса)."""
        if not (self._iface and self._loop and self._loop.is_running()):
            return
        try:
            self._loop.call_soon_threadsafe(
                lambda: self._iface.emit_properties_changed({name: payload})
            )
        except Exception as e:
            _log(f"MPRIS: {name} не опубликован: {e!r}")

    def set_track(self, title: str, artist: str, album: str, duration: float) -> None:
        self._state.update(title=title or "", artist=artist or "", album=album or "",
                           duration=float(duration or 0), position=0.0)
        self._emit("Metadata", self._iface and self._iface.Metadata())

    def set_state(self, playing: bool, paused: bool) -> None:
        self._state["playing"] = bool(playing and not paused)
        self._emit("PlaybackStatus", "Playing" if self._state["playing"] else "Paused")

    def set_position(self, position: float) -> None:
        self._state["position"] = float(position or 0.0)

    def stop(self) -> None:
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


def make_media(on_command=None, on_position=None, force: str | None = None) -> MediaMedia:
    """Выбирает медиа-сессию по платформе. force: "macos" | "mpris" | "off"."""
    choice = (force or os.environ.get("SCPLAY_MEDIA", "") or "").strip().lower()
    candidates: list[MediaMedia] = []
    if choice in ("0", "off", "none", "no"):
        return NullMedia()
    if choice == "macos" or (not choice and sys.platform == "darwin"):
        candidates.append(MacNowPlaying(on_command, on_position))
    if choice == "mpris" or (not choice and sys.platform.startswith("linux")):
        candidates.append(MprisMedia(on_command, on_position))
    if not candidates:
        return NullMedia()
    for backend in candidates:
        error = backend.start()
        if error is None:
            return backend
        _log(f"{backend.name}: {error}")
    return NullMedia()


# --------------------------------------------------------------- CLI


def run_cli(argv: list[str]) -> int:
    """`scplay --send ...` и `scplay --status`."""
    def fail(text: str, code: int = 2) -> int:
        print(f"scplay: {text}", file=sys.stderr)
        return code

    if not argv:
        print("использование: scplay --send <команда> | scplay --status", file=sys.stderr)
        for name, help_text in COMMANDS.items():
            print(f"  {name:14} {help_text}", file=sys.stderr)
        return 2
    command = " ".join(argv)
    if command in ("help", "-h", "--help"):
        for name, help_text in COMMANDS.items():
            print(f"  {name:14} {help_text}")
        return 0
    try:
        reply = send_command(command)
    except (OSError, ValueError, RuntimeError) as e:
        return fail(f"не удалось связаться с запущенным scplay ({e}). Он запущен?", 1)
    if command.split()[0] == "status" or not reply.get("ok"):
        print(format_status(reply))
        return 0 if reply.get("ok") else 1
    if reply.get("message"):
        print(reply["message"])
    elif reply.get("result") is not None:
        print(json.dumps(reply["result"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_cli(sys.argv[1:]))
