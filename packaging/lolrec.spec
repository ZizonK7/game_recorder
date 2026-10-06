# PyInstaller 빌드 설정 (pyinstaller packaging/lolrec.spec)
# vendor/ 폴더에 ffmpeg.exe, ffprobe.exe, lol_audio_capture.exe 가 있어야 한다 (CI 에서 준비).
from pathlib import Path

ROOT = Path(SPECPATH).parent
VENDOR = ROOT / "vendor"

binaries = [(str(VENDOR / name), "bin") for name in ("ffmpeg.exe", "ffprobe.exe", "lol_audio_capture.exe")
            if (VENDOR / name).exists()]
# shared FFmpeg 빌드인 경우 필요한 DLL
binaries += [(str(dll), "bin") for dll in VENDOR.glob("*.dll")]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[],
    hiddenimports=["keyring.backends.Windows", "win32ctypes.core"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick3D", "PySide6.QtPdf", "PySide6.QtDesigner"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LoLRecorder",
    console=False,
    icon=str(ROOT / "packaging" / "icon.ico") if (ROOT / "packaging" / "icon.ico").exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="LoLRecorder")
