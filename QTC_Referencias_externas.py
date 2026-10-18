"""QTC Referências externas — gestor de vínculos para IngeTrazo 0.5.8.
Metadata-only reference manager. Does NOT load external geometry or claim true XREF support.
"""
import copy
import json
import os
import getpass
import hashlib
import socket
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QProcess,
                            QProcessEnvironment, QRect, QSize, QTimer, Qt)
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QMenu, QMessageBox,
                               QLabel, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)
from tools.base import Tool

_KEY = 'references'
_PANEL_DOCK = None
_APP_KEY = None
_ICON_DIR = Path(__file__).with_name('Icones')
# Intervalo da verificação automática dos ficheiros referenciados (milissegundos).
XREF_CHECK_INTERVAL_MS = 10 * 60 * 1000
EDIT_LOCK_TIMEOUT_SECONDS = 90
EDIT_LOCK_CHECK_INTERVAL_MS = 5 * 1000


def _edit_lock_paths(source_path):
    """Devolve todos os locks, incluindo o formato antigo para migração."""
    source = Path(source_path)
    paths = [source.with_name(f'{source.stem}_lock.json')]
    try:
        # Formato transitório usado em versões anteriores.
        paths.extend(source.parent.glob(f'{source.name}-*.lock'))
    except OSError:
        pass
    # O formato anterior é reconhecido apenas para não deixar uma edição
    # iniciada antes desta versão invisível ou bloqueada para sempre.
    normalized = os.path.normcase(os.path.abspath(source_path)).encode('utf-8')
    legacy = source.with_name(f'.qtc_xref_{hashlib.sha256(normalized).hexdigest()[:16]}.lock')
    if legacy.exists():
        paths.append(legacy)
    return paths


def _new_edit_lock_path(source_path):
    """Nome de presença: ``nome_sem_extensão_lock.json``."""
    source = Path(source_path)
    return source.with_name(f'{source.stem}_lock.json')


def _edit_lock_info(source_path):
    """Devolve o editor ativo, ignorando locks que já não recebem heartbeat."""
    active = []
    for lock_path in _edit_lock_paths(source_path):
        try:
            info = json.loads(lock_path.read_text(encoding='utf-8'))
            heartbeat = float(info.get('heartbeat', 0))
        except (OSError, ValueError, TypeError):
            continue
        if time.time() - heartbeat <= EDIT_LOCK_TIMEOUT_SECONDS:
            active.append((heartbeat, info))
    return max(active, default=(None, None), key=lambda entry: entry[0])[1]


def _clear_expired_edit_lock(source_path):
    """Apaga um lock abandonado; devolve True quando libertou o vínculo."""
    removed = False
    for lock_path in _edit_lock_paths(source_path):
        try:
            info = json.loads(lock_path.read_text(encoding='utf-8'))
            heartbeat = float(info.get('heartbeat', 0))
        except (OSError, ValueError, TypeError):
            continue
        if time.time() - heartbeat <= EDIT_LOCK_TIMEOUT_SECONDS:
            continue
        try:
            lock_path.unlink()
            removed = True
        except OSError:
            pass
    return removed


def _is_document_switch_action(action):
    """Identifica ações nativas indisponíveis durante uma edição XREF."""
    label = action.text().split('\t', 1)[0]
    label = label.replace('&', '').replace('…', '').strip().casefold()
    return (label in {
        'novo', 'new', 'nova janela', 'new window',
        'abrir', 'open', 'recuperar auto-save', 'recuperar autosave',
        'recuperar salvamento automático', 'recover autosave',
        'recover auto-save',
    } or label.startswith((
        'abrir recente', 'open recent', 'recuperar auto-save',
        'recuperar autosave', 'recuperar salvamento automático',
        'recover autosave', 'recover auto-save',
    )))


def _disable_document_switch_actions(window):
    """Desativa e devolve o estado original das ações encontradas."""
    disabled = []
    for action in window.findChildren(QAction):
        if _is_document_switch_action(action):
            disabled.append((action, action.isEnabled()))
            action.setEnabled(False)
    return disabled


def _restore_document_switch_actions(disabled):
    for action, was_enabled in disabled:
        action.setEnabled(was_enabled)


