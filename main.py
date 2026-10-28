# -*- coding: utf-8 -*-
"""StudyFlow Timer — планировщик занятий для Windows."""
from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths, QTimer, QUrl, Qt, QSize
from PySide6.QtGui import QAction, QColor, QBrush, QFont, QIcon, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
from PySide6.QtWebEngineWidgets import QWebEngineView

from discord_rpc import DiscordRPCWorker, DiscordSettingsDialog

try:
    from winotify import Notification, audio as toast_audio
except Exception:  # optional fallback for environments without winotify
    Notification = None
    toast_audio = None

APP_NAME = "StudyFlow Timer"
DEFAULT_SUBJECTS = ["Русский", "История", "Английский", "Обществознание", "Математика"]
DEFAULT_YOUTUBE_URL = "https://www.youtube.com/@LofiGirl/live"
AUDIO_FILTER = "Audio files (*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.wma);;All files (*.*)"


@dataclass
class Task:
    name: str
    minutes: int
    kind: str = "study"  # study or rest


def format_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def format_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def make_tone(path: Path, notes: list[tuple[float, float]]) -> None:
    """Создаёт короткий WAV-сигнал только стандартной библиотекой Python."""
    sample_rate = 44100
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for frequency, duration in notes:
            count = int(sample_rate * duration)
            for i in range(count):
                # Короткие fade-in/fade-out убирают щелчки в начале и конце звука.
                fade = min(1.0, i / max(1, sample_rate * 0.012), (count - i) / max(1, sample_rate * 0.025))
                sample = int(10500 * max(0.0, fade) * math.sin(2 * math.pi * frequency * i / sample_rate))
                frames.extend(struct.pack("<h", sample))
            frames.extend(b"\x00\x00" * int(sample_rate * 0.055))
        wav.writeframes(frames)


