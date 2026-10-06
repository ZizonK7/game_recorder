"""설정 창."""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import ENCODERS, RESOLUTIONS, Settings, get_api_key, set_api_key
from ..riot.api import PLATFORMS, RiotApi

ENCODER_NAMES = {"auto": "자동 (권장)", "nvenc": "NVIDIA (NVENC)", "amf": "AMD (AMF)",
                 "qsv": "Intel (QuickSync)", "x264": "CPU (x264, 느림)"}
POSITIONS = {"top-center": "위 가운데", "top-left": "위 왼쪽", "top-right": "위 오른쪽",
             "bottom-left": "아래 왼쪽", "bottom-right": "아래 오른쪽"}


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.setMinimumWidth(560)
        self.s = settings
        tabs = QTabWidget()
        tabs.addTab(self._recording_tab(), "녹화")
        tabs.addTab(self._replay_tab(), "데스 리플레이")
        tabs.addTab(self._api_tab(), "Riot API")
        tabs.addTab(self._app_tab(), "일반")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        v = QVBoxLayout(self)
        v.addWidget(tabs)
        v.addWidget(buttons)

    # ------------------------------------------------------------------ tabs
    def _recording_tab(self) -> QWidget:
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        self.dir_edit = QLineEdit(s.recordings_dir)
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit)
        row.addWidget(browse)
        f.addRow("저장 위치", row)

        self.res = QComboBox()
        self.res.addItems(RESOLUTIONS)
        self.res.setCurrentText(s.resolution)
        f.addRow("해상도", self.res)
        self.fps = QComboBox()
        self.fps.addItems(["30", "60"])
        self.fps.setCurrentText(str(s.fps))
        f.addRow("프레임 (fps)", self.fps)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(1000, 30000)
        self.bitrate.setSingleStep(500)
        self.bitrate.setSuffix(" kbps")
        self.bitrate.setValue(s.bitrate_kbps)
        f.addRow("비트레이트", self.bitrate)
        self.size_hint = QLabel()
        self.size_hint.setObjectName("muted")
        self.bitrate.valueChanged.connect(self._update_size_hint)
        self._update_size_hint()
        f.addRow("", self.size_hint)

        self.encoder = QComboBox()
        for e in ENCODERS:
            self.encoder.addItem(ENCODER_NAMES[e], e)
        self.encoder.setCurrentIndex(ENCODERS.index(s.encoder) if s.encoder in ENCODERS else 0)
        f.addRow("인코더", self.encoder)

        self.monitor = QComboBox()
        for i, sc in enumerate(QGuiApplication.screens()):
            g = sc.geometry()
            self.monitor.addItem(f"모니터 {i + 1} ({g.width()}x{g.height()})", i)
        self.monitor.setCurrentIndex(min(s.monitor_index, max(0, self.monitor.count() - 1)))
        f.addRow("녹화할 모니터", self.monitor)

        self.audio = QCheckBox("게임 소리 녹음 (게임 소리만 - 디스코드/음악/마이크 제외)")
        self.audio.setChecked(s.record_game_audio)
        f.addRow(self.audio)
        self.ranked = QCheckBox("랭크 게임만 녹화 (솔로랭크 / 자유랭크)")
        self.ranked.setChecked(s.ranked_only)
        f.addRow(self.ranked)
        self.max_storage = QDoubleSpinBox()
        self.max_storage.setRange(0, 10000)
        self.max_storage.setSuffix(" GB")
        self.max_storage.setValue(s.max_storage_gb)
        f.addRow("최대 저장 용량 (0=무제한)", self.max_storage)
        self.keep_recent = QSpinBox()
        self.keep_recent.setRange(0, 1000)
        self.keep_recent.setSpecialValueText("끄기 (모두 보관)")
        self.keep_recent.setSuffix(" 경기")
        self.keep_recent.setValue(s.keep_recent_videos)
        f.addRow("최근 경기 영상만 보관", self.keep_recent)
        note = QLabel("용량을 넘거나 보관 경기 수를 넘으면 오래된 영상부터 자동 삭제됩니다.\n"
                      "경기 기록과 통계(대시보드)는 유지됩니다.\n"
                      "게임 성능을 위해 720p · 30fps · 자동 인코더(그래픽카드)를 권장합니다.")
        note.setObjectName("muted")
        f.addRow(note)
        return w

    def _replay_tab(self) -> QWidget:
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        self.replay_on = QCheckBox("죽으면 리플레이 자동 재생")
        self.replay_on.setChecked(s.death_replay_enabled)
        f.addRow(self.replay_on)
        self.before = QDoubleSpinBox()
        self.before.setRange(2, 30)
        self.before.setSuffix(" 초")
        self.before.setValue(s.replay_before_sec)
        f.addRow("죽기 전", self.before)
        self.after = QDoubleSpinBox()
        self.after.setRange(0, 10)
        self.after.setSuffix(" 초")
        self.after.setValue(s.replay_after_sec)
        f.addRow("죽은 후", self.after)
        self.position = QComboBox()
        for k, v in POSITIONS.items():
            self.position.addItem(v, k)
        self.position.setCurrentIndex(list(POSITIONS).index(s.replay_overlay_position)
                                      if s.replay_overlay_position in POSITIONS else 0)
        f.addRow("창 위치", self.position)
        self.scale = QSpinBox()
        self.scale.setRange(15, 70)
        self.scale.setSuffix(" % (화면 너비 대비)")
        self.scale.setValue(int(s.replay_overlay_scale * 100))
        f.addRow("창 크기", self.scale)
        self.close_respawn = QCheckBox("부활하면 리플레이 창 자동 닫기")
        self.close_respawn.setChecked(s.replay_close_on_respawn)
        f.addRow(self.close_respawn)
        self.replay_vol = QSpinBox()
        self.replay_vol.setRange(0, 100)
        self.replay_vol.setSuffix(" %")
        self.replay_vol.setValue(int(s.replay_volume * 100))
        f.addRow("리플레이 소리", self.replay_vol)
        note = QLabel("리플레이 창은 '테두리 없는 창 모드'에서 게임 위에 표시됩니다.\n"
                      "전체 화면 모드에서는 보이지 않지만, 녹화 목록에서 데스 장면으로 바로 이동할 수 있습니다.\n"
                      "리플레이 창은 녹화 영상에 찍히지 않고, 클릭이 게임으로 그대로 전달됩니다.")
        note.setObjectName("muted")
        f.addRow(note)
        return w

    def _api_tab(self) -> QWidget:
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        self.key = QLineEdit(get_api_key())
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("RGAPI-xxxxxxxx-xxxx-...")
        show = QPushButton("보기")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        test = QPushButton("키 확인")
        test.clicked.connect(self._test_key)
        row = QHBoxLayout()
        row.addWidget(self.key)
        row.addWidget(show)
        row.addWidget(test)
        f.addRow("API 키", row)
        self.region = QComboBox()
        self.region.addItem("자동 (클라이언트 기준)", "auto")
        for p in PLATFORMS:
            self.region.addItem(p, p)
        idx = self.region.findData(s.region)
        self.region.setCurrentIndex(max(0, idx))
        f.addRow("서버", self.region)
        self.riot_id = QLineEdit(s.riot_id)
        self.riot_id.setPlaceholderText("이름#태그 (비워두면 클라이언트에서 자동 감지)")
        f.addRow("Riot ID", self.riot_id)
        self.puuid_label = QLabel(s.puuid or "(아직 없음 - 게임을 한 판 하거나 Riot ID 입력 후 과거 경기 가져오기)")
        self.puuid_label.setObjectName("muted")
        self.puuid_label.setWordWrap(True)
        f.addRow("PUUID", self.puuid_label)
        note = QLabel("API 키는 Windows 자격 증명 관리자에 저장되며 파일에 기록되지 않습니다.")
        note.setObjectName("muted")
        f.addRow(note)
        return w

    def _app_tab(self) -> QWidget:
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        self.start_min = QCheckBox("트레이로 최소화된 상태로 시작")
        self.start_min.setChecked(s.start_minimized)
        f.addRow(self.start_min)
        self.autostart = QCheckBox("Windows 시작 시 자동 실행")
        self.autostart.setChecked(s.launch_on_startup)
        f.addRow(self.autostart)
        self.lead = QDoubleSpinBox()
        self.lead.setRange(0, 30)
        self.lead.setSuffix(" 초 전부터")
        self.lead.setValue(s.seek_lead_sec)
        f.addRow("이벤트 클릭 시 재생 시작", self.lead)
        return w

    # ------------------------------------------------------------------ actions
    def _update_size_hint(self) -> None:
        gb = self.bitrate.value() * 1000 / 8 * 1800 / 1024 ** 3
        self.size_hint.setText(f"30분 게임 기준 약 {gb:.1f} GB")

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "저장 위치", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _test_key(self) -> None:
        key = self.key.text().strip()
        if not key:
            QMessageBox.warning(self, "API 키", "키를 입력하세요")
            return
        region = self.region.currentData()
        ok, msg = RiotApi(key, "KR" if region == "auto" else region).check_key()
        (QMessageBox.information if ok else QMessageBox.warning)(self, "API 키", msg)

    def _save(self) -> None:
        s = self.s
        s.recordings_dir = self.dir_edit.text().strip() or s.recordings_dir
        s.resolution = self.res.currentText()
        s.fps = int(self.fps.currentText())
        s.bitrate_kbps = self.bitrate.value()
        s.encoder = self.encoder.currentData()
        s.monitor_index = self.monitor.currentData() or 0
        s.record_game_audio = self.audio.isChecked()
        s.ranked_only = self.ranked.isChecked()
        s.max_storage_gb = self.max_storage.value()
        s.keep_recent_videos = self.keep_recent.value()
        s.death_replay_enabled = self.replay_on.isChecked()
        s.replay_before_sec = self.before.value()
        s.replay_after_sec = self.after.value()
        s.replay_overlay_position = self.position.currentData()
        s.replay_overlay_scale = self.scale.value() / 100
        s.replay_close_on_respawn = self.close_respawn.isChecked()
        s.replay_volume = self.replay_vol.value() / 100
        new_riot_id = self.riot_id.text().strip()
        if new_riot_id != s.riot_id:
            s.riot_id = new_riot_id
            s.puuid = ""  # 다른 계정이면 다시 조회
        s.region = self.region.currentData()
        s.start_minimized = self.start_min.isChecked()
        s.launch_on_startup = self.autostart.isChecked()
        s.seek_lead_sec = self.lead.value()
        try:
            set_api_key(self.key.text().strip())
        except Exception as e:
            QMessageBox.warning(self, "API 키", f"API 키를 저장하지 못했습니다: {e}")
        self.accept()
