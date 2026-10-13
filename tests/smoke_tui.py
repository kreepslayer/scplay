#!/usr/bin/env python3
"""Живая проверка scplay: приложение поднимает mpv, играет локальный файл,
затем корректно всё останавливает и убирает IPC-сокет.

Запуск (из корня проекта):  ./venv/bin/python tests/smoke_tui.py
Требует mpv и ffmpeg в PATH. Реальный SoundCloud не нужен.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import ROOT, ensure_deps  # noqa: E402

ensure_deps()

spec = importlib.util.spec_from_file_location("scp", ROOT / "scplay.py")
scp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scp)

# Тест не должен трогать рабочий ~/.config/scplay: уводим все пути в temp
SANDBOX = Path(tempfile.mkdtemp(prefix="scplay-smoke-"))
scp.CONFIG_DIR = SANDBOX
scp.CONFIG_PATH = SANDBOX / "config.json"
scp.MPV_LOG = SANDBOX / "mpv.log"
scp.DEBUG_LOG = SANDBOX / "debug.log"
scp.LYRICS_DIR = SANDBOX / "lyrics"
scp.save_cfg({"volume": 25, "shuffle": False, "repeat": 0, "theme": "mocha"})

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_wav(path: Path, seconds: int = 12) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=330:duration={seconds}",
            "-ar", "16000", "-ac", "1", str(path),
        ],
        check=True,
        timeout=60,
    )


async def main() -> int:
    wav = Path(tempfile.gettempdir()) / f"scplay_smoke_{os.getpid()}.wav"
    make_wav(wav)
    print(f"локальный файл: {wav}")

    app = scp.ScPlay(str(wav))
    async with app.run_test(size=(100, 32)) as pilot:
        print("\n[старт]")
        for _ in range(100):
            await pilot.pause(0.1)
            if app.mpv is not None or app.mpv_error:
                break
        check("mpv запущен", app.mpv is not None, app.mpv_error or f"режим {app.mpv_profile!r}")
        check("mpv_proc жив", bool(app.mpv_proc and app.mpv_proc.poll() is None))
        check("watchdog-поток создан", any(t.name == "mpv-watchdog" for t in __import__("threading").enumerate()))

        for _ in range(120):
            await pilot.pause(0.1)
            if app.state == "playing" and app.started:
                break
        check("воспроизведение пошло", app.state == "playing", f"state={app.state}")
        check("длительность известна", app.duration > 0, f"{app.duration:.1f} с")

        for _ in range(60):
            await pilot.pause(0.1)
            if app.analyzer and app.analyzer.nbd > 100:
                break
        check("анализатор декодирует", bool(app.analyzer and app.analyzer.nbd > 100),
              f"блоков огибающей: {app.analyzer.nbd if app.analyzer else 0}")
        if app.analyzer:
            check("PCM ограничен кольцом", app.analyzer.cap == scp.ANALYZER_WINDOW_SEC * scp.SR,
                  f"{app.analyzer.cap} сэмплов ({(app.analyzer.cap * 2) / 1e6:.1f} МБ)")

        spec_bars = app.bars_for(80)
        lv = app.analyzer.spectrum(app.position(), spec_bars) if app.analyzer else None
        check("спектр считается", lv is not None and len(lv) == spec_bars)
        wave = app.analyzer.wave(60, app.duration) if app.analyzer else None
        check("волна считается", wave is not None and float(wave.max()) > 0)

        print("\n[управление]")
        app.action_pause()
        await pilot.pause(0.3)
        check("пауза", app.paused is True)
        app.action_pause()
        await pilot.pause(0.3)
        check("снятие паузы", app.paused is False)

        app.action_seek(4)
        await pilot.pause(0.4)
        check("перемотка", app.position() > 2.0, f"позиция {app.position():.1f} с")

        vol = app.volume
        app.action_volume(-5)
        await pilot.pause(0.2)
        check("громкость", app.volume == vol - 5, f"{vol} -> {app.volume}")
        check("громкость сохранена в конфиг", scp.load_cfg().get("volume") == app.volume)

        app.action_shuffle()
        await pilot.pause(0.2)
        check("шаффл сохранён", scp.load_cfg().get("shuffle") == app.shuffle)

        app.action_view(scp.VIEWS.index("lyrics"))
        await pilot.pause(0.3)
        check("вкладка текста открылась", app.view == "lyrics")
        app.action_lyrics()
        await pilot.pause(0.3)
        check("возврат из текста на поиск", app.view == "search", f"view={app.view}")

        print("\n[управление извне: канал]")
        import scplay_media as media

        check("канал управления поднят", app.control is not None and os.path.exists(media.control_socket_path()),
              media.control_socket_path())
        st = await asyncio.to_thread(media.send_command, "status")
        check("status отвечает", st.get("ok") is True and st.get("title"), str(st.get("title")))
        check("status видит воспроизведение", st.get("state") == "playing", str(st.get("state")))

        await asyncio.to_thread(media.send_command, "pause")
        await pilot.pause(0.4)
        check("pause извне", app.paused is True)
        st = await asyncio.to_thread(media.send_command, "status")
        check("status показывает паузу", st.get("state") == "paused", str(st.get("state")))
        await asyncio.to_thread(media.send_command, "toggle")
        await pilot.pause(0.4)
        check("toggle извне снимает паузу", app.paused is False)

        await asyncio.to_thread(media.send_command, "seek 6")
        await pilot.pause(0.4)
        check("seek извне", app.position() > 4.0, f"позиция {app.position():.1f} с")

        before = app.volume
        await asyncio.to_thread(media.send_command, "volume-rel -5")
        await pilot.pause(0.3)
        check("volume-rel извне", app.volume == before - 5, f"{before} -> {app.volume}")

        q = await asyncio.to_thread(media.send_command, "queue")
        check("queue отдаёт список", isinstance(q.get("result"), list) and q["result"],
              f"треков: {len(q.get('result') or [])}")

        bad = await asyncio.to_thread(media.send_command, "нет-такой-команды")
        check("неизвестная команда отклоняется", bad.get("ok") is False, str(bad.get("error"))[:60])

        check("формат --status читаемый", media.format_status(st).splitlines()[0].startswith(("▶", "‖", "■")))

        sock = app._sock
        proc = app.mpv_proc
        app.shutdown()
        await pilot.pause(0.5)
        check("mpv остановлен", proc is None or proc.poll() is not None,
              f"rc={proc.poll() if proc else None}")
        check("IPC-сокет удалён", not sock or not os.path.exists(sock), sock or "")
        check("канал управления закрыт", not os.path.exists(media.control_socket_path()))
        check("shutdown идемпотентен", (app.shutdown() or True) is True)

    wav.unlink(missing_ok=True)
    print("\n" + ("ВСЁ ОК" if not FAILS else f"ПРОВАЛЕНО: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
