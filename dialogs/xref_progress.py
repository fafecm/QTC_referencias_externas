"""Janela de progresso para operações sobre referências externas."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout


class XrefProgress(QDialog):
    """Executa uma alteração por referência, mantendo a interface pintada."""

    _STEP_MS = 30
    _CLOSE_MS = 450

    def __init__(self, parent, title, targets, apply_target, finished):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Dialog)
        self._targets = list(targets)
        self._apply_target = apply_target
        self._finished = finished
        self._done = self._failed = 0
        self._closing = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setFixedWidth(430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        heading = QLabel(title, self)
        heading.setStyleSheet('font-weight: 600;')
        count = QLabel('1 vínculo a processar.' if len(self._targets) == 1
                       else f'{len(self._targets)} vínculos a processar.', self)
        count.setStyleSheet('color: gray;')
        self._bar = QProgressBar(self)
        self._bar.setRange(0, max(1, len(self._targets)))
        self._bar.setFormat('Vínculos processados %v de %m  —  %p%')
        self._current = QLabel('', self)
        self._current.setStyleSheet('color: gray;')
        for widget in (heading, count, self._bar, self._current):
            layout.addWidget(widget)

    def reject(self):
        if self._closing:
            super().reject()

    def close(self):
        self._closing = True
        try:
            return super().close()
        finally:
            self._closing = False

    def open(self):
        owner = self.parentWidget()
        owner = owner.topLevelWidget() if owner is not None else None
        if owner is not None:
            self.move(owner.mapToGlobal(owner.rect().center()) - self.rect().center())
        self.show()
        self._timer.start(self._STEP_MS)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)

    def _step(self):
        if self._done >= len(self._targets):
            return
        label, target = self._targets[self._done]
        self._current.setText(label)
        try:
            self._apply_target(target)
        except Exception:
            self._failed += 1
        self._done += 1
        self._bar.setValue(self._done)
        if self._done < len(self._targets):
            return
        self._timer.stop()
        self._current.setText(
            f'{self._failed} vínculo(s) não puderam ser processados.'
            if self._failed else 'Operação concluída.')
        # A gravação dos metadados ou a atualização da seleção pode falhar por
        # motivos externos. O diálogo tem de fechar de qualquer modo; caso
        # contrário ficaria modal sobre o IngeTrazo depois do último vínculo.
        try:
            self._finished(self._done, self._failed)
        finally:
            QTimer.singleShot(self._CLOSE_MS, self.close)
