import hashlib
import os

from calibre.constants import config_dir
from calibre.gui2 import error_dialog
from calibre.utils.config import JSONConfig
from qt.core import QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from calibre_plugins.anchorsync.sync_core import NEW_BOOK_TEMPLATE, SyncError, validate_template


SCHEMA_VERSION = 2

prefs = JSONConfig('plugins/anchorsync')
prefs.defaults = {
    'book_dir': '',
    'filename_template': NEW_BOOK_TEMPLATE,
    'trash_dir': os.path.join(config_dir, 'anchorsync_trash'),
    'schema_version': SCHEMA_VERSION,
}


def _migrate_preferences():
    """Apply current preference defaults and drop legacy settings.

    Existing book names are frozen by the manifest, so changing the template
    here only affects books that enter the folder for the first time.
    """
    stored_version = prefs['schema_version'] if 'schema_version' in prefs else 0
    try:
        stored_version = int(stored_version or 0)
    except (TypeError, ValueError):
        stored_version = 0
    if stored_version >= SCHEMA_VERSION:
        return
    prefs['filename_template'] = NEW_BOOK_TEMPLATE
    # These settings must not exist in the new plugin.  JSONConfig silently
    # ignores missing keys when they are deleted.
    del prefs['auto_sync']
    del prefs['delete_orphans']
    prefs['schema_version'] = SCHEMA_VERSION


_migrate_preferences()


def manifest_path(library_id=''):
    suffix = ''
    if library_id:
        suffix = '-' + hashlib.sha1(str(library_id).encode('utf-8')).hexdigest()[:12]
    return os.path.join(config_dir, 'anchorsync_manifest%s.json' % suffix)


class ConfigWidget(QWidget):
    def __init__(self):
        QWidget.__init__(self)
        self.setLayout(QFormLayout())

        self.book_dir = QLineEdit(prefs['book_dir'], self)
        self.layout().addRow(QLabel('Book folder'), self._path_row(self.book_dir, 'Choose book folder'))

        self.filename_template = QLineEdit(prefs['filename_template'], self)
        self.layout().addRow(QLabel('New-book filename template'), self.filename_template)

        template_help = QLabel(
            'Used only when a book first enters the folder, normally '
            '{title} - {authors}. Existing filenames are frozen by the manifest and are '
            'never changed by a template, title, author, or page-count edit. Page-count '
            'placeholders are rejected. The extension is added automatically.'
        )
        template_help.setWordWrap(True)
        self.layout().addRow(template_help)

        self.trash_dir = QLineEdit(prefs['trash_dir'], self)
        self.layout().addRow(
            QLabel('Deleted-book trash'), self._path_row(self.trash_dir, 'Choose local trash folder')
        )

        note = QLabel(
            'Sync is manual only. Use the AnchorSync toolbar menu for Preview sync, '
            'Cleanup removed books, and Restore from trash. A normal sync never renames, '
            'deletes, or writes KOReader .sdr sidecars. Cleanup moves only books that were '
            'in the manifest but are no longer in Calibre, and never permanently deletes.'
        )
        note.setWordWrap(True)
        self.layout().addRow(note)

    def _path_row(self, edit, button_text):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit)
        button = QPushButton(button_text, row)
        button.clicked.connect(lambda: self._choose_directory(edit))
        layout.addWidget(button)
        return row

    def _choose_directory(self, edit):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', edit.text())
        if path:
            edit.setText(path)

    def validate(self):
        if not self.book_dir.text().strip():
            self.book_dir.setFocus()
            return False
        try:
            validate_template(self.filename_template.text())
        except SyncError as err:
            self.filename_template.setFocus()
            error_dialog(self, 'AnchorSync', str(err), show=True)
            return False
        return True

    def save_settings(self):
        prefs['book_dir'] = self.book_dir.text().strip()
        prefs['filename_template'] = self.filename_template.text().strip()
        prefs['trash_dir'] = self.trash_dir.text().strip()
        prefs['schema_version'] = SCHEMA_VERSION
