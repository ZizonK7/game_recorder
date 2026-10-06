"""앱 진입점."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_DISPLAY_NAME, APP_NAME
from .paths import app_data_dir


def setup_logging() -> None:
    log_file = app_data_dir() / "lolrec.log"
    handlers: list[logging.Handler] = [RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3,
                                                           encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")


def lower_own_priority() -> None:
    """앱 자체도 게임보다 낮은 우선순위로 실행."""
    try:
        import psutil

        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(5)
    except Exception:
        pass


def main() -> int:
    setup_logging()
    log = logging.getLogger("lolrec")
    lower_own_priority()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .config import load_settings, save_settings
    from .paths import ffmpeg_path
    from .storage import Storage
    from .ui import theme
    from .ui.main_window import MainWindow, screen_pixel_size

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app)

    # 중복 실행 방지: 이미 실행 중이면 기존 창을 띄우고 종료
    sock = QLocalSocket()
    sock.connectToServer(APP_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(APP_NAME)
    server = QLocalServer()
    server.listen(APP_NAME)

    settings = load_settings()
    data_dir = settings.raw_data_path
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(app_data_dir() / "games.db", data_dir)
    # 저장 위치를 바꾼 적이 있으면 예전 위치에 남은 원본 데이터를 옮겨 온다.
    # 옮기지 못한 위치는 storage.fallback_dirs 에 남아 계속 읽히고, 다음 실행 때 다시 시도한다.
    old_dirs = [Path(d) for d in settings.raw_data_fallbacks]
    if settings.raw_data_dir:
        old_dirs.append(Path(settings.raw_data_dir))
    old_dirs += [Path(g.folder).parent / "data" for g in storage.list_games() if g.folder]
    for old in dict.fromkeys(old_dirs):
        try:
            storage.import_data_from(old)
        except Exception:
            log.exception("예전 원본 데이터 이관 실패: %s", old)
            storage.fallback_dirs.append(old)
    fallbacks = [str(d) for d in storage.fallback_dirs]
    if settings.raw_data_dir != str(data_dir) or settings.raw_data_fallbacks != fallbacks:
        settings.raw_data_dir = str(data_dir)
        settings.raw_data_fallbacks = fallbacks
        save_settings(settings)

    size = screen_pixel_size(settings.monitor_index)
    log.info("녹화 모니터 %d: %dx%d", settings.monitor_index, *size)

    window = MainWindow(settings, storage, size)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window.show_normal()))

    if ffmpeg_path() is None:
        QMessageBox.warning(window, APP_DISPLAY_NAME, "ffmpeg 를 찾을 수 없어 녹화를 할 수 없습니다.\n"
                                                      "프로그램을 다시 설치해 주세요.")
    if not (settings.start_minimized or "--minimized" in sys.argv):
        window.show()
    code = app.exec()
    if window.shutdown_clean:
        storage.close()
    else:
        # 강제 종료 등으로 아직 DB 를 쓰는 작업이 있을 수 있다. 매 변경마다 commit 하므로
        # 닫지 않고 끝내도 데이터는 남는다 (닫으면 남은 작업이 오류를 낸다).
        log.warning("백그라운드 작업이 끝나지 않은 상태로 종료 - DB 연결을 닫지 않음")
    return code


if __name__ == "__main__":
    sys.exit(main())
