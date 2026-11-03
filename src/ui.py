import os
import shutil
import tempfile
import threading

from qt.core import (
    QDialog,
    QDialogButtonBox,
    QIcon,
    QInputDialog,
    QLabel,
    QPixmap,
    QPlainTextEdit,
    QTimer,
    QVBoxLayout,
)

from calibre.gui2 import (
    Dispatcher,
    error_dialog,
    info_dialog,
    question_dialog,
    warning_dialog,
)
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.save import Saver
from calibre.library.save_to_disk import config as save_config

from calibre_plugins.anchorsync.config import manifest_path, prefs
from calibre_plugins.anchorsync.sync_core import (
    CLEANUP_CONFIRM_THRESHOLD,
    NEW_BOOK_TEMPLATE,
    SyncError,
    apply_cleanup,
    cleanup_preview,
    list_trash_runs,
    rebuild_manifest,
    reconcile,
    restore_trash_run,
    validate_book_dir,
    validate_template,
)


class SyncSaver(Saver):
    def __init__(self, *args, finished_callback=None, **kwargs):
        self.finished_callback = finished_callback
        super().__init__(*args, **kwargs)

    def on_complete(self):
        if self.finished_callback is not None:
            self.finished_callback(self)


class CleanupPreviewDialog(QDialog):
    def __init__(self, parent, preview, trash_dir):
        QDialog.__init__(self, parent)
        self.setWindowTitle('Cleanup preview')
        self.resize(920, 620)
        layout = QVBoxLayout(self)

        items = preview['items']
        lines = [
            '%d book(s) were removed from Calibre and will be moved to the trash.' % len(items),
            '',
        ]
        for item in items:
            detail = [
                item['filename'],
                'format: %s' % (item.get('format') or 'unknown'),
            ]
            if item.get('sidecar'):
                detail.append('sidecar: %s' % item['sidecar'])
            elif item.get('sidecar_ambiguous'):
                detail.append('sidecar: ambiguous name, left in place')
            if not item.get('book_exists'):
                detail.append('book file is already missing')
            lines.append('  ' + ' | '.join(detail))
        if not items:
            lines.append('  (none)')
        lines.extend([
            '',
            'Trash run: %s' % os.path.join(trash_dir, '<run-id>'),
            '',
            'Unmanaged books, Syncthing conflict copies, and orphan sidecars are never '
            'included in cleanup. Nothing is permanently deleted.',
        ])
        text = QPlainTextEdit(self)
        text.setReadOnly(True)
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)

        note = QLabel(
            'The book and its adjacent .sdr sidecar move together. If one move fails, '
            'the operation is rolled back and stopped. You can restore a trash run later.',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Move to trash')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class InterfacePlugin(InterfaceAction):
    name = 'AnchorSync'
    action_spec = (
        'Sync to KOReader',
        None,
        'Export the Calibre library to the flat KOReader folder',
        'Ctrl+Shift+K',
    )
    action_type = 'current'
    action_add_menu = True
    action_menu_clone_qaction = 'Sync to KOReader'

    def genesis(self):
        self._load_icon()
        self.qaction.triggered.connect(self.sync)
        self._sync_running = False
        self._operation = None
        self._stage = None
        self._settings_snapshot = None
        self._expected = None
        self._library_id = ''
        self._replace_corrupt = False
        self._sync_errors = {}
        self._result_dispatcher = Dispatcher(self._operation_finished, parent=self.gui)

        menu = self.qaction.menu()
        self._menu_actions = [
            self.create_menu_action(
                menu,
                'anchorsync-preview-sync',
                'Preview sync (dry run)',
                description='Show what a sync would copy without changing any files.',
                triggered=self.preview_sync,
                shortcut=False,
            ),
            self.create_menu_action(
                menu,
                'anchorsync-cleanup',
                'Cleanup removed books...',
                description='Preview and move books removed from Calibre to the trash.',
                triggered=self.cleanup,
                shortcut=False,
            ),
            self.create_menu_action(
                menu,
                'anchorsync-restore',
                'Restore from trash...',
                description='Restore a previous cleanup run when the destination is free.',
                triggered=self.restore,
                shortcut=False,
            ),
            self.create_menu_action(
                menu,
                'anchorsync-rebuild-manifest',
                'Rebuild manifest (read-only)...',
                description='Adopt existing folder names into a missing or corrupt manifest without moving books.',
                triggered=self.rebuild,
                shortcut=False,
            ),
        ]

    def _load_icon(self):
        """Use the icon packed in images/ for the toolbar button."""
        names = tuple(
            'images/icon-%d.png' % size
            for size in (16, 24, 32, 48, 64, 128, 256)
        )
        resources = {}
        plugin = self.interface_action_base_plugin
        if plugin is not None:
            try:
                resources = plugin.load_resources(names)
            except (AttributeError, OSError, ValueError):
                pass
        if not resources:
            try:
                resources = self.load_resources(names)
            except (AttributeError, OSError, ValueError):
                pass
        icon = QIcon()
        source_dir = os.path.dirname(os.path.abspath(__file__))
        for name in names:
            pixmap = QPixmap()
            data = resources.get(name)
            if data:
                loaded = pixmap.loadFromData(data, 'PNG')
            else:
                path = os.path.join(source_dir, name.replace('/', os.sep))
                loaded = pixmap.load(path)
            if loaded and not pixmap.isNull():
                icon.addPixmap(pixmap)
        if not icon.isNull():
            self.qaction.setIcon(icon)
            self.menuless_qaction.setIcon(icon)

    # ------------------------------------------------------------------
    # Action state
    # ------------------------------------------------------------------

    def location_selected(self, loc):
        self._set_actions_enabled(loc == 'library' and not self._sync_running)

    def _set_actions_enabled(self, enabled):
        self.qaction.setEnabled(enabled)
        for action in getattr(self, '_menu_actions', ()):
            action.setEnabled(enabled)

    def _set_running(self, running):
        self._sync_running = running
        self._set_actions_enabled(not running)

    def shutting_down(self):
        # Every sync is manual.  There is no listener, timer, or database hook.
        self._stage = None

    # ------------------------------------------------------------------
    # Settings and library data
    # ------------------------------------------------------------------

    def _settings(self, require_trash=False):
        book_dir_value = (prefs['book_dir'] or '').strip()
        if not book_dir_value:
            self._configure()
            return None
        try:
            template = validate_template(prefs['filename_template'])
        except SyncError as err:
            error_dialog(self.gui, 'AnchorSync', str(err), show=True)
            return None
        trash_value = (prefs['trash_dir'] or '').strip()
        if require_trash and not trash_value:
            error_dialog(
                self.gui,
                'AnchorSync',
                'Set the deleted-book trash folder in the plugin settings before using this action.',
                show=True,
            )
            return None
        book_dir = os.path.abspath(os.path.expanduser(book_dir_value))
        trash_dir = os.path.abspath(os.path.expanduser(trash_value)) if trash_value else ''
        library_dir = os.path.abspath(str(self.gui.current_db.library_path))
        try:
            validate_book_dir(book_dir, library_dir, trash_dir)
        except SyncError as err:
            error_dialog(self.gui, 'AnchorSync', str(err), show=True)
            return None
        return {
            'book_dir': book_dir,
            'trash_dir': trash_dir,
            'template': template,
            'library_dir': library_dir,
            'library_id': str(self.gui.current_db.new_api.library_id),
        }

    def _library_data(self):
        db = self.gui.current_db.new_api
        expected = {}
        epub_ids = []
        pdf_ids = []
        template = (prefs['filename_template'] or NEW_BOOK_TEMPLATE).strip()
        for book_id in db.all_book_ids():
            metadata = db.get_metadata(book_id)
            uuid_value = str(metadata.uuid or 'id:%s' % book_id)
            expected[int(book_id)] = {
                'uuid': uuid_value,
                'title': metadata.title or '',
                'authors': ' & '.join(metadata.authors or ()),
                'template': template,
            }
            formats = {str(fmt).lower() for fmt in (db.formats(book_id) or ())}
            if 'epub' in formats:
                epub_ids.append(book_id)
            elif 'pdf' in formats:
                pdf_ids.append(book_id)
        return expected, epub_ids, pdf_ids, str(db.library_id)

    def _library_uuids(self):
        db = self.gui.current_db.new_api
        return {
            str(db.get_metadata(book_id).uuid or 'id:%s' % book_id)
            for book_id in db.all_book_ids()
        }

    # ------------------------------------------------------------------
    # Sync / preview / rebuild export pipeline
    # ------------------------------------------------------------------

    def sync(self):
        self._start_export_operation('sync')

    def preview_sync(self):
        self._start_export_operation('preview')

    def rebuild(self):
        if self._sync_running:
            return
        if not question_dialog(
            self.gui,
            'Rebuild manifest',
            'Rebuild the AnchorSync manifest from the current book folder and Calibre library?\n\n'
            'This never deletes or renames a book. Existing locked names and existing manifest '
            'entries are preserved. Books that cannot be matched are reported and left alone. '
            'If the existing manifest cannot be read, it is backed up as .bak.1 and replaced.',
            default_yes=False,
        ):
            return
        self._start_export_operation('rebuild', replace_corrupt=True)

    def _start_export_operation(self, mode, replace_corrupt=False):
        if self._sync_running:
            return
        settings = self._settings()
        if settings is None:
            return
        expected, epub_ids, pdf_ids, library_id = self._library_data()
        if not expected:
            error_dialog(self.gui, 'AnchorSync', 'The Calibre library is empty.', show=True)
            return
        if not epub_ids and not pdf_ids:
            error_dialog(
                self.gui,
                'AnchorSync',
                'No book in this library has an EPUB or PDF format to export.',
                show=True,
            )
            return
        stage = tempfile.mkdtemp(prefix='anchorsync-')
        self._operation = mode
        self._stage = stage
        self._settings_snapshot = settings
        self._expected = expected
        self._library_id = library_id
        self._replace_corrupt = replace_corrupt
        self._sync_errors = {}
        self._set_running(True)
        phases = []
        if epub_ids:
            phases.append(('epub', epub_ids))
        if pdf_ids:
            phases.append(('pdf', pdf_ids))
        self._start_export(phases, stage)

    def _start_export(self, phases, stage):
        if not phases:
            self._export_finished(stage)
            return
        format_name, book_ids = phases[0]
        opts = save_config().parse()
        opts.formats = format_name
        opts.template = '__{id}__' + self._settings_snapshot['template']
        opts.single_dir = True
        opts.write_opf = False
        opts.save_cover = False
        opts.save_extra_files = False
        # Calibre's PDF writer puts a fresh XMP MetadataDate in every export.
        # Rewriting metadata changes all PDF hashes on every run.  EPUB
        # metadata updates are deterministic, so keep them; copy PDFs as bytes.
        opts.update_metadata = format_name == 'epub'
        opts.asciiize = False
        opts.replace_whitespace = False
        opts.to_lowercase = False
        try:
            self._saver = SyncSaver(
                set(book_ids),
                self.gui.current_db,
                opts,
                stage,
                parent=self.gui,
                pool=self.gui.spare_pool(),
                finished_callback=lambda saver: self._export_phase_finished(
                    saver, phases[1:], stage
                ),
            )
        except Exception as err:
            self._set_running(False)
            self._sync_errors = {}
            self._operation = None
            self._settings_snapshot = None
            self._expected = None
            self._stage = None
            shutil.rmtree(stage, ignore_errors=True)
            error_dialog(self.gui, 'AnchorSync failed', str(err), show=True)

    def _export_phase_finished(self, saver, remaining, stage):
        for book_id, entries in saver.errors.items():
            self._sync_errors.setdefault(book_id, []).extend(entries)
        self._saver = None
        QTimer.singleShot(0, lambda: self._start_export(remaining, stage))

    def _export_finished(self, stage):
        worker = threading.Thread(
            target=self._worker,
            args=(stage,),
            name='AnchorSyncWorker',
            daemon=True,
        )
        worker.start()

    def _worker(self, stage):
        mode = self._operation
        settings = self._settings_snapshot
        expected = self._expected
        library_id = self._library_id
        manifest_file = manifest_path(library_id)
        try:
            if mode == 'rebuild':
                result = rebuild_manifest(
                    settings['book_dir'],
                    manifest_file,
                    expected,
                    stage_dir=stage,
                    library_id=library_id,
                    replace_corrupt=self._replace_corrupt,
                    library_dir=settings['library_dir'],
                )
            else:
                result = reconcile(
                    settings['book_dir'],
                    stage,
                    expected,
                    manifest_file,
                    settings['trash_dir'],
                    template=settings['template'],
                    library_id=library_id,
                    library_dir=settings['library_dir'],
                    dry_run=(mode == 'preview'),
                )
            failure = None
        except Exception as err:
            result = None
            failure = err
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        self._result_dispatcher({'mode': mode, 'result': result, 'failure': failure})

    # ------------------------------------------------------------------
    # Cleanup and restore
    # ------------------------------------------------------------------

    def cleanup(self):
        if self._sync_running:
            return
        settings = self._settings(require_trash=True)
        if settings is None:
            return
        manifest_file = manifest_path(settings['library_id'])
        try:
            preview = cleanup_preview(
                settings['book_dir'],
                manifest_file,
                self._library_uuids(),
                settings['trash_dir'],
                library_dir=settings['library_dir'],
            )
        except Exception as err:
            error_dialog(self.gui, 'AnchorSync cleanup', str(err), show=True)
            return
        if not preview['items']:
            info_dialog(
                self.gui,
                'AnchorSync cleanup',
                'No removed-from-Calibre books were found in the manifest. Nothing was changed.',
                show=True,
            )
            return
        dialog = CleanupPreviewDialog(self.gui, preview, settings['trash_dir'])
        if not dialog.exec():
            return
        large = preview['total'] > CLEANUP_CONFIRM_THRESHOLD
        if large and not question_dialog(
            self.gui,
            'Large cleanup',
            'This cleanup will move %d books and their sidecars to the trash.\n\n'
            'This is more than the normal %d-book threshold. Confirm the large cleanup.'
            % (preview['total'], CLEANUP_CONFIRM_THRESHOLD),
            default_yes=False,
        ):
            return
        self._operation = 'cleanup'
        self._set_running(True)
        worker = threading.Thread(
            target=self._cleanup_worker,
            args=(settings, manifest_file, self._library_uuids(), preview['token'], large),
            name='AnchorSyncCleanup',
            daemon=True,
        )
        worker.start()

    def _cleanup_worker(self, settings, manifest_file, current_uuids, token, large):
        try:
            result = apply_cleanup(
                settings['book_dir'],
                manifest_file,
                settings['trash_dir'],
                current_uuids,
                preview_token=token,
                confirm=True,
                confirm_large=large,
                library_dir=settings['library_dir'],
            )
            failure = None
        except Exception as err:
            result = None
            failure = err
        self._result_dispatcher({'mode': 'cleanup', 'result': result, 'failure': failure})

    def restore(self):
        if self._sync_running:
            return
        settings = self._settings(require_trash=True)
        if settings is None:
            return
        runs = [
            run for run in list_trash_runs(settings['trash_dir'])
            if run.get('state') != 'invalid' and run.get('remaining', 0) > 0
        ]
        if not runs:
            info_dialog(
                self.gui,
                'AnchorSync restore',
                'There are no trash runs with books left to restore.',
                show=True,
            )
            return
        labels = []
        for run in runs:
            created = run.get('created_at') or 'unknown date'
            labels.append(
                '%s  %s  (%d book(s) remaining)'
                % (run['run_id'], created, run.get('remaining', 0))
            )
        choice, accepted = QInputDialog.getItem(
            self.gui,
            'Restore from trash',
            'Trash run:',
            labels,
            0,
            False,
        )
        if not accepted:
            return
        run = runs[labels.index(choice)]
        run_book_dir = run.get('book_dir') or ''
        if run_book_dir and os.path.normcase(os.path.realpath(os.path.abspath(run_book_dir))) != (
            os.path.normcase(os.path.realpath(os.path.abspath(settings['book_dir'])))
        ):
            error_dialog(
                self.gui,
                'AnchorSync restore',
                'This trash run belongs to a different book folder:\n%s\n\n'
                'Set the book folder back to that location before restoring.'
                % run_book_dir,
                show=True,
            )
            return
        if not question_dialog(
            self.gui,
            'Restore from trash',
            'Restore %d book(s) from run %s?\n\n'
            'A book is restored only if both the book and its sidecar destinations are free. '
            'Nothing is overwritten.'
            % (run.get('remaining', 0), run['run_id']),
            default_yes=False,
        ):
            return
        manifest_file = manifest_path(run.get('library_id') or settings['library_id'])
        self._operation = 'restore'
        self._set_running(True)
        worker = threading.Thread(
            target=self._restore_worker,
            args=(settings, manifest_file, run['run_id']),
            name='AnchorSyncRestore',
            daemon=True,
        )
        worker.start()

    def _restore_worker(self, settings, manifest_file, run_id):
        try:
            result = restore_trash_run(
                settings['trash_dir'],
                run_id,
                settings['book_dir'],
                manifest_file,
                confirm=True,
                library_dir=settings['library_dir'],
            )
            failure = None
        except Exception as err:
            result = None
            failure = err
        self._result_dispatcher({'mode': 'restore', 'result': result, 'failure': failure})

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _operation_finished(self, payload):
        self._set_running(False)
        self._operation = None
        self._stage = None
        self._settings_snapshot = None
        self._expected = None
        failure = payload.get('failure')
        mode = payload.get('mode')
        if failure is not None:
            error_dialog(self.gui, 'AnchorSync failed', str(failure), show=True)
            return
        result = payload.get('result')
        if mode == 'cleanup':
            self._show_cleanup_result(result)
        elif mode == 'restore':
            self._show_restore_result(result)
        elif mode == 'rebuild':
            self._show_rebuild_result(result)
        else:
            self._show_sync_result(result, dry_run=(mode == 'preview'))
        self._sync_errors = {}

    @staticmethod
    def _summarize(items, formatter, limit=10):
        lines = [formatter(item) for item in items[:limit]]
        if len(items) > limit:
            lines.append('... and %d more.' % (len(items) - limit))
        return '\n'.join(lines)

    def _show_sync_result(self, result, dry_run):
        lines = []
        if dry_run:
            lines.append('DRY RUN: no files were changed.')
            lines.append('Would copy: %d' % result['would_copy'])
            lines.append('Unchanged: %d' % result['unchanged'])
        else:
            lines.append('Copied: %d (new: %d, updated: %d)' % (
                result['copied'], result['new_files'], result['updated']
            ))
            lines.append('Unchanged: %d' % result['unchanged'])
        lines.extend([
            'Renamed: 0',
            'Deleted: 0',
            'Sidecars written: 0',
        ])
        warnings = []
        if result.get('recovery'):
            recovery = result['recovery']
            warnings.append(
                'Recovered an interrupted %s operation: %s.'
                % (recovery.get('kind') or 'sync', recovery.get('action') or 'unknown')
            )
        if result['name_drift']:
            warnings.append(
                'Name drift reported, but existing names were kept:\n'
                + self._summarize(
                    result['name_drift'],
                    lambda item: '%s -> would be %s' % (item['locked'], item['proposed']),
                )
            )
        if result['format_changed']:
            warnings.append(
                'Preferred format changed; the old file and name were kept:\n'
                + self._summarize(
                    result['format_changed'],
                    lambda item: '%s -> would be %s (%s)'
                    % (item['locked'], item['proposed'], item['proposed_format']),
                )
            )
        if result['removed_from_calibre']:
            warnings.append(
                'Removed from Calibre but left in the folder (use Cleanup to move them to trash):\n'
                + self._summarize(
                    result['removed_from_calibre'],
                    lambda item: item['filename'] + (' (+ sidecar)' if item.get('sidecar') else ''),
                )
            )
        if result['missing']:
            warnings.append(
                'Books with no EPUB/PDF export: %s' % ', '.join(map(str, result['missing'][:20]))
                + (' ...' if len(result['missing']) > 20 else '')
            )
        if result['unmanaged']:
            warnings.append(
                'Unmanaged books were left untouched:\n'
                + self._summarize(result['unmanaged'], lambda name: name)
            )
        if result['conflicts']:
            warnings.append(
                'Syncthing conflict copies were left untouched:\n'
                + self._summarize(result['conflicts'], lambda name: name)
            )
        if result['orphan_sidecars']:
            warnings.append(
                'Orphan .sdr sidecars were left untouched:\n'
                + self._summarize(result['orphan_sidecars'], lambda name: name)
            )
        if result['missing_from_folder']:
            warnings.append(
                'Still missing from the folder:\n'
                + self._summarize(result['missing_from_folder'], lambda name: name)
            )
        if result['reactivated']:
            warnings.append(
                'Reactivated books that had been cleaned up:\n'
                + self._summarize(result['reactivated'], lambda item: item['filename'])
            )
        if self._sync_errors:
            error_lines = []
            for book_id, entries in sorted(self._sync_errors.items(), key=lambda item: str(item[0])):
                for entry in entries:
                    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                        category, detail = entry[0], entry[1]
                    else:
                        category, detail = 'export', entry
                    error_lines.append('Book %s [%s]: %s' % (book_id, category, str(detail).strip()))
            warnings.append(
                'Some Calibre exports had errors. The affected books were reported and left in place:\n'
                + '\n'.join(error_lines[:10])
                + ('\n... and %d more.' % (len(error_lines) - 10) if len(error_lines) > 10 else '')
            )
        message = '\n'.join(lines)
        if warnings:
            warning_dialog(
                self.gui,
                'AnchorSync completed with warnings',
                message + '\n\n' + '\n\n'.join(warnings),
                show=True,
            )
        else:
            info_dialog(self.gui, 'AnchorSync complete', message, show=True)

    def _show_cleanup_result(self, result):
        if not result or not result.get('total'):
            info_dialog(
                self.gui,
                'AnchorSync cleanup',
                'No removed books were moved. Nothing was changed.',
                show=True,
            )
            return
        info_dialog(
            self.gui,
            'AnchorSync cleanup complete',
            'Approved %d removed book(s).\n'
            'Moved %d book file(s) and %d sidecar(s) to the trash.\n\n'
            'Trash run: %s\nLocation: %s\n\n'
            'Restore is available from the AnchorSync menu. Nothing was permanently deleted.'
            % (
                result.get('total', 0),
                result.get('books', 0),
                result.get('sidecars', 0),
                result.get('run_id') or '',
                result.get('run_dir') or '',
            ),
            show=True,
        )

    def _show_restore_result(self, result):
        lines = [
            'Restored: %d' % len(result.get('restored', [])),
            'Skipped because the destination was occupied: %d' % len(result.get('conflicts', [])),
            'Missing from the trash run: %d' % len(result.get('missing', [])),
            'Failed: %d' % len(result.get('failed', [])),
        ]
        warnings = []
        if result.get('conflicts'):
            warnings.append(
                'These books were not restored because a file already exists at the destination:\n'
                + self._summarize(
                    result['conflicts'],
                    lambda item: '%s (%s)' % (item['filename'], item.get('reason') or 'occupied'),
                )
            )
        if result.get('missing'):
            warnings.append(
                'These items could not be found in the trash run:\n'
                + self._summarize(result['missing'], lambda item: item['filename'])
            )
        if result.get('failed'):
            warnings.append(
                'These items failed to restore:\n'
                + self._summarize(
                    result['failed'],
                    lambda item: '%s (%s)' % (item['filename'], item.get('reason') or 'error'),
                )
            )
        if result.get('remaining'):
            warnings.append(
                'Still in the trash run:\n'
                + self._summarize(result['remaining'], lambda name: name)
            )
        message = '\n'.join(lines)
        if warnings:
            warning_dialog(
                self.gui,
                'AnchorSync restore completed with warnings',
                message + '\n\n' + '\n\n'.join(warnings),
                show=True,
            )
        else:
            info_dialog(self.gui, 'AnchorSync restore complete', message, show=True)

    def _show_rebuild_result(self, result):
        lines = [
            'Adopted existing files: %d' % len(result.get('matched', [])),
            'Preserved manifest entries: %d' % result.get('preserved_entries', 0),
            'Not matched: %d' % len(result.get('unmatched', [])),
            'Ambiguous: %d' % len(result.get('ambiguous', [])),
            '',
            'No book file was moved, renamed, or deleted.',
        ]
        warnings = []
        if result.get('incomplete'):
            warnings.append(
                'REBUILD INCOMPLETE: sync is blocked until the unmatched existing files are '
                'resolved. Move or rename them, or run Rebuild again with names that match.\n'
                + self._summarize(result.get('extra_books', []), lambda name: name)
            )
        if result.get('unmatched'):
            warnings.append(
                'These Calibre books have no matching file in the folder:\n'
                + self._summarize(
                    result['unmatched'],
                    lambda item: 'Book %s (%s)' % (item.get('id'), item.get('proposed') or 'no proposed name'),
                )
            )
        if result.get('ambiguous'):
            warnings.append(
                'These books matched more than one file and were left alone:\n'
                + self._summarize(
                    result['ambiguous'],
                    lambda item: 'Book %s: %s' % (item.get('id'), ', '.join(item.get('candidates') or [])),
                )
            )
        if self._sync_errors:
            warnings.append(
                'Some Calibre exports had errors during the rebuild; those books could not be matched.'
            )
        message = '\n'.join(lines)
        if warnings:
            warning_dialog(
                self.gui,
                'AnchorSync manifest rebuild completed with warnings',
                message + '\n\n' + '\n\n'.join(warnings),
                show=True,
            )
        else:
            info_dialog(self.gui, 'AnchorSync manifest rebuilt', message, show=True)

    def _configure(self):
        base = self.interface_action_base_plugin
        if base is not None:
            base.do_user_config(parent=self.gui)