class YoutubePanel(QWidget):
    """Небольшой встроенный YouTube-плеер на базе Qt WebEngine."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel("YouTube / Lo-fi радио")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.url_edit = QLineEdit(DEFAULT_YOUTUBE_URL)
        self.url_edit.setPlaceholderText("Вставь ссылку на YouTube")
        self.load_button = QPushButton("Открыть ссылку")
        self.load_button.clicked.connect(self.load_url)
        self.external_button = QPushButton("В обычном браузере")
        self.external_button.setToolTip("Открыть эту же ссылку в стандартном браузере Windows, например Edge или Chrome")
        self.external_button.clicked.connect(self.open_external)
        self.url_edit.returnPressed.connect(self.load_url)
        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.load_button)
        url_row.addWidget(self.external_button)
        layout.addLayout(url_row)

        self.web = QWebEngineView()
        # Некоторые сайты не распознают QtWebEngine как совместимый браузер.
        # Убираем только фирменный токен QtWebEngine из User-Agent, сохраняя
        # реальную версию Chromium и остальные параметры браузера.
        try:
            profile = self.web.page().profile()
            user_agent = profile.httpUserAgent()
            compatible_user_agent = re.sub(r"\s*QtWebEngine/\S+", "", user_agent).strip()
            if compatible_user_agent and compatible_user_agent != user_agent:
                profile.setHttpUserAgent(compatible_user_agent)
        except Exception:
            pass
        self.web.setMinimumHeight(245)
        self.web.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.web, 1)

        hint = QLabel("Нажми Play в YouTube. Если встроенный плеер сообщает, что видео нельзя воспроизвести в этом браузере, нажми «В обычном браузере» — ссылка откроется в Edge, Chrome или другом браузере Windows.")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._loaded = False

    def open_external(self):
        """Открывает текущую ссылку в браузере Windows по умолчанию."""
        url_text = self.url_edit.text().strip() or DEFAULT_YOUTUBE_URL
        if not url_text.startswith(("https://", "http://")):
            url_text = "https://" + url_text
        url = QUrl.fromUserInput(url_text)
        if not url.isValid() or not url.scheme().startswith("http"):
            QMessageBox.warning(self, "Ссылка не распознана", "Вставь корректную ссылку на YouTube, начинающуюся с https://")
            return
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "Не удалось открыть браузер", "Windows не смогла открыть ссылку. Скопируй её и открой вручную в Edge или Chrome.")

    def ensure_loaded(self):
        if not self._loaded:
            self.load_url()

    def load_url(self):
        url_text = self.url_edit.text().strip() or DEFAULT_YOUTUBE_URL
        if not url_text.startswith(("https://", "http://")):
            url_text = "https://" + url_text
        url = QUrl.fromUserInput(url_text)
        if not url.isValid() or not url.scheme().startswith("http"):
            QMessageBox.warning(self, "Ссылка не распознана", "Вставь корректную ссылку на YouTube, начинающуюся с https://")
            return
        self.url_edit.setText(url.toString())
        self.web.setUrl(url)
        self._loaded = True

    def pause_video(self):
        # При переключении на локальный плейлист стараемся остановить видео/звук YouTube.
        try:
            self.web.page().runJavaScript("document.querySelectorAll('video').forEach(v => v.pause());")
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1000, 700)
        self.resize(1220, 820)

        self.data_dir = self._get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "settings.json"
        self.start_sound_path = self.data_dir / "start.wav"
        self.end_sound_path = self.data_dir / "end.wav"
        self._ensure_sounds()

        self.subjects = DEFAULT_SUBJECTS.copy()
        self.music_files: list[str] = []
        self.queue: list[Task] = []
        self.current_task: Optional[Task] = None
        self.remaining_seconds = 0
        self.total_seconds = 0
        self.deadline: Optional[float] = None
        self.is_running = False
        self.is_paused = False
        self._loading_state = False
        self._closing = False
        self.discord_enabled = False
        self.discord_client_id = ""
        self.discord_large_image = ""
        self.discord_large_text = "StudyFlow Timer"
        self.discord_rpc_worker: Optional[DiscordRPCWorker] = None

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(250)
        self.tick_timer.timeout.connect(self.on_tick)

        self._load_state()
        self._build_ui()
        self._init_sounds()
        self._init_music_player()
        self._init_tray()
        self.refresh_queue()
        self.update_timer_display()
        self._init_discord_rpc()
        self.statusBar().showMessage("Готово. Добавь занятия и отдых в нужном порядке.")

    @staticmethod
    def _get_data_dir() -> Path:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if location:
            return Path(location)
        return Path.home() / ".studyflow_timer"

    def _ensure_sounds(self):
        if not self.start_sound_path.exists():
            make_tone(self.start_sound_path, [(880.0, 0.12), (1174.66, 0.15)])
        if not self.end_sound_path.exists():
            make_tone(self.end_sound_path, [(659.25, 0.15), (523.25, 0.20)])

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            subjects = data.get("subjects", DEFAULT_SUBJECTS)
            self.subjects = [str(s).strip() for s in subjects if str(s).strip()] or DEFAULT_SUBJECTS.copy()
            self.music_files = [str(p) for p in data.get("music_files", []) if Path(str(p)).is_file()]
            self.queue = []
            for raw in data.get("queue", []):
                name = str(raw.get("name", "")).strip()
                minutes = int(raw.get("minutes", 0))
                kind = str(raw.get("kind", "study"))
                if name and 5 <= minutes <= 240 and kind in ("study", "rest"):
                    self.queue.append(Task(name, minutes, kind))
            self.saved_youtube_url = str(data.get("youtube_url", DEFAULT_YOUTUBE_URL))
            self.repeat_playlist_saved = bool(data.get("repeat_playlist", True))
            self.discord_enabled = bool(data.get("discord_enabled", False))
            self.discord_client_id = str(data.get("discord_client_id", "")).strip()
            self.discord_large_image = str(data.get("discord_large_image", "")).strip().lower()
            self.discord_large_text = str(data.get("discord_large_text", "StudyFlow Timer")).strip() or "StudyFlow Timer"
        except Exception:
            self.subjects = DEFAULT_SUBJECTS.copy()
            self.music_files = []
            self.queue = []
            self.saved_youtube_url = DEFAULT_YOUTUBE_URL
            self.repeat_playlist_saved = True
            self.discord_enabled = False
            self.discord_client_id = ""
            self.discord_large_image = ""
            self.discord_large_text = "StudyFlow Timer"

    def save_state(self):
        if self._loading_state:
            return
        try:
            data = {
                "subjects": self.subjects,
                "music_files": self.music_files,
                "queue": [asdict(task) for task in self.queue],
                "youtube_url": self.youtube_panel.url_edit.text() if hasattr(self, "youtube_panel") else DEFAULT_YOUTUBE_URL,
                "repeat_playlist": self.repeat_checkbox.isChecked() if hasattr(self, "repeat_checkbox") else True,
                "discord_enabled": self.discord_enabled,
                "discord_client_id": self.discord_client_id,
                "discord_large_image": self.discord_large_image,
                "discord_large_text": self.discord_large_text,
            }
            self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            if hasattr(self, "statusBar"):
                self.statusBar().showMessage(f"Не удалось сохранить настройки: {exc}", 5000)

    def _build_ui(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 16, 18, 12)
        root_layout.setSpacing(12)

        header = QHBoxLayout()
        branding = QVBoxLayout()
        heading = QLabel("StudyFlow")
        heading.setObjectName("appTitle")
        subtitle = QLabel("План занятий • таймеры • музыка")
        subtitle.setObjectName("subtle")
        branding.addWidget(heading)
        branding.addWidget(subtitle)
        header.addLayout(branding)
        header.addStretch(1)
        self.discord_status_label = QLabel("Discord RPC: выключен")
        self.discord_status_label.setObjectName("subtle")
        header.addWidget(self.discord_status_label)
        self.discord_settings_button = QPushButton("Discord Activity ⚙")
        self.discord_settings_button.setToolTip("Настроить Discord Rich Presence")
        self.discord_settings_button.clicked.connect(self.open_discord_settings)
        header.addWidget(self.discord_settings_button)
        self.add_subject_button = QPushButton("＋ Добавить предмет")
        self.add_subject_button.clicked.connect(self.add_subject)
        header.addWidget(self.add_subject_button)
        root_layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(7)
        splitter.addWidget(self._build_planner_panel())
        splitter.addWidget(self._build_music_panel())
        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([650, 520])
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.setStyleSheet("""
            QMainWindow, QWidget { background: #11131a; color: #e9eaf2; font-family: 'Segoe UI'; font-size: 10pt; }
            QLabel#appTitle { font-size: 25pt; font-weight: 700; color: #f5f6ff; }
            QLabel#sectionTitle { font-size: 13pt; font-weight: 650; color: #f3f4fb; }
            QLabel#subtle { color: #9da4b8; font-size: 9pt; }
            QFrame#card { background: #1a1d27; border: 1px solid #2a2e3d; border-radius: 15px; }
            QLineEdit, QComboBox, QListWidget { background: #171a23; border: 1px solid #34394b; border-radius: 8px; padding: 8px; selection-background-color: #4655a4; }
            QComboBox QAbstractItemView { background: #171a23; selection-background-color: #4655a4; }
            QListWidget { padding: 6px; }
            QPushButton { background: #292e3d; border: 1px solid #3a4054; padding: 9px 13px; border-radius: 9px; font-weight: 600; }
            QPushButton:hover { background: #363d50; }
            QPushButton:pressed { background: #242938; }
            QPushButton:disabled { color: #62697b; background: #1b1e27; border-color: #292d39; }
            QPushButton#primary { background: #6875e7; color: white; border: 0; }
            QPushButton#primary:hover { background: #7a86f2; }
            QPushButton#danger { background: #48272e; border-color: #75414b; }
            QProgressBar { border: 0; background: #292d3b; border-radius: 5px; height: 10px; text-align: center; }
            QProgressBar::chunk { background: #7b87ff; border-radius: 5px; }
            QSlider::groove:horizontal { height: 5px; background: #34394b; border-radius: 2px; }
            QSlider::handle:horizontal { background: #8790ff; width: 14px; margin: -5px 0; border-radius: 7px; }
            QTabWidget::pane { border: 1px solid #2e3343; border-radius: 10px; top: -1px; }
            QTabBar::tab { background: #1b1e28; padding: 10px 12px; border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 3px; }
            QTabBar::tab:selected { background: #30364a; color: #ffffff; }
            QStatusBar { color: #9da4b8; }
        """)

    def _build_planner_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        timer_card = QFrame()
        timer_card.setObjectName("card")
        tc = QVBoxLayout(timer_card)
        tc.setContentsMargins(18, 16, 18, 15)
        tc.setSpacing(8)
        current_label = QLabel("СЕЙЧАС")
        current_label.setObjectName("subtle")
        self.current_title = QLabel("План пока не запущен")
        self.current_title.setWordWrap(True)
        self.current_title.setStyleSheet("font-size: 16pt; font-weight: 650;")
        self.clock_label = QLabel("00:00:00")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_label.setStyleSheet("font-size: 36pt; font-weight: 700; letter-spacing: 2px; color: #aab1ff;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.timer_detail = QLabel("Добавь первый пункт в план")
        self.timer_detail.setObjectName("subtle")
        self.timer_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tc.addWidget(current_label)
        tc.addWidget(self.current_title)
        tc.addWidget(self.clock_label)
        tc.addWidget(self.progress)
        tc.addWidget(self.timer_detail)

        controls = QHBoxLayout()
        self.start_button = QPushButton("▶  Старт")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_or_resume)
        self.pause_button = QPushButton("Ⅱ  Пауза")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.skip_button = QPushButton("Следующий →")
        self.skip_button.clicked.connect(self.skip_task)
        controls.addWidget(self.start_button, 2)
        controls.addWidget(self.pause_button, 1)
        controls.addWidget(self.skip_button, 1)

        queue_card = QFrame()
        queue_card.setObjectName("card")
        qc = QVBoxLayout(queue_card)
        qc.setContentsMargins(14, 13, 14, 13)
        qc.setSpacing(8)
        queue_header = QHBoxLayout()
        qtitle = QLabel("Порядок плана")
        qtitle.setObjectName("sectionTitle")
        self.queue_count = QLabel("0 пунктов")
        self.queue_count.setObjectName("subtle")
        queue_header.addWidget(qtitle)
        queue_header.addStretch(1)
        queue_header.addWidget(self.queue_count)
        qc.addLayout(queue_header)
        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(190)
        self.queue_list.setSpacing(3)
        self.queue_list.currentRowChanged.connect(self.update_queue_buttons)
        qc.addWidget(self.queue_list, 1)

        add_header = QLabel("Добавить в план")
        add_header.setObjectName("sectionTitle")
        add_row = QHBoxLayout()
        self.task_combo = QComboBox()
        self.task_combo.setMinimumWidth(150)
        self.refresh_task_combo()
        self.task_combo.currentTextChanged.connect(self.update_duration_options)
        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumWidth(115)
        self.update_duration_options(self.task_combo.currentText())
        self.add_task_button = QPushButton("＋ Добавить")
        self.add_task_button.setObjectName("primary")
        self.add_task_button.clicked.connect(self.add_task)
        add_row.addWidget(self.task_combo, 2)
        add_row.addWidget(self.duration_combo, 1)
        add_row.addWidget(self.add_task_button, 1)

        queue_tools = QHBoxLayout()
        self.up_button = QPushButton("↑ Выше")
        self.up_button.clicked.connect(lambda: self.move_task(-1))
        self.down_button = QPushButton("↓ Ниже")
        self.down_button.clicked.connect(lambda: self.move_task(1))
        self.remove_button = QPushButton("Удалить")
        self.remove_button.clicked.connect(self.remove_task)
        self.clear_button = QPushButton("Очистить план")
        self.clear_button.setObjectName("danger")
        self.clear_button.clicked.connect(self.clear_plan)
        queue_tools.addWidget(self.up_button)
        queue_tools.addWidget(self.down_button)
        queue_tools.addWidget(self.remove_button)
        queue_tools.addStretch(1)
        queue_tools.addWidget(self.clear_button)
        qc.addWidget(add_header)
        qc.addLayout(add_row)
        qc.addLayout(queue_tools)

        layout.addWidget(timer_card)
        layout.addLayout(controls)
        layout.addWidget(queue_card, 1)
        return panel

    def _build_music_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title = QLabel("Музыкальный мини-плеер")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.music_tabs = QTabWidget()
        self.files_tab = QWidget()
        fl = QVBoxLayout(self.files_tab)
        fl.setContentsMargins(8, 8, 8, 8)
        fl.setSpacing(8)
        self.track_label = QLabel("Ничего не играет")
        self.track_label.setWordWrap(True)
        self.track_label.setStyleSheet("font-weight: 600;")
        fl.addWidget(self.track_label)
        self.music_list = QListWidget()
        self.music_list.setMinimumHeight(190)
        self.music_list.itemDoubleClicked.connect(lambda _item: self.play_selected_track())
        fl.addWidget(self.music_list, 1)
        music_file_buttons = QHBoxLayout()
        add_files = QPushButton("＋ Файлы")
        add_files.clicked.connect(self.add_music_files)
        remove_file = QPushButton("Убрать")
        remove_file.clicked.connect(self.remove_music_file)
        music_file_buttons.addWidget(add_files)
        music_file_buttons.addWidget(remove_file)
        fl.addLayout(music_file_buttons)

        playback_row = QHBoxLayout()
        self.prev_button = QPushButton("|◀")
        self.prev_button.setToolTip("Предыдущий трек")
        self.prev_button.clicked.connect(self.previous_track)
        self.music_play_button = QPushButton("▶  Играть")
        self.music_play_button.clicked.connect(self.toggle_music_playback)
        self.next_button = QPushButton("▶|")
        self.next_button.setToolTip("Следующий трек")
        self.next_button.clicked.connect(self.next_track)
        playback_row.addWidget(self.prev_button)
        playback_row.addWidget(self.music_play_button, 1)
        playback_row.addWidget(self.next_button)
        fl.addLayout(playback_row)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("Громкость"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(45)
        volume_row.addWidget(self.volume_slider, 1)
        self.repeat_checkbox = self._make_repeat_checkbox()
        volume_row.addWidget(self.repeat_checkbox)
        fl.addLayout(volume_row)

        self.youtube_panel = YoutubePanel()
        self.youtube_tab = self.youtube_panel
        self.music_tabs.addTab(self.files_tab, "Локальные файлы")
        self.music_tabs.addTab(self.youtube_tab, "YouTube радио")
        self.music_tabs.currentChanged.connect(self.on_music_tab_changed)
        layout.addWidget(self.music_tabs, 1)

        note = QLabel("Поддерживаемые форматы зависят от декодеров Windows/Qt; обычно подходят MP3, FLAC, WAV и OGG.")
        note.setWordWrap(True)
        note.setObjectName("subtle")
        layout.addWidget(note)
        return panel

    def _make_repeat_checkbox(self):
        from PySide6.QtWidgets import QCheckBox
        checkbox = QCheckBox("Повтор")
        checkbox.setChecked(getattr(self, "repeat_playlist_saved", True))
        checkbox.stateChanged.connect(self.save_state)
        return checkbox

    def refresh_task_combo(self):
        if not hasattr(self, "task_combo"):
            return
        previous = self.task_combo.currentText()
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItems(self.subjects)
        self.task_combo.addItem("Другой предмет…")
        self.task_combo.addItem("Отдых")
        idx = self.task_combo.findText(previous)
        if idx >= 0:
            self.task_combo.setCurrentIndex(idx)
        self.task_combo.blockSignals(False)

    def update_duration_options(self, task_name: str):
        if not hasattr(self, "duration_combo"):
            return
        is_rest = task_name == "Отдых"
        old_value = self.duration_combo.currentData()
        self.duration_combo.blockSignals(True)
        self.duration_combo.clear()
        start = 5 if is_rest else 10
        default = 100 if is_rest else 30
        for mins in range(start, 241, 5):
            self.duration_combo.addItem(format_minutes(mins), mins)
        index = self.duration_combo.findData(old_value if old_value is not None else default)
        if index < 0:
            index = self.duration_combo.findData(default)
        self.duration_combo.setCurrentIndex(max(0, index))
        self.duration_combo.blockSignals(False)

    def add_subject(self):
        name, ok = QInputDialog.getText(self, "Добавить предмет", "Название предмета:")
        name = name.strip()
        if not ok or not name:
            return
        if name == "Отдых":
            QMessageBox.information(self, "Это отдельный пункт", "«Отдых» уже есть в списке и не требует добавления как предмета.")
            return
        if name not in self.subjects:
            self.subjects.append(name)
            self.refresh_task_combo()
            self.task_combo.setCurrentText(name)
            self.save_state()
            self.statusBar().showMessage(f"Предмет «{name}» добавлен", 3000)
        else:
            self.task_combo.setCurrentText(name)

    def add_task(self):
        selected = self.task_combo.currentText()
        if selected == "Другой предмет…":
            name, ok = QInputDialog.getText(self, "Название занятия", "Например, химия или повторение дат:")
            name = name.strip()
            if not ok or not name:
                return
            if name not in self.subjects:
                self.subjects.append(name)
                self.refresh_task_combo()
                self.task_combo.setCurrentText(name)
        else:
            name = selected
        minutes = int(self.duration_combo.currentData() or 30)
        kind = "rest" if name == "Отдых" else "study"
        self.queue.append(Task(name=name, minutes=minutes, kind=kind))
        self.refresh_queue(select_row=len(self.queue) - 1)
        self.save_state()
        self.statusBar().showMessage(f"Добавлено: {name} — {format_minutes(minutes)}", 3000)

    def refresh_queue(self, select_row: Optional[int] = None):
        if not hasattr(self, "queue_list"):
            return
        old_row = self.queue_list.currentRow()
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for index, task in enumerate(self.queue):
            label = f"{index + 1:02d}   {task.name}   ·   {format_minutes(task.minutes)}"
            if self.current_task is task:
                label = f"▶  {label}"
            elif task.kind == "rest":
                label = f"☕ {label}"
            else:
                label = f"   {label}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, index)
            if self.current_task is task:
                item.setBackground(QBrush(QColor("#303752")))
                item.setForeground(QBrush(QColor("#ffffff")))
            elif task.kind == "rest":
                item.setForeground(QBrush(QColor("#a9d5c0")))
            self.queue_list.addItem(item)
        self.queue_list.blockSignals(False)
        if self.queue:
            row = select_row if select_row is not None else (old_row if old_row >= 0 else 0)
            self.queue_list.setCurrentRow(min(max(0, row), len(self.queue) - 1))
        self.queue_count.setText(f"{len(self.queue)} пунктов")
        self.update_queue_buttons()
        self.update_discord_presence()

    def update_queue_buttons(self, *_):
        if not hasattr(self, "queue_list"):
            return
        row = self.queue_list.currentRow()
        valid = 0 <= row < len(self.queue)
        is_current = valid and self.current_task is self.queue[row] and (self.is_running or self.is_paused)
        self.remove_button.setEnabled(valid and not is_current)
        self.up_button.setEnabled(valid and row > 0 and not (self.current_task is self.queue[0] and row == 1))
        self.down_button.setEnabled(valid and row < len(self.queue) - 1 and not is_current)
        self.clear_button.setEnabled(bool(self.queue) and not (self.is_running or self.is_paused))

    def move_task(self, direction: int):
        row = self.queue_list.currentRow()
        new_row = row + direction
        if row < 0 or new_row < 0 or new_row >= len(self.queue):
            return
        if self.current_task is self.queue[row] and (self.is_running or self.is_paused):
            return
        if self.current_task is self.queue[new_row] and (self.is_running or self.is_paused):
            return
        self.queue[row], self.queue[new_row] = self.queue[new_row], self.queue[row]
        self.refresh_queue(select_row=new_row)
        self.save_state()

    def remove_task(self):
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(self.queue):
            return
        task = self.queue[row]
        if self.current_task is task and (self.is_running or self.is_paused):
            QMessageBox.information(self, "Таймер уже идёт", "Сначала нажми «Следующий» или дождись окончания текущего пункта.")
            return
        del self.queue[row]
        self.refresh_queue(select_row=min(row, len(self.queue) - 1) if self.queue else None)
        self.save_state()

    def clear_plan(self):
        if self.is_running or self.is_paused:
            QMessageBox.information(self, "Таймер запущен", "Чтобы очистить план, сначала заверши или пропусти активный пункт.")
            return
        if not self.queue:
            return
        answer = QMessageBox.question(self, "Очистить план", "Удалить все пункты плана?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.queue.clear()
            self.current_task = None
            self.refresh_queue()
            self.update_timer_display()
            self.save_state()

    def start_or_resume(self):
        if self.is_paused:
            self.resume_timer()
            return
        if self.is_running:
            return
        if self.current_task is not None and self.remaining_seconds > 0:
            self.resume_timer()
        else:
            self.start_current_task()

    def start_current_task(self):
        # Защита от повторного старта при клике во время короткого перехода между задачами.
        if self.is_running:
            return
        if self.current_task is not None and self.current_task in self.queue:
            task = self.current_task
        elif self.queue:
            task = self.queue[0]
            self.current_task = task
            self.remaining_seconds = task.minutes * 60
            self.total_seconds = self.remaining_seconds
        else:
            self.current_task = None
            self.is_running = False
            self.is_paused = False
            self.tick_timer.stop()
            self.current_title.setText("План завершён")
            self.timer_detail.setText("Добавь новые пункты, когда будешь готов")
            self.clock_label.setText("00:00:00")
            self.progress.setValue(1000)
            self.start_button.setText("▶  Старт")
            self.pause_button.setEnabled(False)
            self.skip_button.setEnabled(False)
            self.update_queue_buttons()
            self.update_discord_presence()
            return

        self.current_task = task
        if self.remaining_seconds <= 0:
            self.remaining_seconds = task.minutes * 60
            self.total_seconds = self.remaining_seconds
        self.deadline = time.monotonic() + self.remaining_seconds
        self.is_running = True
        self.is_paused = False
        self.tick_timer.start()
        self.play_sound("start")
        self.notify("Началось", f"{task.name} — {format_minutes(task.minutes)}")
        self.current_title.setText(task.name)
        self.timer_detail.setText(f"Длительность: {format_minutes(task.minutes)}" + (" • время отдыха" if task.kind == "rest" else " • время занятия"))
        self.start_button.setText("▶  Идёт таймер")
        self.pause_button.setText("Ⅱ  Пауза")
        self.pause_button.setEnabled(True)
        self.skip_button.setEnabled(True)
        self.progress.setValue(0)
        self.refresh_queue()
        self.update_timer_display()
        self.update_queue_buttons()

    def pause_timer(self):
        if not self.is_running:
            return
        if self.deadline is not None:
            self.remaining_seconds = max(0, math.ceil(self.deadline - time.monotonic()))
        self.tick_timer.stop()
        self.is_running = False
        self.is_paused = True
        self.deadline = None
        self.start_button.setText("▶  Продолжить")
        self.pause_button.setText("Ⅱ  На паузе")
        self.update_timer_display()
        self.update_queue_buttons()
        self.update_discord_presence()

    def resume_timer(self):
        if not self.is_paused or self.current_task is None:
            if self.current_task is None:
                self.start_current_task()
            return
        self.deadline = time.monotonic() + max(1, self.remaining_seconds)
        self.tick_timer.start()
        self.is_running = True
        self.is_paused = False
        self.start_button.setText("▶  Идёт таймер")
        self.pause_button.setText("Ⅱ  Пауза")
        self.pause_button.setEnabled(True)
        self.update_queue_buttons()
        self.update_discord_presence()

    def toggle_pause(self):
        if self.is_running:
            self.pause_timer()
        elif self.is_paused:
            self.resume_timer()

    def on_tick(self):
        if not self.is_running or self.deadline is None:
            return
        self.remaining_seconds = max(0, math.ceil(self.deadline - time.monotonic()))
        self.update_timer_display()
        if self.remaining_seconds <= 0:
            self.finish_current_task(skipped=False)

    def update_timer_display(self):
        self.clock_label.setText(format_clock(self.remaining_seconds))
        if self.current_task is None:
            self.progress.setValue(0 if not self.queue else 0)
            if self.queue:
                self.current_title.setText("Готов к запуску")
                first = self.queue[0]
                self.timer_detail.setText(f"Первый пункт: {first.name} — {format_minutes(first.minutes)}")
            else:
                self.current_title.setText("План пока не запущен")
                self.timer_detail.setText("Добавь первый пункт в план")
            self.pause_button.setEnabled(False)
            self.skip_button.setEnabled(False)
            self.start_button.setText("▶  Старт")
            return
        if self.total_seconds > 0:
            elapsed = max(0, self.total_seconds - self.remaining_seconds)
            self.progress.setValue(min(1000, int(elapsed / self.total_seconds * 1000)))
        if self.is_paused:
            self.timer_detail.setText(f"Пауза • осталось {format_clock(self.remaining_seconds)}")
        elif self.is_running:
            self.timer_detail.setText(f"Осталось {format_clock(self.remaining_seconds)}")

    def finish_current_task(self, skipped: bool):
        task = self.current_task
        self.tick_timer.stop()
        self.deadline = None
        self.is_running = False
        self.is_paused = False
        if task is not None:
            if not skipped:
                self.play_sound("end")
                self.notify("Завершено", f"{task.name} — время вышло")
            else:
                self.notify("Пропущено", task.name)
            try:
                self.queue.remove(task)
            except ValueError:
                pass
        self.current_task = None
        self.remaining_seconds = 0
        self.total_seconds = 0
        self.save_state()
        self.refresh_queue(select_row=0 if self.queue else None)
        if skipped:
            self.statusBar().showMessage("Пункт пропущен", 2500)
        elif task is not None:
            self.statusBar().showMessage(f"Время вышло: {task.name}", 3000)
        self.current_title.setText("Переход к следующему пункту…" if self.queue else "План завершён")
        self.clock_label.setText("00:00:00")
        self.progress.setValue(1000 if not self.queue else 0)
        self.start_button.setText("▶  Старт")
        self.pause_button.setEnabled(False)
        self.skip_button.setEnabled(False)
        self.update_queue_buttons()
        if self.queue:
            QTimer.singleShot(850 if not skipped else 300, self.start_current_task)
        else:
            self.timer_detail.setText("Все пункты выполнены. Отличная работа!")
            self.notify("План завершён", "Все пункты плана выполнены")

    def skip_task(self):
        if self.current_task is not None and (self.is_running or self.is_paused):
            self.finish_current_task(skipped=True)
        elif self.queue:
            # Если таймер ещё не стартовал, пропускаем первый пункт плана.
            task = self.queue.pop(0)
            self.notify("Пропущено", task.name)
            self.refresh_queue(select_row=0 if self.queue else None)
            self.save_state()
            if self.queue:
                self.start_current_task()
            else:
                self.update_timer_display()

    def _init_discord_rpc(self):
        self.discord_rpc_worker = DiscordRPCWorker(self)
        self.discord_rpc_worker.status_changed.connect(self.on_discord_rpc_status_changed)
        self.discord_rpc_worker.start()
        self.discord_rpc_worker.configure(self.discord_enabled, self.discord_client_id)
        self.update_discord_presence()

    def on_discord_rpc_status_changed(self, message: str):
        if hasattr(self, "discord_status_label"):
            self.discord_status_label.setText(message)
            self.discord_status_label.setToolTip(message)

    def open_discord_settings(self):
        dialog = DiscordSettingsDialog(
            self.discord_enabled,
            self.discord_client_id,
            self.discord_large_image,
            self.discord_large_text,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        enabled, client_id, large_image, large_text = dialog.values()
        self.discord_enabled = enabled
        self.discord_client_id = client_id
        self.discord_large_image = large_image
        self.discord_large_text = large_text or "StudyFlow Timer"
        self.save_state()
        if self.discord_rpc_worker is not None:
            self.discord_rpc_worker.configure(self.discord_enabled, self.discord_client_id)
        self.update_discord_presence()
        if enabled:
            self.statusBar().showMessage("Discord Rich Presence включён. Убедись, что настольный Discord запущен.", 6000)
        else:
            self.statusBar().showMessage("Discord Rich Presence выключен", 3000)

    @staticmethod
    def _discord_text(text: str, max_chars: int = 120) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"

    def update_discord_presence(self):
        """Send the current timer and next queued task to the Discord RPC worker."""
        worker = getattr(self, "discord_rpc_worker", None)
        if worker is None:
            return

        payload: dict[str, object] = {"name": "StudyFlow Timer"}
        if self.discord_large_image:
            payload["large_image"] = self.discord_large_image
            payload["large_text"] = self.discord_large_text or "StudyFlow Timer"

        next_task = None
        if self.current_task is not None and self.current_task in self.queue:
            current_index = next((i for i, queued_task in enumerate(self.queue) if queued_task is self.current_task), -1)
            if current_index >= 0 and current_index + 1 < len(self.queue):
                next_task = self.queue[current_index + 1]
        elif self.queue:
            next_task = self.queue[0]

        if self.current_task is not None and (self.is_running or self.is_paused):
            task = self.current_task
            current_description = "Сейчас: отдых" if task.kind == "rest" else f"Сейчас: {task.name}"
            paused_description = "Пауза: отдых" if task.kind == "rest" else f"Пауза: {task.name}"
            if self.is_running:
                payload["details"] = self._discord_text(current_description)
                payload["end"] = int((time.time() + max(0, self.remaining_seconds)) * 1000)
                if next_task is not None:
                    payload["state"] = self._discord_text(
                        f"Далее: {next_task.name} — {format_minutes(next_task.minutes)}"
                    )
                else:
                    payload["state"] = "Последний пункт плана"
            else:
                payload["details"] = self._discord_text(paused_description)
                remaining = format_clock(self.remaining_seconds)
                if next_task is not None:
                    payload["state"] = self._discord_text(
                        f"Осталось {remaining} · Далее: {next_task.name} — {format_minutes(next_task.minutes)}"
                    )
                else:
                    payload["state"] = self._discord_text(f"Осталось {remaining} · Последний пункт")
        elif self.queue:
            first = self.queue[0]
            payload["details"] = "План готов к запуску"
            payload["state"] = self._discord_text(
                f"Далее: {first.name} — {format_minutes(first.minutes)}"
            )
        else:
            payload["details"] = "План не запущен"
            payload["state"] = "Добавь занятия в StudyFlow"

        worker.set_activity(payload)

    def notify(self, title: str, message: str):
        """Windows toast, а если он недоступен — сообщение в системном трее."""
        shown = False
        if Notification is not None:
            try:
                toast = Notification(app_id=APP_NAME, title=title, msg=message, duration="short")
                if toast_audio is not None:
                    toast.set_audio(toast_audio.Silent, loop=False)
                toast.show()
                shown = True
            except Exception:
                shown = False
        if not shown and hasattr(self, "tray") and self.tray.isVisible():
            try:
                self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
                shown = True
            except Exception:
                pass
        self.statusBar().showMessage(f"{title}: {message}", 5000)

    def _init_sounds(self):
        self.start_sound = QSoundEffect(self)
        self.start_sound.setSource(QUrl.fromLocalFile(str(self.start_sound_path)))
        self.start_sound.setVolume(0.8)
        self.end_sound = QSoundEffect(self)
        self.end_sound.setSource(QUrl.fromLocalFile(str(self.end_sound_path)))
        self.end_sound.setVolume(0.8)

    def play_sound(self, kind: str):
        sound = self.start_sound if kind == "start" else self.end_sound
        try:
            sound.stop()
            sound.play()
        except Exception:
            QApplication.beep()

    def _init_music_player(self):
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(self.volume_slider.value() / 100.0)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_media_error)
        self.volume_slider.valueChanged.connect(lambda val: self.audio_output.setVolume(val / 100.0))
        self._refresh_music_list()
        if hasattr(self, "saved_youtube_url"):
            self.youtube_panel.url_edit.setText(self.saved_youtube_url)
        self.player.playbackStateChanged.connect(self.update_music_button)
        self.update_music_button()

    def _refresh_music_list(self, select_row: Optional[int] = None):
        if not hasattr(self, "music_list"):
            return
        self.music_list.clear()
        for path in self.music_files:
            self.music_list.addItem(QListWidgetItem(Path(path).name))
        if self.music_files:
            self.music_list.setCurrentRow(0 if select_row is None else min(select_row, len(self.music_files) - 1))
        self.save_state()

    def add_music_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить музыку", str(Path.home()), AUDIO_FILTER)
        if not files:
            return
        added = 0
        for path in files:
            if path not in self.music_files:
                self.music_files.append(path)
                added += 1
        self._refresh_music_list(select_row=max(0, len(self.music_files) - added))
        self.statusBar().showMessage(f"Добавлено файлов: {added}", 3000)

    def remove_music_file(self):
        row = self.music_list.currentRow()
        if row < 0 or row >= len(self.music_files):
            return
        removed_path = self.music_files[row]
        if self.player.source().toLocalFile() == removed_path:
            self.player.stop()
            self.player.setSource(QUrl())
        del self.music_files[row]
        self._refresh_music_list(select_row=max(0, row - 1))
        self.track_label.setText("Ничего не играет")

    def play_selected_track(self):
        row = self.music_list.currentRow()
        if row < 0 or row >= len(self.music_files):
            self.statusBar().showMessage("Сначала добавь музыкальные файлы", 3000)
            return
        path = self.music_files[row]
        if not Path(path).is_file():
            QMessageBox.warning(self, "Файл не найден", f"Не удалось найти файл:\n{path}")
            return
        self.youtube_panel.pause_video()
        self.music_tabs.setCurrentWidget(self.files_tab)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.track_label.setText(Path(path).name)
        self.player.play()
        self.update_music_button()

    def toggle_music_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if not self.player.source().isEmpty():
                self.player.play()
            else:
                self.play_selected_track()
        self.update_music_button()

    def update_music_button(self, *_):
        if not hasattr(self, "music_play_button") or not hasattr(self, "player"):
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.music_play_button.setText("Ⅱ  Пауза")
        else:
            self.music_play_button.setText("▶  Играть")

    def next_track(self):
        if not self.music_files:
            return
        row = self.music_list.currentRow()
        row = (row + 1) % len(self.music_files)
        self.music_list.setCurrentRow(row)
        self.play_selected_track()

    def previous_track(self):
        if not self.music_files:
            return
        row = self.music_list.currentRow()
        row = (row - 1) % len(self.music_files)
        self.music_list.setCurrentRow(row)
        self.play_selected_track()

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.music_files:
                if self.repeat_checkbox.isChecked():
                    self.next_track()
                else:
                    current_row = self.music_list.currentRow()
                    if current_row < len(self.music_files) - 1:
                        self.next_track()
                    else:
                        self.player.stop()

    def on_media_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            self.statusBar().showMessage(f"Ошибка воспроизведения: {error_string}", 7000)

    def on_music_tab_changed(self, index: int):
        if index == 1:
            self.player.pause()
            self.youtube_panel.ensure_loaded()
        else:
            self.youtube_panel.pause_video()
        self.update_music_button()

    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        restore_action = QAction("Открыть StudyFlow", self)
        restore_action.triggered.connect(self.showNormal)
        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self.close)
        menu.addAction(restore_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event):
        if (self.is_running or self.is_paused) and not self._closing:
            answer = QMessageBox.question(
                self,
                "Таймер ещё идёт",
                "Если закрыть программу, активный таймер остановится. Всё равно выйти?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        self.tick_timer.stop()
        self.player.stop() if hasattr(self, "player") else None
        self.save_state()
        if hasattr(self, "tray"):
            self.tray.hide()
        if self.discord_rpc_worker is not None and self.discord_rpc_worker.isRunning():
            self.discord_rpc_worker.stop_worker()
            self.discord_rpc_worker.wait(3000)
        event.accept()


def main():
    QApplication.setOrganizationName("StudyFlow")
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
