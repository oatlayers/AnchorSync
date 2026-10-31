import json
import os
import sys
import tempfile
import unittest
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from sync_core import reconcile, save_manifest, sidecar_name  # noqa: E402


class SimulatedCrash(BaseException):
    """A BaseException so reconcile cannot catch it as an ordinary error."""


class BaseSyncTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = self._temporary.name
        self.books = os.path.join(self.root, 'books')
        self.trash = os.path.join(self.root, 'trash')
        self.manifest = os.path.join(self.root, 'config', 'manifest.json')
        os.makedirs(self.books)

    def tearDown(self):
        self._temporary.cleanup()

    def new_stage(self):
        path = os.path.join(self.root, 'stage-' + uuid.uuid4().hex)
        os.makedirs(path)
        return path

    def stage_file(self, book_id, name, content=b'book', stage=None):
        stage = stage or self.new_stage()
        path = os.path.join(stage, '__%s__%s' % (book_id, name))
        with open(path, 'wb') as stream:
            stream.write(content)
        return path

    def expected_for(self, books):
        expected = {}
        for book_id, uuid_value, filename, _content in books:
            expected[int(book_id)] = {
                'uuid': uuid_value,
                'title': os.path.splitext(filename)[0],
                'authors': 'Author',
                'template': '{title} - {authors}',
            }
        return expected

    def sync_books(self, books, manifest=None, stage=None, **kwargs):
        stage = stage or self.new_stage()
        expected = self.expected_for(books)
        for book_id, _uuid_value, filename, content in books:
            self.stage_file(book_id, filename, content, stage=stage)
        result = reconcile(
            self.books,
            stage,
            expected,
            manifest or self.manifest,
            **kwargs
        )
        return result, stage, expected

    def write_book(self, filename, content=b'book', book_dir=None):
        root = book_dir or self.books
        path = os.path.join(root, filename)
        with open(path, 'wb') as stream:
            stream.write(content)
        return path

    def add_sidecar(self, filename, content=b'return { progress = 7 }',
                    book_dir=None, metadata_name='metadata.epub.lua'):
        root = book_dir or self.books
        sidecar = os.path.join(root, sidecar_name(filename))
        os.makedirs(sidecar, exist_ok=True)
        metadata = os.path.join(sidecar, metadata_name)
        with open(metadata, 'wb') as stream:
            stream.write(content)
        return sidecar, metadata

    def read_manifest(self, path=None):
        with open(path or self.manifest, 'r', encoding='utf-8') as stream:
            return json.load(stream)

    def read_bytes(self, path=None):
        with open(path or self.manifest, 'rb') as stream:
            return stream.read()

    def write_raw_manifest(self, data, path=None):
        path = path or self.manifest
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        return path

    def make_manifest(self, count, state='active', prefix='Book'):
        books = {}
        for index in range(1, count + 1):
            filename = '%s %03d - Author.epub' % (prefix, index)
            books['uuid-%03d' % index] = {
                'id': index,
                'filename': filename,
                'filename_locked': filename,
                'format': 'epub',
                'sha256': '0' * 64,
                'size': 1,
                'state': state,
            }
        return {
            'version': 2,
            'library_id': 'test-library',
            'generation': 1,
            'host': 'test-host',
            'books': books,
        }

    def save_manifest(self, manifest, path=None):
        return save_manifest(path or self.manifest, manifest)

    def tree_snapshot(self, root):
        snapshot = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                snapshot[os.path.relpath(os.path.join(dirpath, name), root) + '/'] = None
            for name in filenames:
                path = os.path.join(dirpath, name)
                with open(path, 'rb') as stream:
                    snapshot[os.path.relpath(path, root)] = stream.read()
        return snapshot

    def assert_no_temp_files(self, root=None):
        root = root or self.books
        leftovers = [
            name for name in os.listdir(root)
            if name.startswith('.anchorsync-')
        ]
        self.assertEqual(leftovers, [])