class _RefreshOnEnter(QObject):
    """Pede uma única atualização ao entrar ou dar foco ao painel."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback
        self._pending = False

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if event.type() in (QEvent.Enter, QEvent.FocusIn):
            if not self._pending:
                self._pending = True

                def refresh_once():
                    self._pending = False
                    self._callback()

                QTimer.singleShot(0, refresh_once)
        return False


class _ClearSelectionOnEmptyTreeClick(QObject):
    """Limpa a seleção quando o utilizador clica numa zona vazia da lista."""

    def __init__(self, tree, callback, parent=None):
        super().__init__(parent)
        self._tree = tree
        self._callback = callback

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if (watched is self._tree.viewport() and
                event.type() == QEvent.MouseButtonPress and
                self._tree.itemAt(event.pos()) is None):
            # Deixa o QTreeWidget concluir o clique antes de limpar a seleção.
            QTimer.singleShot(0, self._callback)
        return False


class _MarkUserViewportSelection(QObject):
    """Distingue um clique real da seleção criada pela importação."""

    def __init__(self, viewport, callback, parent=None):
        super().__init__(parent)
        self._viewport = viewport
        self._callback = callback

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if event.type() == QEvent.MouseButtonPress:
            # A área OpenGL pode ser um filho interno do viewport. Um filtro
            # instalado na aplicação vê o evento original e evita confundir
            # uma seleção manual com a seleção criada pela importação.
            try:
                inside_viewport = (watched is self._viewport or
                                   self._viewport.isAncestorOf(watched))
            except TypeError:
                inside_viewport = False
            if inside_viewport:
                self._callback()
        return False


class _CenteredStatusIconDelegate(QStyledItemDelegate):
    """Desenha o ícone de estado centrado, sem texto na coluna."""

    def paint(self, painter, option, index):  # noqa: N802 - Qt override
        icon = index.data(Qt.DecorationRole)
        if icon is None:
            return super().paint(painter, option, index)
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.icon = QIcon()
        base.text = ''
        base.decorationSize = QSize(0, 0)
        style = base.widget.style() if base.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, base, painter, base.widget)
        size = icon.actualSize(QSize(16, 16))
        rect = QRect(option.rect.x() + (option.rect.width() - size.width()) // 2,
                     option.rect.y() + (option.rect.height() - size.height()) // 2,
                     size.width(), size.height())
        icon.paint(painter, rect, Qt.AlignCenter)


class _EditCloseGuard(QObject):
    """Impede fechar o projeto central enquanto uma referência é editada."""

    def __init__(self, has_active_edits, parent=None):
        super().__init__(parent)
        self._has_active_edits = has_active_edits

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if event.type() == QEvent.Close and self._has_active_edits():
            QMessageBox.warning(
                watched, 'Edição de referência em curso',
                'Termine a edição da referência na outra janela antes de fechar o projeto principal.')
            event.ignore()
            return True
        return False


class _ReadOnlyViewportGuard(QObject):
    """Durante edição externa, deixa a vista apenas para órbita e medição."""

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if event.type() == QEvent.ContextMenu:
            return True
        if event.type() == QEvent.KeyPress:
            key = event.key()
            modifiers = event.modifiers()
            undo_redo = (modifiers & Qt.ControlModifier and
                         key in (Qt.Key_Z, Qt.Key_Y))
            if key in (Qt.Key_Delete, Qt.Key_Backspace) or undo_redo:
                return True
        return False


class _EditModeWindowGuard(QObject):
    """Bloqueia criar/abrir documentos durante a edição de um vínculo."""

    _BLOCKED_SHORTCUTS = {
        (Qt.ControlModifier, Qt.Key_N),
        (Qt.ControlModifier | Qt.ShiftModifier, Qt.Key_N),
        (Qt.ControlModifier, Qt.Key_O),
    }

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if event.type() == QEvent.KeyPress:
            shortcut = (event.modifiers(), event.key())
            if shortcut in self._BLOCKED_SHORTCUTS:
                event.accept()
                return True
        return False


class _EditModeOverlay(QWidget):
    """Moldura não interativa que torna inequívoco o modo de edição."""

    def __init__(self, viewport):
        super().__init__(viewport)
        self._viewport = viewport
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet('background: transparent;')
        self._label = QLabel('MODO DE EDIÇÃO', self)
        self._label.setStyleSheet(
            'background: transparent; color: #F28C28; border: 0; '
            'font-size: 14px; font-weight: 700; padding: 0;')
        self._label.move(10, 10)
        viewport.installEventFilter(self)
        self._resize_to_viewport()
        self.show()
        self.raise_()

    def _resize_to_viewport(self):
        self.setGeometry(self._viewport.rect())
        self.raise_()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor('#F28C28'), 4))
        painter.setBrush(Qt.NoBrush)
        inset = 2
        painter.drawRect(self.rect().adjusted(inset, inset, -inset, -inset))
        painter.end()

    def eventFilter(self, watched, event):  # noqa: N802 - assinatura Qt
        if watched is self._viewport and event.type() == QEvent.Resize:
            QTimer.singleShot(0, self._resize_to_viewport)
        return False

    def remove(self):
        self._viewport.removeEventFilter(self)
        self.hide()
        self.deleteLater()


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _source_state(path):
    """Estado do ficheiro, sem o abrir nem importar a respetiva geometria."""
    try:
        stat = os.stat(path)
    except OSError:
        return 'Em falta', None
    return 'Disponível', stat.st_mtime_ns


def setup(app):
    global _PANEL_DOCK, _APP_KEY
    _APP_KEY = app.key
    edit_session_path = os.environ.get('QTC_XREF_EDIT_SESSION', '')
    edit_session = None
    if edit_session_path:
        try:
            edit_session = json.loads(Path(edit_session_path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            edit_session_path = ''
    panel = QWidget()
    # Evita que o contentor ainda sem dock seja apresentado brevemente na
    # origem da janela durante o arranque do IngeTrazo.
    panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    panel.hide()
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    tree = QTreeWidget()
    tree.setHeaderLabels(['Referência externa', '', '', 'Descrição', 'Atualização'])
    tree.header().setStretchLastSection(False)
    tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
    tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
    tree.header().setSectionResizeMode(2, QHeaderView.Fixed)
    tree.header().setSectionResizeMode(3, QHeaderView.Interactive)
    tree.header().setSectionResizeMode(4, QHeaderView.Fixed)
    tree.setColumnWidth(1, 26)
    tree.setItemDelegateForColumn(1, _CenteredStatusIconDelegate(tree))
    tree.header().resizeSection(2, tree.fontMetrics().horizontalAdvance('IGZ') + 16)
    tree.header().resizeSection(3, 100)
    tree.header().resizeSection(4, 168)
    for column in (2, 3, 4):
        tree.setColumnHidden(column, True)
    tree.setIndentation(0)
    tree.setRootIsDecorated(False)
    tree.setSelectionMode(QAbstractItemView.SingleSelection)
    tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tree.setContextMenuPolicy(Qt.CustomContextMenu)
    tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
    toolbar = QWidget(panel)
    buttons = QHBoxLayout(toolbar)
    buttons.setContentsMargins(0, 0, 0, 0)
    buttons.setSpacing(4)
    buttons.setAlignment(Qt.AlignLeft)
    # O IngeTrazo posiciona o painel no ciclo seguinte. Mantê-lo oculto até
    # lá evita que botões filhos sejam pintados transitoriamente em (0, 0).
    toolbar.hide()
    layout.addWidget(toolbar)
    search = QLineEdit(panel)
    search.setPlaceholderText('Pesquisar referências…')
    search.setClearButtonEnabled(True)
    layout.addWidget(search)
    layout.addWidget(tree)

    def records():
        data = app.document_data(default={}) or {}
        return list(data.get(_KEY, [])) if isinstance(data, dict) else []

    def records_signature(rows):
        """Identifica o conteúdo do documento sem considerar o estado visual."""
        return tuple(sorted(
            (str(row.get('id', '')), str(row.get('path', '')),
             row.get('source_mtime_ns'),
             str(row.get('component_uid') or ''), str(row.get('discipline', '')))
            for row in rows))

    saved_signature = {'value': None}

    def save(rows):
        data = dict(app.document_data(default={}) or {})
        data[_KEY] = rows
        saved_signature['value'] = records_signature(rows)
        app.set_document_data(data)
        refresh()

    progress_dialog = {'value': None}
    # Só a janela principal cria entradas; a janela aberta para edição recebe
    # o caminho da sua sessão através da variável de ambiente acima.
    editing_sessions = {}

    def run_with_progress(title, targets, apply_target, finished):
        """Executa alterações de vínculos na janela de progresso QTC."""
        if progress_dialog['value'] is not None or not targets:
            return False
        from core.extensions import _import_by_path
        module = _import_by_path(
            'qtc_xref_progress', Path(__file__).with_name('dialogs') / 'xref_progress.py')

        def complete(done, failed):
            progress_dialog['value'] = None
            finished(done, failed)

        dialog = module.XrefProgress(panel, title, targets, apply_target, complete)
        progress_dialog['value'] = dialog
        dialog.open()
        return True

    def refresh(*args):
        query = search.text().strip().casefold()
        # A lista é reconstruída para refletir estados e datas, mas isso não
        # deve retirar ao utilizador a referência que acabou de selecionar.
        current_item = tree.currentItem()
        selected_reference_id = (
            current_item.data(0, Qt.UserRole) if current_item is not None else None)
        tree.clear()
        restored_item = None
        for item in records():
            path = item.get('path', '')
            status, stamp = _source_state(path)
            loaded = component_for(item) is not None
            known_stamp = item.get('source_mtime_ns')
            editor = _edit_lock_info(path)
            if status == 'Em falta':
                status, status_icon = 'Indisponível', 'status_unavailable.svg'
                status_tip = status
            elif item.get('syncing'):
                status, status_icon = 'Em atualização', 'status_syncing.svg'
                status_tip = status
            elif not loaded:
                status, status_icon = 'Não carregado', 'status_unloaded.svg'
                status_tip = status
            elif known_stamp is not None and stamp != known_stamp:
                status, status_icon = 'Desatualizado', 'status_outdated.svg'
                status_tip = status
            elif editor is not None:
                status, status_icon = 'Em edição', 'status_editing.svg'
                status_tip = ('Em edição — PC: {host} / Utilizador: {user}'
                              .format(host=editor.get('host', 'desconhecido'),
                                      user=editor.get('user', 'desconhecido')))
            else:
                status, status_icon = 'Atualizado', 'status_current.svg'
                status_tip = status
            updated = item.get('updated_at', '—').replace('T', ' ').replace('+00:00', ' UTC')
            values = [os.path.basename(path), '', Path(path).suffix.upper().lstrip('.'),
                      item.get('discipline', ''), updated]
            if query and query not in ' '.join(values).casefold():
                continue
            row = QTreeWidgetItem(values)
            row.setData(0, Qt.UserRole, item.get('id'))
            row.setToolTip(0, path)
            row.setIcon(0, QIcon(str(_ICON_DIR / 'file_igz.svg')))
            row.setIcon(1, QIcon(str(_ICON_DIR / status_icon)))
            row.setToolTip(1, status_tip)
            row.setTextAlignment(1, Qt.AlignCenter)
            row.setFlags(row.flags() | Qt.ItemIsEditable)
            if not loaded:
                # Como no Navegador de projeto, um vínculo desligado mantém-se
                # legível, mas usa cinzento claro em vez do branco normal.
                # A diferença é visível também em temas escuros de contraste
                # reduzido, onde o tom anterior era praticamente igual ao ativo.
                muted = QBrush(QColor('#9BA9B8'))
                for column in range(5):
                    row.setForeground(column, muted)
            tree.addTopLevelItem(row)
            if item.get('id') == selected_reference_id:
                restored_item = row
        if restored_item is not None:
            tree.setCurrentItem(restored_item)

    def add():
        paths, _ = QFileDialog.getOpenFileNames(panel, 'Adicionar referências IGZ', '', 'IngeTrazo (*.igz *.IGZ)')
        if not paths:
            return
        rows = records()
        existing = {os.path.normcase(os.path.abspath(r['path'])) for r in rows}
        added = []
        for path in paths:
            canonical = os.path.normcase(os.path.abspath(path))
            if canonical in existing:
                continue
            state, stamp = _source_state(path)
            row = {'id': str(uuid.uuid4()), 'path': os.path.abspath(path),
                   'discipline': Path(path).stem,
                   'source_mtime_ns': stamp, 'updated_at': _utc_now()}
            rows.append(row)
            added.append(row)
            existing.add(canonical)

        def load_added(row):
            try:
                load_row(row)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f'{Path(row["path"]).name}: {exc}')

        def added_finished(_done, failed):
            save(rows)
            if failed:
                QMessageBox.warning(panel, 'Adicionar referência',
                                    'Algumas referências foram registadas, mas não puderam ser carregadas.')

        run_with_progress('A carregar referências',
                          [(Path(row['path']).name, row) for row in added],
                          load_added, added_finished)

    def remove():
        item = tree.currentItem()
        if item is None:
            return
        ref_id = item.data(0, Qt.UserRole)
        rows = records()
        row = next((r for r in rows if r.get('id') == ref_id), None)
        if row is None:
            return

        def removed_finished(_done, _failed):
            save([r for r in rows if r.get('id') != ref_id])

        run_with_progress('A remover referência', [(Path(row['path']).name, row)],
                          unload_row, removed_finished)

    def verify():
        refresh()
        rows = records()
        missing = [r['path'] for r in rows if not os.path.isfile(r['path'])]
        QMessageBox.information(panel, 'Verificação XREF',
            '%d referência(s); %d ficheiro(s) em falta.'
            % (len(rows), len(missing)))

    def refresh_states():
        """Reavalia os vínculos e liberta somente locks abandonados."""
        rows = records()
        released = sum(
            1 for row in rows
            if _clear_expired_edit_lock(row.get('path', ''))
        )
        refresh()
        message = 'Estados atualizados.'
        if released:
            message += ' %d bloqueio(s) expirado(s) removido(s).' % released
        QMessageBox.information(panel, 'Atualizar estados', message)

    def project_path():
        path = getattr(getattr(app, 'window', None), '_current_path', None)
        return Path(path) if path and Path(path).is_file() else None

    def export_igp():
        """Empacota o projeto IGZ e as referências disponíveis num IGP."""
        source_project = project_path()
        if source_project is None:
            QMessageBox.warning(panel, 'Exportar projeto IGP',
                                'Guarde o projeto principal antes de o exportar.')
            return
        suggested = str(source_project.with_suffix('.igp'))
        archive_name, _ = QFileDialog.getSaveFileName(
            panel, 'Exportar projeto completo IGP', suggested,
            'Projeto IngeTrazo (*.igp)')
        if not archive_name:
            return
        archive_path = Path(archive_name)
        if archive_path.suffix.casefold() != '.igp':
            archive_path = archive_path.with_suffix('.igp')
        references = records()
        manifest = {
            'format': 'QTC_IGP', 'version': 1,
            'project_entry': f'project/{source_project.name}',
            'project_filename': source_project.name,
            'references': [],
        }
        files = [(source_project.name, (source_project, manifest['project_entry']))]
        for row in references:
            source = Path(row.get('path', ''))
            entry = None
            restore_name = None
            if source.is_file():
                # O identificador organiza apenas o conteúdo interno do IGP;
                # o ficheiro restaurado mantém exatamente o nome original.
                restore_name = source.name
                entry = f'references/{str(row.get("id", ""))}/{source.name}'
                files.append((source.name, (source, entry)))
            manifest['references'].append({
                'id': row.get('id'), 'entry': entry,
                'restore_filename': restore_name,
            })
        try:
            archive = zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED)
        except (OSError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(panel, 'Exportar projeto IGP',
                                 f'Não foi possível criar o ficheiro IGP.\n\n{exc}')
            return

        def archive_file(target):
            source, entry = target
            archive.write(source, entry)

        def exported(_done, failed):
            try:
                archive.writestr('manifest.json', json.dumps(
                    manifest, ensure_ascii=False, indent=2))
            finally:
                archive.close()
            if failed:
                QMessageBox.warning(panel, 'Exportar projeto IGP',
                                    'O IGP foi criado, mas alguns ficheiros não puderam ser incluídos.')
            else:
                QMessageBox.information(panel, 'Exportar projeto IGP',
                                        f'Projeto exportado para:\n{archive_path}')

        if not run_with_progress('A exportar projeto IGP', files, archive_file, exported):
            archive.close()

    def export_compact_igz():
        """Grava uma cópia IGZ autónoma, sem metadados de vínculos.

        A geometria dos componentes carregados não é explodida nem alterada:
        apenas deixa de ter a marca QTC que a identifica como referência
        externa. A remoção é temporária na cena ativa, exclusivamente durante
        a serialização, e é sempre reposta no fim.
        """
        source_project = project_path()
        if source_project is None:
            QMessageBox.warning(panel, 'Exportar IGZ compacto',
                                'Guarde primeiro o projeto principal.')
            return
        rows = records()
        not_loaded = [row for row in rows if component_for(row) is None]
        if not_loaded:
            QMessageBox.warning(
                panel, 'Exportar IGZ compacto',
                'Todas as referências têm de estar carregadas para serem '
                'incorporadas no IGZ compacto.\n\nCarregue as referências '
                'em falta e volte a exportar.')
            return
        timestamp = datetime.now().strftime('%y%m%d-%H%M%S')
        target = source_project.with_name(
            f'{source_project.stem}_Compacto_{timestamp}{source_project.suffix}')

        def descendants(groups):
            for group in groups:
                yield group
                yield from descendants(getattr(group, 'children', ()) or ())

        # Uma etapa por vínculo deixa explícito no diálogo quantos vínculos
        # fazem parte da compactação, mesmo que a escrita IGZ aconteça numa
        # única operação atómica no último passo.
        compact_steps = [(Path(row['path']).name, index)
                         for index, row in enumerate(rows)]
        if not compact_steps:
            compact_steps = [(target.name, 0)]

        def write_compact(step):
            if step != len(compact_steps) - 1:
                return
            # ``set_document_data`` alimenta ``scene.plugin_data``. Guardamos
            # a cópia profunda para restaurar inclusive listas/dicionários de
            # extensões exatamente como estavam no projeto que continua aberto.
            original_data = copy.deepcopy(app.document_data(default={}) or {})
            compact_data = copy.deepcopy(original_data)
            compact_data.pop(_KEY, None)
            saved_extensions = []
            groups = list(getattr(app.scene, 'groups', ()) or ())
            groups.extend(list(getattr(app.scene, 'kept_components', ()) or ()))
            try:
                for group in descendants(groups):
                    ext = getattr(group, 'ext', None)
                    if not isinstance(ext, dict) or _APP_KEY not in ext:
                        continue
                    saved_extensions.append((group, ext))
                    stripped = dict(ext)
                    stripped.pop(_APP_KEY, None)
                    group.ext = stripped
                app.set_document_data(compact_data)
                from formats import igz as igz_format
                igz_format.save_scene(app.scene, target)
            finally:
                for group, extension in saved_extensions:
                    group.ext = extension
                app.set_document_data(original_data)

        def compacted(_done, failed):
            refresh()
            if failed:
                QMessageBox.critical(panel, 'Exportar IGZ compacto',
                                     'Não foi possível criar o IGZ compacto.')
            else:
                QMessageBox.information(
                    panel, 'Exportar IGZ compacto',
                    f'Projeto compacto criado em:\n{target}\n\n'
                    'Os objetos dos vínculos foram mantidos como objetos normais.')

        run_with_progress('A exportar IGZ compacto', compact_steps,
                          write_compact, compacted)

    def open_igp():
        """Restaura um IGP ao lado do arquivo e abre o projeto principal."""
        archive_name, _ = QFileDialog.getOpenFileName(
            panel, 'Abrir projeto IGP', '', 'Projeto IngeTrazo (*.igp)')
        if not archive_name:
            return
        archive_path = Path(archive_name)
        output_dir = archive_path.parent
        try:
            with zipfile.ZipFile(archive_path, 'r') as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
                if manifest.get('format') != 'QTC_IGP':
                    raise ValueError('O ficheiro não é um projeto IGP QTC válido.')
                files = [(manifest['project_entry'], output_dir / manifest['project_filename'])]
                references = {}
                for item in manifest.get('references', []):
                    if not item.get('entry') or not item.get('restore_filename'):
                        continue
                    target = output_dir / item['restore_filename']
                    files.append((item['entry'], target))
                    references[item.get('id')] = str(target)
                existing = [target for _entry, target in files if target.exists()]
                if existing:
                    answer = QMessageBox.question(
                        panel, 'Abrir projeto IGP',
                        'Já existem ficheiros com o mesmo nome na pasta do IGP.\n\n'
                        'Pretende substituí-los?',
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if answer != QMessageBox.Yes:
                        return
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(panel, 'Abrir projeto IGP',
                                 f'Não foi possível restaurar o ficheiro IGP.\n\n{exc}')
            return

        def extract_file(target):
            entry, destination_path = target
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, 'r') as archive:
                with archive.open(entry) as source, destination_path.open('wb') as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        destination.write(chunk)

        def restored(_done, failed):
            restored_project = output_dir / manifest['project_filename']
            if not restored_project.is_file():
                QMessageBox.critical(panel, 'Abrir projeto IGP',
                                     'O projeto principal não pôde ser restaurado.')
                return
            sidecar = restored_project.with_name(restored_project.name + '.igp_restore.json')
            try:
                sidecar.write_text(json.dumps({'references': references}, ensure_ascii=False),
                                   encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(panel, 'Abrir projeto IGP',
                                    f'O projeto foi restaurado, mas os caminhos dos vínculos não puderam ser preparados.\n\n{exc}')
            executable = QCoreApplication.applicationFilePath()
            process = QProcess(panel)
            process.setProgram(executable)
            # Sem ``--new-window`` a política de instância única do núcleo
            # encaminha a abertura para a janela atualmente ativa.
            process.setArguments([str(restored_project)])
            process.setWorkingDirectory(str(output_dir))
            if not executable or not process.startDetached():
                QMessageBox.warning(panel, 'Abrir projeto IGP',
                                    f'Ficheiros restaurados em:\n{output_dir}\n\nAbra o projeto manualmente.')
            elif failed:
                QMessageBox.warning(panel, 'Abrir projeto IGP',
                                    'O projeto foi aberto, mas alguns vínculos não puderam ser restaurados.')

        run_with_progress('A restaurar projeto IGP',
                          [(target.name, (entry, target)) for entry, target in files],
                          extract_file, restored)

    def apply_igp_restore_manifest():
        """Atualiza os caminhos absolutos do projeto acabado de restaurar."""
        source_project = project_path()
        if source_project is None:
            return
        sidecar = source_project.with_name(source_project.name + '.igp_restore.json')
        try:
            restored = json.loads(sidecar.read_text(encoding='utf-8'))
            mapping = restored.get('references', {})
        except (OSError, ValueError, TypeError):
            return
        rows = records()
        changed = False
        for row in rows:
            restored_path = mapping.get(row.get('id'))
            if restored_path:
                row['path'] = restored_path
                changed = True
        if changed:
            data = dict(app.document_data(default={}) or {})
            data[_KEY] = rows
            app.set_document_data(data)
        try:
            sidecar.unlink()
        except OSError:
            pass

    def selected_id():
        item = tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def component_for(row):
        """Devolve somente o componente que este gestor criou para o vínculo."""
        uid = row.get('component_uid')
        if not uid:
            return None
        for group in getattr(app.scene, 'groups', ()):
            if getattr(group, 'uid', None) != uid:
                continue
            ext = getattr(group, 'ext', None) or {}
            marker = ext.get(_APP_KEY, {}) if isinstance(ext, dict) else {}
            if marker.get('xref_id') == row.get('id'):
                return group
        return None

    def unload_row(row):
        """Remove do documento exclusivamente o componente deste vínculo."""
        component = component_for(row)
        row['component_uid'] = None
        if component is None:
            return False
        from core.history import SnapshotImport

        def mutate(scene):
            scene.groups[:] = [group for group in scene.groups if group is not component]
            discard_selection = getattr(scene.selection, 'discard', None)
            if callable(discard_selection):
                discard_selection(component)
            else:
                scene.selection[:] = [item for item in scene.selection if item is not component]

        app.viewport.history.execute(SnapshotImport(mutate))
        app.viewport.notify_scene_changed()
        app.viewport.update()
        return True

    def discard_nested_xref_components(component):
        """Remove componentes XREF já existentes dentro da origem importada."""
        removed = 0
        children = getattr(component, 'children', None)
        if not children:
            return removed
        kept = []
        for child in list(children):
            ext = getattr(child, 'ext', None) or {}
            marker = ext.get(_APP_KEY, {}) if isinstance(ext, dict) else {}
            if isinstance(marker, dict) and marker.get('xref_id'):
                removed += 1
                continue
            removed += discard_nested_xref_components(child)
            kept.append(child)
        try:
            children[:] = kept
        except TypeError:
            component.children = kept
        return removed

    def load_row(row):
        """Importa o IGZ como componente, assinalado para remoção segura."""
        if component_for(row) is not None:
            return False
        state, _stamp = _source_state(row.get('path', ''))
        if state == 'Em falta':
            raise FileNotFoundError(row.get('path', ''))
        component = app.import_igz(row['path'], at=(0.0, 0.0, 0.0))
        if component is None:
            raise ValueError('O ficheiro não contém geometria para importar.')
        # Os vínculos declarados dentro do IGZ de origem pertencem ao respetivo
        # projeto. Nunca passam a ser vínculos do projeto principal nem são
        # carregados como geometria aninhada nesta importação.
        discard_nested_xref_components(component)
        ext = dict(getattr(component, 'ext', None) or {})
        ext[_APP_KEY] = {'xref_id': row['id']}
        component.ext = ext
        # Não se usa ``locked``: no núcleo atual essa propriedade também retira
        # o componente do picking e dos snaps. A referência tem de continuar
        # selecionável para coordenar e medir no modelo central.
        component.locked = False
        row['component_uid'] = component.uid
        row['source_mtime_ns'] = _stamp
        row['updated_at'] = _utc_now()
        return True

    def selected_row(rows):
        ref_id = selected_id()
        return next((row for row in rows if row.get('id') == ref_id), None)

    selection_sync = {'pending': False}
    user_tree_selection = {'value': False}

    def sync_selected_reference_to_view():
        """Espelha a referência escolhida no painel na seleção da vista 3D."""
        if selection_sync['pending']:
            return
        selection_sync['pending'] = True

        def apply_selection():
            selection_sync['pending'] = False
            item = tree.currentItem()
            if item is None or not item.isSelected():
                return
            rows = records()
            row = next((entry for entry in rows
                        if entry.get('id') == item.data(0, Qt.UserRole)), None)
            component = component_for(row) if row is not None else None
            if component is None:
                return
            scene = app.viewport.scene
            selectable = getattr(scene, 'entity_selectable', None)
            if callable(selectable) and not selectable(component):
                return
            if (not callable(selectable) and
                    (getattr(component, 'hidden', False) or
                     getattr(component, 'locked', False))):
                return
            scene.selection.clear()
            scene.selection.add(component)
            bump_view = getattr(scene, 'bump_view', None)
            if callable(bump_view):
                bump_view()
            app.viewport.update()

            # A seleção no painel não pode deixar no projeto principal um
            # filtro de ferramentas de uma edição anterior. Se já não há
            # sessões de edição, elimina qualquer guarda residual e devolve
            # todos os comandos ao utilizador.
            if not editing_sessions:
                main_window = getattr(app, 'window', None)
                set_filter = getattr(main_window, 'set_tool_filter', None)
                if callable(set_filter):
                    set_filter(None)
                readonly_guard = getattr(panel, '_xref_readonly_guard', None)
                if readonly_guard is not None:
                    app.viewport.removeEventFilter(readonly_guard)
                    panel._xref_readonly_guard = None

        # O Qt pode concluir o clique depois de emitir o sinal de seleção.
        QTimer.singleShot(0, apply_selection)

    scene_selection_signature = {'value': None}

    def sync_view_selection_to_tree():
        """Espelha na árvore o grupo de vínculo escolhido na vista 3D."""
        scene = getattr(app.viewport, 'scene', None)
        selection = getattr(scene, 'selection', ()) if scene is not None else ()
        selected_uids = frozenset(
            str(getattr(entity, 'uid', '')) for entity in selection
            if getattr(entity, 'uid', None) is not None)
        if selected_uids == scene_selection_signature['value']:
            return
        scene_selection_signature['value'] = selected_uids

        selected_reference_id = None
        for row in records():
            component = component_for(row)
            if component is not None and str(getattr(component, 'uid', '')) in selected_uids:
                selected_reference_id = row.get('id')
                break

        current = tree.currentItem()
        current_id = current.data(0, Qt.UserRole) if current is not None else None
        if current_id == selected_reference_id:
            return
        tree.blockSignals(True)
        try:
            tree.clearSelection()
            if selected_reference_id is None:
                tree.setCurrentItem(None)
                return
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                if item.data(0, Qt.UserRole) == selected_reference_id:
                    tree.setCurrentItem(item)
                    item.setSelected(True)
                    break
        finally:
            tree.blockSignals(False)

    def select_tree_reference(_item, _column):
        """Regista uma escolha explícita antes de espelhar na vista 3D."""
        user_tree_selection['value'] = True
        sync_selected_reference_to_view()

    def load_or_unload():
        rows = records()
        row = selected_row(rows)
        if row is None:
            return
        loading = component_for(row) is None

        def change(target):
            if loading:
                load_row(target)
            else:
                unload_row(target)

        run_with_progress('A carregar referência' if loading else 'A descarregar referência',
                          [(Path(row['path']).name, row)], change,
                          lambda _done, _failed: save(rows))

    def reload_selected():
        rows = records()
        row = selected_row(rows)
        if row is None:
            return
        def reload(target):
            unload_row(target)
            load_row(target)

        def reloaded(_done, _failed):
            save(rows)
            clear_after_xref_operation()

        run_with_progress('A sincronizar referência', [(Path(row['path']).name, row)],
                          reload, reloaded)

    def update(reference_ids=None):
        rows = records()
        selected = set(reference_ids) if reference_ids is not None else None
        updated = 0
        for row in rows:
            if selected is not None and row.get('id') not in selected:
                continue
            _state, stamp = _source_state(row.get('path', ''))
            row['source_mtime_ns'] = stamp
            row['updated_at'] = _utc_now()
            updated += 1
        save(rows)
        return updated

    def sync_all():
        rows = records()
        # Só os vínculos carregados podem ser sincronizados. Um vínculo
        # descarregado volta a ler a versão atual quando for carregado.
        targets = [row for row in rows if component_for(row) is not None]

        def sync(row):
            row['syncing'] = True
            try:
                unload_row(row)
                load_row(row)
            finally:
                row['syncing'] = False

        def synced(_done, failed):
            save(rows)
            clear_after_xref_operation()
            if failed:
                QMessageBox.warning(panel, 'Sincronizar referências',
                                    'Algumas referências não foram sincronizadas.')

        run_with_progress('A sincronizar referências',
                          [(Path(row['path']).name, row) for row in targets], sync, synced)

    def clear_view_selection():
        """Limpa a seleção da lista e da cena publicada pelo núcleo."""
        tree.clearSelection()
        tree.setCurrentItem(None)
        # A importação trabalha na cena do viewport. Em algumas versões a
        # fachada ``app.scene`` ainda aponta para a cena anterior durante
        # a abertura, portanto as duas referências são limpas.
        scenes = (getattr(app, 'scene', None),
                  getattr(app.viewport, 'scene', None))
        for scene in {id(scene): scene for scene in scenes if scene is not None}.values():
            selection = getattr(scene, 'selection', None)
            if selection is not None:
                # A seleção do núcleo é um ``set`` nas versões atuais;
                # ``clear`` também mantém compatibilidade com coleções
                # que a exponham como lista.
                clear_selection = getattr(selection, 'clear', None)
                if callable(clear_selection):
                    clear_selection()
                else:
                    selection[:] = []
        # Seleção não é uma alteração à geometria/documento. Notificar a cena
        # aqui fazia o núcleo reconstruir o estado de comandos e podia deixar
        # a vista momentaneamente sem resposta. Tal como o Navegador de
        # projeto, invalida-se apenas a vista.
        scene = getattr(app.viewport, 'scene', None)
        bump_view = getattr(scene, 'bump_view', None)
        if callable(bump_view):
            bump_view()
        app.viewport.update()

    def has_regular_view_selection():
        """Indica se a vista contém uma escolha do utilizador fora de XREF."""
        scene = getattr(app.viewport, 'scene', None)
        for entity in getattr(scene, 'selection', ()):
            ext = getattr(entity, 'ext', None) or {}
            marker = ext.get(_APP_KEY, {}) if isinstance(ext, dict) else {}
            if not marker.get('xref_id'):
                return True
        return False

    def clear_after_xref_operation():
        """A operação terminou: nunca altera uma seleção posterior."""
        # Não limpar aqui. Algumas versões do núcleo publicam a seleção do
        # componente com atraso imprevisível; qualquer temporizador para a
        # remover acaba por apagar também uma seleção real do utilizador.
        return

    def clear_opening_selection():
        """A abertura não deve iniciar nem cancelar seleções no painel."""
        return

    def sync_on_document_open():
        """Ao abrir, recompõe somente os vínculos que já não estão atuais."""
        if progress_dialog['value'] is not None:
            return
        apply_igp_restore_manifest()
        rows = records()
        stale = []
        for row in rows:
            state, stamp = _source_state(row.get('path', ''))
            if state == 'Em falta':
                continue
            component = component_for(row)
            if component is None:
                # Descarregada: só volta a importar mediante Carregar.
                continue
            if component is not None and row.get('source_mtime_ns') == stamp:
                continue
            stale.append(row)

        if not stale:
            refresh()
            clear_opening_selection()
            return

        def sync(row):
            try:
                row['syncing'] = True
                unload_row(row)
                load_row(row)
            finally:
                row['syncing'] = False

        def opened_sync_finished(_done, _failed):
            save(rows)
            clear_opening_selection()

        run_with_progress('A atualizar referências do projeto',
                          [(Path(row['path']).name, row) for row in stale], sync,
                          opened_sync_finished)

    def update_selected():
        ref_id = selected_id()
        if ref_id is not None:
            reload_selected()

    def relocate():
        rows = records()
        row = selected_row(rows)
        if row is None:
            return
        path, _ = QFileDialog.getOpenFileName(panel, 'Localizar referência IGZ',
                                              row.get('path', ''), 'IngeTrazo (*.igz *.IGZ)')
        if not path:
            return
        row['path'] = os.path.abspath(path)
        row['source_mtime_ns'] = _source_state(path)[1]
        row['updated_at'] = _utc_now()
        was_loaded = component_for(row) is not None

        def relocate_row(target):
            unload_row(target)
            if was_loaded:
                load_row(target)

        def relocated(_done, failed):
            save(rows)
            if failed:
                QMessageBox.warning(panel, 'Localizar referência',
                                    'O novo ficheiro foi registado, mas não pôde ser carregado.')

        run_with_progress('A localizar referência', [(Path(row['path']).name, row)],
                          relocate_row, relocated)

    def capture_measurement_references():
        """Guarda as guias existentes antes de pôr o projeto base em consulta."""
        scene = app.viewport.scene
        saved = {}
        for name in ('guides', 'guide_lines', 'guide_points'):
            value = getattr(scene, name, None)
            if isinstance(value, list):
                saved[name] = list(value)
            elif isinstance(value, set):
                saved[name] = set(value)
        panel._xref_measurement_references = saved

    def clear_session_measurements():
        """Cancela a fita e remove apenas as guias criadas durante a edição."""
        tool = getattr(app.viewport, 'active_tool', None)
        cancel = getattr(tool, 'on_cancel', None)
        if callable(cancel):
            cancel(app.viewport)
        scene = app.viewport.scene
        for name, original in getattr(panel, '_xref_measurement_references', {}).items():
            current = getattr(scene, name, None)
            if isinstance(current, list):
                current[:] = original
            elif isinstance(current, set):
                current.clear()
                current.update(original)
        app.viewport.notify_scene_changed()
        app.viewport.update()

    def edit_reference():
        """Abre o IGZ de origem noutro processo, sem tocar na janela atual."""
        if editing_sessions:
            QMessageBox.information(panel, 'Editar referência',
                                    'Termine a edição da referência já aberta antes de iniciar outra.')
            return
        rows = records()
        row = selected_row(rows)
        if row is None:
            return
        path = row.get('path', '')
        if _source_state(path)[0] == 'Em falta':
            QMessageBox.warning(panel, 'Editar referência',
                                'O ficheiro de referência não está disponível no caminho indicado.')
            return
        executable = QCoreApplication.applicationFilePath()
        source_path = os.path.abspath(path)
        active_editor = _edit_lock_info(source_path)
        if active_editor is not None:
            QMessageBox.warning(
                panel, 'Editar referência',
                'A referência já está em edição por {user} no PC {host}.'.format(
                    user=active_editor.get('user', 'desconhecido'),
                    host=active_editor.get('host', 'desconhecido')))
            return
        session_path = Path(tempfile.gettempdir()) / f'qtc_xref_edit_{uuid.uuid4().hex}.json'
        lock_path = _new_edit_lock_path(source_path)
        lock = {'host': socket.gethostname(), 'user': getpass.getuser(),
                'source_path': source_path, 'heartbeat': time.time()}
        session = {
            'status': 'editing',
            'reference_id': row['id'],
            'source_path': source_path,
            'lock_path': str(lock_path),
            'parent_pid': os.getpid(),
        }
        try:
            lock_path.write_text(json.dumps(lock), encoding='utf-8')
            session_path.write_text(json.dumps(session), encoding='utf-8')
        except OSError as exc:
            try:
                lock_path.unlink()
            except OSError:
                pass
            QMessageBox.critical(panel, 'Editar referência',
                                 f'Não foi possível iniciar a sessão de edição.\n\n{exc}')
            return
        # O núcleo usa ``--new-window`` para a ação Arquivo → Nova janela.
        # Sem este argumento, a política de instância única encaminha o IGZ
        # para a janela atual. Com ele, o processo novo mantém-se autónomo e
        # o projeto central/seleção desta janela são preservados.
        process = QProcess(panel)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('QTC_XREF_EDIT_SESSION', str(session_path))
        process.setProcessEnvironment(environment)
        process.setProgram(executable)
        process.setArguments(['--new-window', source_path])
        process.setWorkingDirectory(str(Path(source_path).parent))
        started = bool(executable and process.startDetached())
        if not started:
            try:
                session_path.unlink()
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass
            QMessageBox.critical(panel, 'Editar referência',
                                 'Não foi possível abrir a referência numa nova janela.')
            return
        editing_sessions[str(session_path)] = row['id']
        capture_measurement_references()
        main_window = getattr(app, 'window', None)
        set_filter = getattr(main_window, 'set_tool_filter', None)
        activate_tool = getattr(main_window, '_activate_tool', None)
        if callable(set_filter):
            set_filter({'tape'})
        if callable(activate_tool):
            activate_tool('tape')
        readonly_guard = _ReadOnlyViewportGuard(app.viewport)
        app.viewport.installEventFilter(readonly_guard)
        panel._xref_readonly_guard = readonly_guard
        if main_window is not None:
            main_window_guard = _EditModeWindowGuard(main_window)
            main_window.installEventFilter(main_window_guard)
            panel._xref_main_edit_window_guard = main_window_guard
            panel._xref_main_disabled_actions = _disable_document_switch_actions(main_window)

    def finish_reference_edit():
        """Guarda a referência editada, avisa o projeto base e fecha a janela."""
        if not edit_session_path or not isinstance(edit_session, dict):
            return
        window = getattr(app, 'window', None)
        confirm_discard = getattr(window, '_confirm_discard', None)
        if not callable(confirm_discard):
            QMessageBox.critical(panel, 'Terminar edição',
                                 'A janela atual não disponibiliza a confirmação de gravação.')
            return
        is_dirty = getattr(window, '_is_dirty', lambda: True)
        if is_dirty() and not confirm_discard('Terminar a edição da referência?'):
            return
        # A confirmação nativa já gravou, ou o utilizador confirmou que quer
        # perder as alterações. No segundo caso marcamos a sessão como limpa
        # apenas para o closeEvent não perguntar uma segunda vez.
        if is_dirty():
            window._saved_version = window.viewport.scene.version
        if is_dirty():
            QMessageBox.warning(panel, 'Terminar edição',
                                'Guarde a referência antes de fechar a janela.')
            return
        try:
            edit_session['status'] = 'completed'
            Path(edit_session_path).write_text(json.dumps(edit_session), encoding='utf-8')
            Path(edit_session['lock_path']).unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.critical(panel, 'Terminar edição',
                                 f'A referência foi guardada, mas não foi possível avisar o projeto principal.\n\n{exc}')
            return
        overlay = getattr(panel, '_xref_edit_overlay', None)
        if overlay is not None:
            overlay.remove()
            panel._xref_edit_overlay = None
        restore_edit_window_commands()
        window.close()

    def edit_discipline(item, column):
        if column != 3:
            return
        rows = records()
        row = next((entry for entry in rows if entry.get('id') == item.data(0, Qt.UserRole)), None)
        if row is None:
            return
        row['discipline'] = item.text(3).strip()
        save(rows)

    def start_edit(item, column):
        if item is not None and column == 3:
            tree.editItem(item, column)

    toolbar_actions = [
        ('add_reference.svg', 'Adicionar referência IGZ', add),
        ('export_project.svg', 'Exportar projeto completo para IGP', export_igp),
        ('export_compact.svg', 'Exportar IGZ compacto sem vínculos externos', export_compact_igz),
        ('open_project.svg', 'Abrir e restaurar projeto IGP', open_igp),
        ('update_all.svg', 'Sincronizar todas as referências carregadas', sync_all),
        ('update_state.svg', 'Atualizar estados e libertar bloqueios expirados', refresh_states),
        ('verify_paths.svg', 'Verificar caminhos de todas as referências', verify),
    ]
    if edit_session is not None:
        toolbar_actions.append(
            ('finish_edit.svg', 'Terminar edição, guardar e atualizar o projeto principal',
             finish_reference_edit))
    action_buttons = []
    for icon_name, tooltip, callback in toolbar_actions:
        button = QToolButton(toolbar)
        button.setIcon(QIcon(str(_ICON_DIR / icon_name)))
        button.setIconSize(QSize(30, 30))
        button.setFixedSize(34, 34)
        button.setAutoRaise(True)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        buttons.addWidget(button)
        action_buttons.append(button)
    def context_menu(pos):
        item = tree.itemAt(pos)
        menu = QMenu(tree)
        if item is None:
            menu.addAction(QIcon(str(_ICON_DIR / 'add_reference.svg')), 'Adicionar…', add)
            menu.addAction(QIcon(str(_ICON_DIR / 'update_all.svg')), 'Sincronizar tudo', sync_all)
            menu.addAction(QIcon(str(_ICON_DIR / 'update_state.svg')), 'Atualizar estados', refresh_states)
            menu.addAction(QIcon(str(_ICON_DIR / 'verify_paths.svg')), 'Verificar', verify)
        else:
            tree.setCurrentItem(item)
            rows = records()
            row = selected_row(rows)
            if row is None:
                return
            loaded = component_for(row) is not None
            available = _source_state(row.get('path', ''))[0] != 'Em falta'
            if available:
                icon, text = ('unload_objects.svg', 'Descarregar') if loaded else ('load_objects.svg', 'Carregar')
                menu.addAction(QIcon(str(_ICON_DIR / icon)), text, load_or_unload)
                if loaded:
                    menu.addAction(QIcon(str(_ICON_DIR / 'update_state.svg')), 'Sincronizar', update_selected)
            menu.addAction(QIcon(str(_ICON_DIR / 'file_igz.svg')), 'Editar referência', edit_reference)
            menu.addAction(QIcon(str(_ICON_DIR / 'relocate_file.svg')), 'Localizar…', relocate)
            menu.addSeparator()
            menu.addAction(QIcon(str(_ICON_DIR / 'remove_reference.svg')), 'Remover', remove)
        menu.exec(tree.viewport().mapToGlobal(pos))

    def header_menu(pos):
        """Escolhe as colunas auxiliares sem esconder a referência ou estado."""
        menu = QMenu(tree)
        for column, label in ((2, 'Extensão'), (3, 'Descrição'),
                              (4, 'Atualização')):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not tree.isColumnHidden(column))
            action.toggled.connect(
                lambda visible, col=column: tree.setColumnHidden(col, not visible))
        menu.exec(tree.header().viewport().mapToGlobal(pos))

    search.textChanged.connect(refresh)
    # ``itemSelectionChanged`` também é emitido durante arrastos sobre a
    # árvore. Para não recalcular a vista a cada linha atravessada, só o
    # clique efetivo numa referência espelha a seleção na vista 3D.
    tree.itemClicked.connect(select_tree_reference)
    tree.itemChanged.connect(edit_discipline)
    tree.itemDoubleClicked.connect(start_edit)
    tree.customContextMenuRequested.connect(context_menu)
    tree.header().customContextMenuRequested.connect(header_menu)
    scene_selection_timer = QTimer(panel)
    scene_selection_timer.setInterval(150)
    scene_selection_timer.timeout.connect(sync_view_selection_to_tree)
    scene_selection_timer.start()
    panel._xref_scene_selection_timer = scene_selection_timer
    _PANEL_DOCK = app.add_panel('QTC Referências externas', panel)
    _PANEL_DOCK.hide()

    def reveal_docked_panel():
        panel.setAttribute(Qt.WA_DontShowOnScreen, False)
        panel.show()
        toolbar.show()
        panel.updateGeometry()
        _PANEL_DOCK.show()

    QTimer.singleShot(200, reveal_docked_panel)
    _PANEL_DOCK.visibilityChanged.connect(lambda shown: refresh() if shown else None)
    enter_filter = _RefreshOnEnter(refresh, panel)
    panel._xref_enter_filter = enter_filter  # manter vivo durante toda a sessão
    empty_click_filter = _ClearSelectionOnEmptyTreeClick(
        tree, clear_view_selection, panel)
    panel._xref_empty_click_filter = empty_click_filter
    tree.viewport().installEventFilter(empty_click_filter)
    viewport_selection_filter = _MarkUserViewportSelection(
        app.viewport, lambda: user_tree_selection.__setitem__('value', True), panel)
    panel._xref_viewport_selection_filter = viewport_selection_filter
    # O filtro é global porque o clique pode ser entregue ao widget OpenGL
    # interno, e não diretamente ao contentor ``viewport``.
    QApplication.instance().installEventFilter(viewport_selection_filter)
    for widget in (panel, tree, tree.viewport(), tree.header(), search,
                   *action_buttons):
        widget.installEventFilter(enter_filter)
    periodic_check = QTimer(panel)
    periodic_check.setInterval(XREF_CHECK_INTERVAL_MS)
    periodic_check.timeout.connect(refresh)
    periodic_check.start()
    panel._xref_periodic_check = periodic_check

    if isinstance(edit_session, dict) and edit_session.get('lock_path'):
        def refresh_edit_lock():
            if edit_session.get('status') != 'editing':
                return
            lock = {'host': socket.gethostname(), 'user': getpass.getuser(),
                    'source_path': edit_session.get('source_path', ''),
                    'heartbeat': time.time()}
            try:
                Path(edit_session['lock_path']).write_text(json.dumps(lock), encoding='utf-8')
            except OSError:
                pass

        refresh_edit_lock()
        lock_heartbeat_timer = QTimer(panel)
        lock_heartbeat_timer.setInterval(20 * 1000)
        lock_heartbeat_timer.timeout.connect(refresh_edit_lock)
        lock_heartbeat_timer.start()
        panel._xref_lock_heartbeat_timer = lock_heartbeat_timer

    def complete_edit_session(session_name, reference_id):
        """Recarrega no projeto central a referência acabada de editar."""
        clear_session_measurements()

        def restore_base_tools():
            if editing_sessions:
                return
            main_window = getattr(app, 'window', None)
            set_filter = getattr(main_window, 'set_tool_filter', None)
            if callable(set_filter):
                set_filter(None)
            activate_tool = getattr(main_window, '_activate_tool', None)
            if callable(activate_tool):
                activate_tool('select')
            readonly_guard = getattr(panel, '_xref_readonly_guard', None)
            if readonly_guard is not None:
                app.viewport.removeEventFilter(readonly_guard)
                panel._xref_readonly_guard = None
            main_window_guard = getattr(panel, '_xref_main_edit_window_guard', None)
            if main_window_guard is not None and main_window is not None:
                main_window.removeEventFilter(main_window_guard)
                panel._xref_main_edit_window_guard = None
            _restore_document_switch_actions(
                getattr(panel, '_xref_main_disabled_actions', ()))
            panel._xref_main_disabled_actions = []
            if main_window is not None:
                main_window.show()
                main_window.raise_()
                main_window.activateWindow()

        rows = records()
        row = next((entry for entry in rows if entry.get('id') == reference_id), None)
        if row is None:
            editing_sessions.pop(session_name, None)
            restore_base_tools()
            try:
                Path(session_name).unlink()
            except OSError:
                pass
            return

        def reload(target):
            if component_for(target) is None:
                return
            target['syncing'] = True
            try:
                unload_row(target)
                load_row(target)
            finally:
                target['syncing'] = False

        def completed(_done, _failed):
            save(rows)
            editing_sessions.pop(session_name, None)
            restore_base_tools()
            # A importação da versão editada seleciona o componente no núcleo;
            # ao regressar ao projeto principal a vista deve ficar neutra.
            clear_after_xref_operation()
            try:
                Path(session_name).unlink()
            except OSError:
                pass

        run_with_progress('A atualizar referência editada',
                          [(Path(row['path']).name, row)], reload, completed)

    def poll_edit_sessions():
        if progress_dialog['value'] is not None:
            return
        for session_name, reference_id in tuple(editing_sessions.items()):
            try:
                session = json.loads(Path(session_name).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if session.get('status') == 'completed':
                complete_edit_session(session_name, reference_id)
                break

    edit_poll_timer = QTimer(panel)
    edit_poll_timer.setInterval(500)
    edit_poll_timer.timeout.connect(poll_edit_sessions)
    edit_poll_timer.start()
    panel._xref_edit_poll_timer = edit_poll_timer

    refresh_document_lock = lambda: None
    release_document_lock = lambda: None
    restore_edit_window_commands = lambda: None
    window = getattr(app, 'window', None)
    if window is not None:
        if edit_session is not None:
            has_active_edits = lambda: edit_session.get('status') == 'editing'
        else:
            has_active_edits = lambda: bool(editing_sessions)
        close_guard = _EditCloseGuard(has_active_edits, window)
        window.installEventFilter(close_guard)
        panel._xref_close_guard = close_guard
        if edit_session is not None:
            edit_overlay = _EditModeOverlay(app.viewport)
            panel._xref_edit_overlay = edit_overlay

            disabled_new_actions = []

            disabled_new_actions = _disable_document_switch_actions(window)

            edit_window_guard = _EditModeWindowGuard(window)
            window.installEventFilter(edit_window_guard)
            panel._xref_edit_window_guard = edit_window_guard

            def restore_edit_window_commands():
                _restore_document_switch_actions(disabled_new_actions)
                window.removeEventFilter(edit_window_guard)

            def release_edit_session_lock():
                """Liberta o lock se esta instância terminar sem o botão."""
                lock_name = edit_session.get('lock_path')
                if not lock_name:
                    return
                try:
                    Path(lock_name).unlink(missing_ok=True)
                except OSError:
                    pass

            # O Qt pode terminar o processo sem destruir cada janela de forma
            # observável; ``aboutToQuit`` cobre o fecho da aplicação.
            panel._xref_release_edit_session_lock = release_edit_session_lock
            QCoreApplication.instance().aboutToQuit.connect(release_edit_session_lock)

        document_lock = {'path': None, 'source_path': None}

        def release_document_lock():
            lock_path = document_lock.get('path')
            document_lock['path'] = None
            document_lock['source_path'] = None
            if lock_path is None:
                return
            try:
                lock_path.unlink()
            except OSError:
                pass

        def refresh_document_lock():
            """Publica que este IGZ está aberto, também fora do menu XREF."""
            if edit_session is not None:
                return  # a sessão explícita já tem o seu lock e heartbeat
            current_path = getattr(window, '_current_path', None)
            if current_path is None or Path(current_path).suffix.casefold() != '.igz':
                release_document_lock()
                return
            source_path = os.path.abspath(str(current_path))
            if (document_lock['path'] is not None and
                    document_lock['source_path'] != source_path):
                release_document_lock()
            lock_path = document_lock['path']
            if lock_path is None:
                # Uma presença ativa é sempre de outra janela: este processo
                # ainda não tem um lock próprio para renovar.
                if _edit_lock_info(source_path) is not None:
                    return
                lock_path = _new_edit_lock_path(source_path)
            if lock_path is None:
                return
            lock = {'host': socket.gethostname(), 'user': getpass.getuser(),
                    'source_path': source_path, 'heartbeat': time.time(),
                    'mode': 'open'}
            try:
                lock_path.write_text(json.dumps(lock), encoding='utf-8')
            except OSError:
                return
            document_lock['path'] = lock_path
            document_lock['source_path'] = source_path

        refresh_document_lock()
        document_lock_timer = QTimer(panel)
        document_lock_timer.setInterval(20 * 1000)
        document_lock_timer.timeout.connect(refresh_document_lock)
        document_lock_timer.start()
        panel._xref_document_lock_timer = document_lock_timer
        window.destroyed.connect(lambda *_: release_document_lock())
        panel._xref_release_document_lock = release_document_lock
        QCoreApplication.instance().aboutToQuit.connect(release_document_lock)

        # A presença remota deve aparecer sem esperar pela verificação normal
        # de 10 minutos dos caminhos e datas.
        presence_timer = QTimer(panel)
        presence_timer.setInterval(EDIT_LOCK_CHECK_INTERVAL_MS)
        presence_timer.timeout.connect(refresh)
        presence_timer.start()
        panel._xref_presence_timer = presence_timer
        QTimer.singleShot(750, refresh_document_lock)

    scene_marker = {'id': id(app.scene)}
    opening_sync = {'pending': False}

    def schedule_opening_sync():
        """Agenda uma única sincronização após os dados do projeto assentarem."""
        if opening_sync['pending']:
            return
        opening_sync['pending'] = True

        def run():
            opening_sync['pending'] = False
            sync_on_document_open()

        QTimer.singleShot(750, run)

    def document_changed():
        current_scene_id = id(app.scene)
        current_signature = records_signature(records())
        scene_replaced = current_scene_id != scene_marker['id']
        scene_marker['id'] = current_scene_id
        if scene_replaced:
            # Fechar ou trocar de desenho substitui a cena. O lock pertencia
            # ao documento anterior e tem de desaparecer de imediato, sem
            # esperar pelo próximo heartbeat de 20 segundos.
            release_document_lock()
        # Alterações gravadas pelo próprio gestor (carregar, sincronizar,
        # descrição, etc.) não significam que foi aberto outro projeto.
        # Depois da abertura, a verificação limita-se a mudar o estado visual.
        if not scene_replaced and current_signature == saved_signature['value']:
            refresh()
            return
        QTimer.singleShot(750, refresh_document_lock)
        schedule_opening_sync()

    app.on_document_changed(document_changed)
    # Quando o plugin inicia com um documento já aberto pode não receber uma
    # notificação de abertura, pelo que faz a única sincronização inicial.
    schedule_opening_sync()
    refresh()
    if edit_session is not None:
        # A janela aberta por «Editar referência» entra logo no contexto certo.
        QTimer.singleShot(0, show_manager)


def show_manager():
    """Mostra o painel quando a ferramenta for escolhida em Extensões."""
    dock = _PANEL_DOCK
    if dock is None:
        return
    dock.show()
    dock.raise_()
    dock.activateWindow()


class QTCReferenciasExternasTool(Tool):
    """Entrada do gestor no menu Extensões do IngeTrazo."""

    name = 'QTC Referências externas'
    shortcut = None
    description = 'Gere vínculos para ficheiros IGZ externos.'

    def on_activate(self, viewport):
        show_manager()

    def on_deactivate(self, viewport):
        pass
