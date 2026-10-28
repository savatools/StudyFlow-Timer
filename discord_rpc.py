# -*- coding: utf-8 -*-
"""Discord Rich Presence helper for StudyFlow Timer."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

try:
    from pypresence import Presence
except Exception:  # The UI can still launch; the status explains the missing dependency.
    Presence = None  # type: ignore[assignment,misc]


class DiscordRPCWorker(QThread):
    """Talk to the Discord desktop client's local RPC in a worker thread.

    Networking stays off the GUI thread so a closed Discord client cannot freeze the timer.
    Presence timestamps are sent once per activity change; Discord displays the countdown
    from the supplied end timestamp itself.
    """

    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._config: dict[str, Any] = {"enabled": False, "client_id": ""}
        self._payload: dict[str, Any] = {
            "details": "StudyFlow готов к работе",
            "state": "Добавь занятия в план",
        }
        self._rpc = None
        self._connected_client_id: Optional[str] = None
        self._reported_status: Optional[str] = None
        self._last_payload_fingerprint: Optional[str] = None
        self._last_sent_at = 0.0

    def configure(self, enabled: bool, client_id: str) -> None:
        with self._lock:
            self._config = {
                "enabled": bool(enabled),
                "client_id": str(client_id).strip(),
            }
        self._wake_event.set()

    def set_activity(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._payload = dict(payload)
        self._wake_event.set()

    def stop_worker(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def _set_status(self, message: str) -> None:
        if message != self._reported_status:
            self._reported_status = message
            self.status_changed.emit(message)

    def _disconnect(self, clear: bool = True) -> None:
        rpc = self._rpc
        self._rpc = None
        self._connected_client_id = None
        self._last_payload_fingerprint = None
        self._last_sent_at = 0.0
        if rpc is None:
            return
        if clear:
            try:
                rpc.clear()
            except Exception:
                pass
        try:
            rpc.close()
        except Exception:
            pass

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def run(self) -> None:
        last_connect_attempt = 0.0
        try:
            while not self._stop_event.is_set():
                # Wake promptly on app changes, but also poll so failed connections retry.
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()
                if self._stop_event.is_set():
                    break

                with self._lock:
                    config = dict(self._config)
                    payload = dict(self._payload)

                enabled = bool(config.get("enabled"))
                client_id = str(config.get("client_id", "")).strip()

                if not enabled:
                    self._disconnect(clear=True)
                    self._set_status("Discord RPC: выключен")
                    last_connect_attempt = 0.0
                    continue
                if not client_id:
                    self._disconnect(clear=True)
                    self._set_status("Discord RPC: укажи Application ID в настройках")
                    last_connect_attempt = 0.0
                    continue
                if Presence is None:
                    self._disconnect(clear=True)
                    self._set_status("Discord RPC: библиотека не установлена — пересобери программу")
                    continue

                if self._rpc is not None and self._connected_client_id != client_id:
                    self._disconnect(clear=True)
                    last_connect_attempt = 0.0

                now = time.monotonic()
                if self._rpc is None:
                    if now - last_connect_attempt < 10.0:
                        self._set_status("Discord RPC: открой настольный Discord")
                        continue
                    last_connect_attempt = now
                    try:
                        rpc = Presence(client_id)
                        rpc.connect()
                        self._rpc = rpc
                        self._connected_client_id = client_id
                        self._last_payload_fingerprint = None
                        self._set_status("Discord RPC: подключено")
                    except Exception:
                        try:
                            rpc.close()  # type: ignore[possibly-undefined]
                        except Exception:
                            pass
                        self._rpc = None
                        self._connected_client_id = None
                        self._set_status("Discord RPC: открой настольный Discord")
                        continue

                fingerprint = self._fingerprint(payload)
                # pypresence/Discord rate limits: coalesce fast successive UI changes.
                if fingerprint == self._last_payload_fingerprint or now - self._last_sent_at < 2.0:
                    continue
                try:
                    self._rpc.update(**payload)
                    self._last_payload_fingerprint = fingerprint
                    self._last_sent_at = time.monotonic()
                    self._set_status("Discord RPC: подключено")
                except Exception:
                    self._disconnect(clear=False)
                    self._set_status("Discord RPC: соединение потеряно, переподключаюсь…")
                    last_connect_attempt = 0.0
        finally:
            self._disconnect(clear=True)
            self._set_status("Discord RPC: выключен")


class DiscordSettingsDialog(QDialog):
    """Settings dialog for the Discord application ID and image asset key."""

    def __init__(
        self,
        enabled: bool,
        client_id: str,
        large_image: str,
        large_text: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Discord Rich Presence")
        self.setMinimumWidth(530)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.enabled_check = QCheckBox("Показывать StudyFlow в активности Discord")
        self.enabled_check.setChecked(enabled)
        layout.addWidget(self.enabled_check)

        form = QFormLayout()
        self.client_id_edit = QLineEdit(client_id)
        self.client_id_edit.setPlaceholderText("Например, 123456789012345678")
        self.client_id_edit.setClearButtonEnabled(True)
        form.addRow("Application ID / Client ID", self.client_id_edit)

        self.image_key_edit = QLineEdit(large_image)
        self.image_key_edit.setPlaceholderText("Необязательно, например studyflow_icon")
        self.image_key_edit.setClearButtonEnabled(True)
        form.addRow("Ключ большой иконки", self.image_key_edit)

        self.image_text_edit = QLineEdit(large_text)
        self.image_text_edit.setPlaceholderText("Например, StudyFlow Timer")
        self.image_text_edit.setClearButtonEnabled(True)
        form.addRow("Подсказка при наведении", self.image_text_edit)
        layout.addLayout(form)

        help_label = QLabel(
            '1. Открой <a href="https://discord.com/developers/applications">Discord Developer Portal</a> и создай приложение.<br>'
            '2. В <b>General Information</b> скопируй Application ID.<br>'
            '3. Для собственной картинки открой раздел Rich Presence / Art Assets, загрузи её и впиши сюда ключ картинки (обычно в нижнем регистре).<br>'
            '4. Оставь настольный Discord запущенным. Подключение через браузер Discord не поддерживается.'
        )
        help_label.setWordWrap(True)
        help_label.setOpenExternalLinks(True)
        help_label.setStyleSheet("color: #aeb5c7;")
        layout.addWidget(help_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        client_id = self.client_id_edit.text().strip()
        if self.enabled_check.isChecked() and (not client_id or not client_id.isdigit()):
            QMessageBox.warning(
                self,
                "Нужен Application ID",
                "Включи Rich Presence только после того, как вставишь числовой Application ID из Discord Developer Portal.",
            )
            self.client_id_edit.setFocus()
            return
        self.accept()

    def values(self) -> tuple[bool, str, str, str]:
        return (
            self.enabled_check.isChecked(),
            self.client_id_edit.text().strip(),
            self.image_key_edit.text().strip().lower(),
            self.image_text_edit.text().strip(),
        )
