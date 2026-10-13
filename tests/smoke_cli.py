#!/usr/bin/env python3
"""Кросс-процессная проверка: `scplay --status`, `scplay --send ...` и выход по команде.

Приложение работает в главном потоке (Textual этого требует), а команды шлются
отдельными процессами — ровно так, как это делает пользователь.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import ROOT, ensure_deps  # noqa: E402

ensure_deps()


SANDBOX = Path(tempfile.mkdtemp(prefix="scplay-cli-"))
os.environ["SCPLAY_MEDIA"] = "0"  # медиа-сессия проверяется в smoke_tui.py

import scplay as scp  # noqa: E402
import scplay_media as media  # noqa: E402

scp.CONFIG_DIR = SANDBOX
scp.CONFIG_PATH = SANDBOX / "config.json"
scp.MPV_LOG = SANDBOX / "mpv.log"
scp.DEBUG_LOG = SANDBOX / "debug.log"
scp.LYRICS_DIR = SANDBOX / "lyrics"
scp.save_cfg({"volume": 30, "shuffle": False, "repeat": 0, "theme": "mocha"})

FAILS: list[str] = []
mpv_pid: int | None = None


REPORT = Path(tempfile.gettempdir()) / "scplay-cli-report.txt"
QUIT_AT = Path(tempfile.gettempdir()) / "scplay-cli-quit-at.txt"
REPORT.write_text("", encoding="utf-8")


def report(text: str) -> None:
    """Интерфейс Textual занимает stdout и stderr, поэтому пишем отчёт в файл."""
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def check(name: str, ok: bool, detail: str = "") -> None:
    report(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def cli(*args: str, timeout: float = 25.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scplay.py"), *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_state(want: str, seconds: float = 60.0):
    deadline = time.time() + seconds
    state = None
    while time.time() < deadline:
        try:
            state = media.send_command("status")
        except OSError:
            state = None
        if state and state.get("state") == want:
            return state
        time.sleep(0.4)
    return state


def run_checks(app: "scp.ScPlay") -> None:
    global mpv_pid
    time.sleep(1.0)  # даём on_mount доехать
    state = wait_for_state("playing")
    mpv_pid = app.mpv_proc.pid if app.mpv_proc else None
    check("приложение играет", bool(state and state.get("state") == "playing"),
          str(state and state.get("title")))
    if not (state and state.get("state") == "playing"):
        app.call_from_thread(app.action_quit_app)
        return

    report("")
    report("[CLI отдельными процессами]")
    r = cli("--status")
    check("--status вернул 0", r.returncode == 0, f"rc={r.returncode}")
    first = (r.stdout or r.stderr).splitlines()[0][:70]
    check("--status показал трек", "▶" in (r.stdout or "") or "‖" in (r.stdout or ""), first)

    r = cli("--send", "volume-rel", "-5")
    check("--send volume-rel вернул 0", r.returncode == 0, f"rc={r.returncode} {r.stderr.strip()[:60]}")
    st = media.send_command("status")
    check("громкость изменилась через CLI", st.get("volume") == 25,
          f"volume={st.get('volume')} (было 30, ждали 25)")

    r = cli("--send", "shuffle")
    check("--send shuffle принят", r.returncode == 0, f"rc={r.returncode}")
    r = cli("--send", "next")
    check("--send next принят", r.returncode == 0, f"rc={r.returncode}")

    r = cli("--send", "help")
    check("--send help печатает список команд", "toggle" in (r.stdout or "") and "volume" in (r.stdout or ""))

    report("")
    report("[выход по команде]")
    QUIT_AT.write_text(str(time.time()), encoding="utf-8")
    r = cli("--send", "quit")
    check("quit принят", r.returncode == 0, f"rc={r.returncode} {r.stderr.strip()[:60]}")


def main() -> int:
    # локальный файл: проверяем именно управление извне, без зависимости от сети
    wav = Path(tempfile.gettempdir()) / f"scplay_cli_{os.getpid()}.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
         "-ar", "16000", "-ac", "1", str(wav)],
        check=True, timeout=60,
    )
    app = scp.ScPlay(str(wav))
    sys.stdout = open(os.devnull, "w")  # интерфейс печатает в stdout, для теста он не нужен
    global mpv_pid
    worker = threading.Thread(target=run_checks, args=(app,), name="checks", daemon=True)
    watchdog = threading.Timer(180.0, lambda: app.call_from_thread(app.action_quit_app))
    watchdog.daemon = True

    def on_mount() -> None:
        log = open(Path(tempfile.gettempdir()) / "scplay-cli-test.log", "w")
        sys.stdout = log
        sys.stderr = log
        print(f"тест начался, сокет {media.control_socket_path()}", flush=True)
        worker.start()
        watchdog.start()

    app.call_after_refresh(on_mount)
    app.run()  # главный поток: Textual требует его для обработки сигналов
    app.shutdown()
    Path(tempfile.gettempdir()).joinpath(f"scplay_cli_{os.getpid()}.wav").unlink(missing_ok=True)

    if QUIT_AT.exists():
        took = time.time() - float(QUIT_AT.read_text())
        check("приложение вышло по команде quit", took < 20, f"run() вернулся через {took:.1f} с")
    else:
        check("приложение вышло по команде quit", False, "quit не был отправлен")
    check("канал управления убран", not os.path.exists(media.control_socket_path()))
    check("конфиг сохранён при выходе по команде", scp.load_cfg().get("shuffle") is True, str(scp.load_cfg()))
    time.sleep(1.0)
    check("mpv остановлен", mpv_pid is None or not _alive(mpv_pid),
          f"pid={mpv_pid}")

    report("")
    report("ВСЁ ОК" if not FAILS else f"ПРОВАЛЕНО: {FAILS}")
    print(f"отчёт: {REPORT}", file=sys.stderr)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
