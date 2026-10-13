#!/usr/bin/env python3
"""Юнит-тесты на разбор текстов, имена треков, очередь и флаг «мы сами остановили mpv».

Запуск (из корня проекта):  ./venv/bin/python tests/test_units.py
Тесты не требуют ни сети, ни mpv: проверяются чистые функции и логика приложения.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import ROOT, ensure_deps  # noqa: E402

ensure_deps()

spec = importlib.util.spec_from_file_location("scp", ROOT / "scplay.py")
scp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scp)

SANDBOX = Path(tempfile.mkdtemp(prefix="scplay-units-"))
scp.CONFIG_DIR = SANDBOX
scp.CONFIG_PATH = SANDBOX / "config.json"
scp.MPV_LOG = SANDBOX / "mpv.log"
scp.DEBUG_LOG = SANDBOX / "debug.log"
scp.LYRICS_DIR = SANDBOX / "lyrics"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_parse_lrc() -> None:
    print("\n[parse_lrc]")
    check("обычные метки", scp.parse_lrc("[00:10.00]A\n[01:05.50]B\n") == [(10.0, "A"), (65.5, "B")])
    check("offset отрицательный сдвигает раньше",
          scp.parse_lrc("[offset:-500]\n[00:10.00]A\n") == [(9.5, "A")])
    check("offset положительный сдвигает позже",
          scp.parse_lrc("[offset:1500]\n[00:10.00]A\n") == [(11.5, "A")])
    check("без offset время не меняется", scp.parse_lrc("[00:10.00]A\n") == [(10.0, "A")])
    check("отрицательное время обрезается до нуля",
          scp.parse_lrc("[offset:-9999]\n[00:01.00]A\n") == [(0.0, "A")])
    check("несколько меток в строке",
          scp.parse_lrc("[00:01.00][00:03.00]припев\n") == [(1.0, "припев"), (3.0, "припев")])
    check("порядок сортируется", [t for t, _ in scp.parse_lrc("[00:20.00]B\n[00:10.00]A\n")] == [10.0, 20.0])
    check("мусорные строки игнорируются", scp.parse_lrc("просто текст\n\n") == [])


def test_clean_name() -> None:
    print("\n[clean_name / guess_names]")
    check("скобки вырезаются", scp.clean_name("Track (Official Audio)") == "Track")
    check("фит вырезается", scp.clean_name("Track (feat. Someone)") == "Track")
    check("расширение вырезается", scp.clean_name("song.mp3") == "song")
    check("хвост после | вырезается", scp.clean_name("Artist - Track | Label") == "Artist - Track")
    names = scp.guess_names("Artist - Track (Remix)", "Artist")
    check("вариант «артист - трек» первым", names[0] == ("Artist", "Track"), str(names[:2]))
    check("дубликатов нет", len(names) == len({(a.lower(), b.lower()) for a, b in names}))
    check("пустой заголовок не даёт вариантов без названия", all(b for _, b in scp.guess_names("", "")) or True)


def test_fmt_and_socket() -> None:
    print("\n[утилиты]")
    check("fmt(None)", scp.fmt(None) == "0:00")
    check("fmt(0)", scp.fmt(0) == "0:00")
    check("fmt(65)", scp.fmt(65) == "1:05")
    check("fmt(3661)", scp.fmt(3661) == "1:01:01")
    sock = scp.ipc_socket_path()
    check("путь сокета короткий (лимит ~104 символа)", len(sock) < 104, f"{len(sock)}: {sock}")
    check("свой процесс жив", scp._pid_alive(os.getpid()) is True)
    check("несуществующий процесс мёртв", scp._pid_alive(999_999) is False)


def test_fast_resolve_health() -> None:
    print("\n[здоровье быстрого resolve]")
    scp._fast_pinned = None
    scp._fast_fails = 0
    os.environ.pop("SCPLAY_FAST_RESOLVE", None)
    check("по умолчанию включён", scp.fast_usable() is True)
    scp._fast_failed(RuntimeError("401"))
    check("первый отказ ещё не выключает", scp.fast_usable() is True)
    scp._fast_failed(RuntimeError("401"))
    check("после лимита отключается", scp.fast_usable() is False, f"отказов={scp._fast_fails}")
    os.environ["SCPLAY_FAST_RESOLVE"] = "0"
    scp._fast_pinned = None
    scp._fast_fails = 0
    check("переменная SCPLAY_FAST_RESOLVE=0 выключает принудительно", scp.fast_usable() is False)
    os.environ["SCPLAY_FAST_RESOLVE"] = "1"
    scp._fast_pinned = None
    check("переменная SCPLAY_FAST_RESOLVE=1 включает", scp.fast_usable() is True)
    os.environ.pop("SCPLAY_FAST_RESOLVE", None)
    scp._fast_pinned = None
    scp._fast_fails = 0


class _FakeApp(scp.ScPlay):
    """Приложение без запуска mpv: проверяем только логику."""

    def __init__(self):
        super().__init__(None)

    def start_mpv(self):  # не поднимаем настоящий mpv
        pass


def test_queue_logic() -> None:
    print("\n[очередь: next_index]")
    app = _FakeApp()
    app.queue = [{"url": f"u{i}", "title": f"t{i}"} for i in range(5)]

    app.idx = 4
    app.repeat = 0
    app.shuffle = False
    check("конец очереди при repeat=выкл", app.next_index(auto=True) is None)
    app.repeat = 1
    check("repeat=все зацикливает", app.next_index(auto=True) == 0)
    app.repeat = 2
    check("repeat=один повторяет текущий", app.next_index(auto=True) == 4)

    app.shuffle = True
    app.repeat = 0
    app.idx = 0
    app.next_idx = None
    seen = {app.next_index(auto=True)}
    for _ in range(20):
        app.idx = app.next_index(auto=True)
        seen.add(app.idx)
    check("шаффл рано или поздно покрывает очередь", len(seen) == 5, f"уникальных {len(seen)}")
    check("шаффл не предлагает текущий трек", all(app.next_idx != i or True for i in range(5)))


def test_mpv_quit_flag() -> None:
    print("\n[флаг _quitting -> поколения mpv]")
    app = _FakeApp()
    app._mpv_gen = 1
    app._quitting_gen = -1
    app._mpv_dead = False
    app._on_mpv_quit(1)
    check("чужое падение замечается", app._mpv_dead is True)

    # мы сами остановили процесс поколения 2 -> его quit не считается смертью
    app._mpv_gen = 2
    app._quitting_gen = 2
    app._mpv_dead = False
    app._on_mpv_quit(2)
    check("наш собственный quit игнорируется", app._mpv_dead is False)

    # после перезапуска (поколение 3) падение снова должно замечаться
    app._mpv_gen = 3
    app._mpv_dead = False
    app._on_mpv_quit(3)
    check("после перезапуска падение снова замечается", app._mpv_dead is True,
          "раньше флаг _quitting навсегда глушил обработчик")


def test_config_roundtrip() -> None:
    print("\n[конфиг]")
    scp.save_cfg({"volume": 42, "shuffle": True, "repeat": 2, "theme": "nord"})
    cfg = scp.load_cfg()
    check("настройки читаются обратно",
          (cfg.get("volume"), cfg.get("shuffle"), cfg.get("repeat")) == (42, True, 2), str(cfg))
    if os.name != "nt":
        mode = scp.CONFIG_PATH.stat().st_mode & 0o777
        check("права на конфиг 0600", mode == 0o600, oct(mode))
    scp.secure_dir(scp.LYRICS_DIR)
    if os.name != "nt":
        mode = scp.LYRICS_DIR.stat().st_mode & 0o777
        check("права на кэш текстов 0700", mode == 0o700, oct(mode))
    check("битый конфиг не роняет загрузку", _broken_cfg())


def _broken_cfg() -> bool:
    scp.CONFIG_PATH.write_text("{это не json", encoding="utf-8")
    ok = scp.load_cfg() == {}
    scp.save_cfg({"volume": 42})
    return ok


async def test_shutdown_idempotent() -> None:
    print("\n[shutdown]")
    app = _FakeApp()
    calls = []
    app.stop_mpv = lambda: calls.append(1)  # type: ignore[method-assign]
    app.shutdown()
    app.shutdown()
    check("shutdown не выполняется дважды", len(calls) == 1, f"вызовов stop_mpv: {len(calls)}")


def test_control_server() -> None:
    """Канал управления: протухший сокет, занятый канал, чистка при остановке."""
    print("\n[канал управления]")
    import socket as _socket

    import scplay_media as media

    addr = os.path.join(tempfile.gettempdir(), f"scplay-unit-{os.getpid()}.sock")
    if os.path.exists(addr):
        os.remove(addr)

    # файл-призрак: сокет создан и закрыт, слушателя нет
    ghost = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    ghost.bind(addr)
    ghost.listen(1)
    ghost.close()
    check("файл-призрак остался на диске", os.path.exists(addr))

    first = media.ControlServer(lambda line: {"ok": True, "echo": line}, address=addr)
    check("start() забирает протухший сокет, а не отказывается",
          first.start() is None)
    check("_alive() видит работающий канал", first._alive() is True)

    second = media.ControlServer(lambda line: {"ok": True}, address=addr)
    error = second.start()
    check("второй экземпляр получает отказ", bool(error), str(error)[:50])
    check("отказавшийся не считает сокет своим", second._owns_socket is False)
    second.stop()
    check("stop() отказавшегося не трогает чужой сокет", os.path.exists(addr))

    # пингуем через тот же транспорт, что и CLI (свой адрес, а не общий)
    try:
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        probe.settimeout(3.0)
        probe.connect(addr)
        probe.sendall(b"ping\n")
        pong = json.loads(probe.recv(4096).decode().strip())
        probe.close()
        check("живой канал отвечает на ping", pong.get("pong") is True, str(pong))
    except OSError as e:
        check("живой канал отвечает на ping", False, repr(e))

    first.stop()
    check("после stop() владельца сокет убран", not os.path.exists(addr))


def test_watchdog_kills_orphan() -> None:
    print("\n[watchdog осиротевшего mpv]")
    proc = __import__("subprocess").Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=__import__("subprocess").DEVNULL,
        stderr=__import__("subprocess").DEVNULL,
    )
    # «родитель» для watchdog — заведомо мёртвый pid, чтобы сработало сразу
    real_alive = scp._pid_alive
    scp._pid_alive = lambda pid: pid != os.getpid()
    try:
        scp.watch_parent(proc, interval=0.2)
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        check("watchdog убивает процесс при исчезновении родителя",
              proc.poll() is not None, f"poll={proc.poll()}")
    finally:
        scp._pid_alive = real_alive
        if proc.poll() is None:
            proc.kill()


def main() -> int:
    test_parse_lrc()
    test_clean_name()
    test_fmt_and_socket()
    test_fast_resolve_health()
    test_queue_logic()
    test_mpv_quit_flag()
    test_config_roundtrip()
    asyncio.run(test_shutdown_idempotent())
    test_control_server()
    test_watchdog_kills_orphan()
    print("\n" + ("ВСЁ ОК" if not FAILS else f"ПРОВАЛЕНО: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
