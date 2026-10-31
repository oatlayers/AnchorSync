import errno
import json
import os
import unittest

try:
    from support import BaseSyncTest, SimulatedCrash
except ImportError:
    from tests.support import BaseSyncTest, SimulatedCrash

from sync_core import (
    CleanupError,
    LockError,
    ManifestError,
    RestoreError,
    SyncError,
    SyncLock,
    apply_cleanup,
    cleanup_preview,
    journal_directory,
    journal_path,
    list_trash_runs,
    manifest_exists,
    rebuild_manifest,
    reconcile,
    recover_interrupted_run,
    restore_trash_run,
    sha256_file,
    sidecar_name,
    validate_template,
)


UUID_A = '11111111-1111-1111-1111-111111111111'
UUID_B = '22222222-2222-2222-2222-222222222222'


class NormalSyncSafetyTests(BaseSyncTest):
    def test_fresh_install_copies_new_book_and_writes_manifest(self):
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'Book - Author.epub', b'book'),
        ])

        self.assertEqual(result['copied'], 1)
        self.assertEqual(result['new_files'], 1)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['unchanged'], 0)
        self.assertEqual(result['renamed'], 0)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['sidecars_written'], 0)
        self.assertEqual(result['export_complete'], True)
        self.assertEqual(result['generation'], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book - Author.epub')))
        self.assertFalse(os.path.exists(os.path.join(self.books, sidecar_name('Book - Author.epub'))))
        self.assertFalse(os.path.exists(self.manifest + '.bak.1'))

        manifest = self.read_manifest()
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['generation'], 1)
        entry = manifest['books']['uuid-1']
        self.assertEqual(entry['filename'], 'Book - Author.epub')
        self.assertEqual(entry['filename_locked'], 'Book - Author.epub')
        self.assertEqual(entry['format'], 'epub')
        self.assertEqual(entry['state'], 'active')
        self.assertIn('source_fingerprint', entry)
        self.assertIn('last_verified_hash', entry)
        self.assertIn('mtime_ns', entry)

    def test_noop_changes_nothing_and_uses_hash_cache(self):
        books = [(1, 'uuid-1', 'Book - Author.epub', b'book')]
        first, stage, expected = self.sync_books(books)
        sidecar, _metadata = self.add_sidecar('Book - Author.epub', b'progress = 7')
        snapshot = self.tree_snapshot(self.books)
        before = self.read_bytes(self.manifest)

        second = reconcile(self.books, stage, expected, self.manifest)

        self.assertEqual(second['copied'], 0)
        self.assertEqual(second['renamed'], 0)
        self.assertEqual(second['deleted'], 0)
        self.assertEqual(second['unchanged'], 1)
        self.assertEqual(second['sidecars_written'], 0)
        self.assertEqual(second['hash_cache_hits'], 1)
        self.assertEqual(second['hash_cache_misses'], 0)
        self.assertEqual(self.read_bytes(self.manifest), before)
        self.assertEqual(self.tree_snapshot(self.books), snapshot)
        self.assertTrue(os.path.isdir(sidecar))

    def test_title_change_keeps_locked_name_and_reports_drift(self):
        self.sync_books([(1, 'uuid-1', 'Old Title - Author.epub', b'old')])
        sidecar, _metadata = self.add_sidecar('Old Title - Author.epub', b'progress = 42')
        sidecar_before = self.tree_snapshot(sidecar)

        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'New Title - Author.epub', b'new'),
        ])

        old_path = os.path.join(self.books, 'Old Title - Author.epub')
        new_path = os.path.join(self.books, 'New Title - Author.epub')
        self.assertTrue(os.path.isfile(old_path))
        self.assertFalse(os.path.exists(new_path))
        with open(old_path, 'rb') as stream:
            self.assertEqual(stream.read(), b'new')
        self.assertEqual(result['copied'], 1)
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['new_files'], 0)
        self.assertEqual(result['renamed'], 0)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['sidecars_written'], 0)
        self.assertEqual(len(result['name_drift']), 1)
        self.assertEqual(result['name_drift'][0]['locked'], 'Old Title - Author.epub')
        self.assertEqual(result['name_drift'][0]['proposed'], 'New Title - Author.epub')
        self.assertEqual(self.tree_snapshot(sidecar), sidecar_before)

        manifest = self.read_manifest()
        self.assertEqual(manifest['books']['uuid-1']['filename'], 'Old Title - Author.epub')
        self.assertEqual(manifest['books']['uuid-1']['filename_locked'], 'Old Title - Author.epub')
        self.assertEqual(manifest['generation'], 2)
        self.assertEqual(manifest['backup_generation'], 1)

    def test_page_count_style_name_change_never_renames(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'book')])
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', '[321] Book - Author.epub', b'book'),
        ])

        self.assertEqual(result['renamed'], 0)
        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(len(result['name_drift']), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book - Author.epub')))
        self.assertFalse(os.path.exists(os.path.join(self.books, '[321] Book - Author.epub')))

    def test_unicode_normalization_alias_is_not_renamed(self):
        nfc = 'Caf\u00e9 - Author.epub'
        nfd = 'Cafe\u0301 - Author.epub'
        self.sync_books([(1, 'uuid-1', nfc, b'book')])
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', nfd, b'book'),
        ])

        self.assertEqual(result['renamed'], 0)
        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(len(result['name_drift']), 1)
        self.assertEqual(self.read_manifest()['books']['uuid-1']['filename'], nfc)

    def test_template_change_only_reports_drift(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'book')])
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', '[999] Book - Author.epub', b'book'),
        ])

        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(result['renamed'], 0)
        self.assertEqual(len(result['name_drift']), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book - Author.epub')))
        self.assertFalse(os.path.exists(os.path.join(self.books, '[999] Book - Author.epub')))
        self.assertEqual(
            self.read_manifest()['books']['uuid-1']['filename'],
            'Book - Author.epub',
        )

    def test_collision_suffix_is_stable(self):
        books = [
            (1, UUID_A, 'Same - Author.epub', b'a'),
            (2, UUID_B, 'Same - Author.epub', b'b'),
        ]
        first, _stage, _expected = self.sync_books(books)
        self.assertEqual(first['copied'], 2)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Same - Author.epub')))
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Same - Author [22222222].epub')))

        second, _stage2, _expected2 = self.sync_books(books)
        self.assertEqual(second['copied'], 0)
        self.assertEqual(second['unchanged'], 2)
        self.assertEqual(second['renamed'], 0)
        self.assertEqual(len(second['name_drift']), 1)
        manifest = self.read_manifest()
        self.assertEqual(manifest['books'][UUID_A]['filename'], 'Same - Author.epub')
        self.assertEqual(manifest['books'][UUID_B]['filename'], 'Same - Author [22222222].epub')

    def test_new_book_does_not_overwrite_unmanaged_file(self):
        seed = (0, 'uuid-0', 'Seed - Author.epub', b'seed')
        self.sync_books([seed])
        self.write_book('Same - Author.epub', b'unmanaged')
        result, _stage, _expected = self.sync_books([
            seed,
            (1, 'abcdef12-1111-1111-1111-111111111111', 'Same - Author.epub', b'new'),
        ])

        self.assertEqual(result['copied'], 1)
        self.assertEqual(result['unmanaged'], ['Same - Author.epub'])
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Same - Author [abcdef12].epub')))
        with open(os.path.join(self.books, 'Same - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'unmanaged')

    def test_new_book_avoids_orphan_sidecar_stem(self):
        orphan = os.path.join(self.books, 'Same - Author.sdr')
        os.makedirs(orphan)
        result, _stage, _expected = self.sync_books([
            (1, 'abcdef12-1111-1111-1111-111111111111', 'Same - Author.epub', b'new'),
        ])

        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Same - Author [abcdef12].epub')))
        self.assertTrue(os.path.isdir(orphan))
        self.assertIn('Same - Author.sdr', result['orphan_sidecars'])

    def test_conflict_copy_is_reported_and_untouched(self):
        conflict = 'Bad.sync-conflict-20260101-120000-ABCDEF.epub'
        seed = (0, 'uuid-0', 'Seed - Author.epub', b'seed')
        self.sync_books([seed])
        self.write_book(conflict, b'conflict')
        result, _stage, _expected = self.sync_books([
            seed,
            (1, 'uuid-1', 'Good - Author.epub', b'book'),
        ])

        self.assertIn(conflict, result['conflicts'])
        self.assertNotIn(conflict, result['unmanaged'])
        self.assertTrue(os.path.isfile(os.path.join(self.books, conflict)))
        with open(os.path.join(self.books, conflict), 'rb') as stream:
            self.assertEqual(stream.read(), b'conflict')

    def test_removed_book_is_left_in_place_by_normal_sync(self):
        books = [
            (index, 'uuid-%02d' % index, 'Book %02d - Author.epub' % index, ('book %d' % index).encode())
            for index in range(1, 11)
        ]
        self.sync_books(books)
        removed = books[-1]
        sidecar, _metadata = self.add_sidecar(removed[2])

        result, _stage, _expected = self.sync_books(books[:-1])

        removed_path = os.path.join(self.books, removed[2])
        self.assertTrue(os.path.isfile(removed_path))
        self.assertTrue(os.path.isdir(sidecar))
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['renamed'], 0)
        self.assertEqual(result['unchanged'], 9)
        self.assertEqual(len(result['removed_from_calibre']), 1)
        self.assertEqual(result['removed_from_calibre'][0]['uuid'], removed[1])
        self.assertEqual(self.read_manifest()['books'][removed[1]]['state'], 'active')

    def test_format_change_is_reported_and_old_file_is_kept(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'epub')])
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'Book - Author.pdf', b'pdf'),
        ])

        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 0)
        self.assertEqual(len(result['format_changed']), 1)
        self.assertEqual(result['format_changed'][0]['proposed_format'], 'pdf')
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book - Author.epub')))
        self.assertFalse(os.path.exists(os.path.join(self.books, 'Book - Author.pdf')))
        manifest = self.read_manifest()
        self.assertEqual(manifest['books']['uuid-1']['filename'], 'Book - Author.epub')
        self.assertEqual(manifest['books']['uuid-1']['format'], 'epub')

    def test_missing_format_is_reported_without_deletion(self):
        stage = self.new_stage()
        self.stage_file(2, 'Book 2 - Author.epub', b'book', stage=stage)
        expected = {
            1: {'uuid': 'uuid-1'},
            2: {'uuid': 'uuid-2'},
        }
        result = reconcile(self.books, stage, expected, self.manifest)

        self.assertEqual(result['missing'], [1])
        self.assertEqual(result['export_complete'], False)
        self.assertEqual(result['copied'], 1)
        self.assertFalse(os.path.exists(os.path.join(self.books, 'Book 1 - Author.epub')))
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book 2 - Author.epub')))

    def test_sidecar_tree_is_never_written_during_normal_sync(self):
        self.sync_books([(1, 'uuid-1', 'Old - Author.epub', b'old')])
        sidecar, _metadata = self.add_sidecar('Old - Author.epub', b'return {}')
        nested = os.path.join(sidecar, 'cache')
        os.makedirs(nested)
        with open(os.path.join(nested, 'state.lua'), 'wb') as stream:
            stream.write(b'return { page = 3 }')
        snapshot = self.tree_snapshot(sidecar)

        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'New - Author.epub', b'new'),
        ])

        self.assertEqual(result['sidecars_written'], 0)
        self.assertEqual(self.tree_snapshot(sidecar), snapshot)

    def test_legacy_page_prefix_is_not_parsed_or_stripped(self):
        source_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'sync_core.py')
        with open(source_path, 'r', encoding='utf-8') as stream:
            source = stream.read()
        self.assertNotIn('_page_prefix', source)
        self.assertNotIn('SequenceMatcher', source)
        self.assertNotIn('FUZZY_MATCH', source)

        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'book')])
        legacy = '[43] Book - Author.epub'
        self.write_book(legacy, b'legacy')
        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'Book - Author.epub', b'book'),
        ])

        self.assertIn(legacy, result['unmanaged'])
        self.assertTrue(os.path.isfile(os.path.join(self.books, legacy)))
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Book - Author.epub')))
        self.assertEqual(result['renamed'], 0)


class SafetyGuardTests(BaseSyncTest):
    def test_empty_library_aborts(self):
        self.save_manifest(self.make_manifest(1))
        before = self.read_bytes(self.manifest)
        with self.assertRaises(SyncError):
            reconcile(self.books, self.new_stage(), {}, self.manifest)
        self.assertEqual(self.read_bytes(self.manifest), before)

    def test_empty_staging_aborts(self):
        stage = self.new_stage()
        expected = {1: {'uuid': 'uuid-1'}}
        with self.assertRaises(SyncError):
            reconcile(self.books, stage, expected, self.manifest)
        self.assertFalse(manifest_exists(self.manifest))
        self.assertEqual(os.listdir(self.books), [])

    def test_aborted_sync_does_not_create_book_folder(self):
        missing_books = os.path.join(self.root, 'missing-books')
        stage = self.new_stage()
        expected = {1: {'uuid': 'uuid-1'}}
        with self.assertRaises(SyncError):
            reconcile(missing_books, stage, expected, self.manifest)
        self.assertFalse(os.path.exists(missing_books))

    def test_large_library_drop_aborts(self):
        self.save_manifest(self.make_manifest(10))
        stage = self.new_stage()
        expected = {}
        for index in range(1, 9):
            filename = 'Book %03d - Author.epub' % index
            self.stage_file(index, filename, b'book', stage=stage)
            expected[index] = {'uuid': 'uuid-%03d' % index}
        with self.assertRaises(SyncError):
            reconcile(self.books, stage, expected, self.manifest)
        self.assertEqual(os.listdir(self.books), [])

    def test_missing_manifest_with_non_empty_folder_aborts(self):
        self.write_book('Existing - Author.epub', b'existing')
        stage = self.new_stage()
        self.stage_file(1, 'New - Author.epub', b'new', stage=stage)
        expected = {1: {'uuid': 'uuid-1'}}
        with self.assertRaises(ManifestError):
            reconcile(self.books, stage, expected, self.manifest)
        self.assertFalse(manifest_exists(self.manifest))
        with open(os.path.join(self.books, 'Existing - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'existing')

    def test_lock_contention_aborts_before_changes(self):
        lock = SyncLock(self.manifest + '.lock')
        with lock:
            with self.assertRaises(LockError):
                self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'book')])
        self.assertEqual(os.listdir(self.books), [])
        self.assertFalse(manifest_exists(self.manifest))

    def test_trash_inside_book_folder_is_rejected(self):
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'book', stage=stage)
        expected = {1: {'uuid': 'uuid-1'}}
        with self.assertRaises(SyncError):
            reconcile(
                self.books,
                stage,
                expected,
                self.manifest,
                trash_dir=os.path.join(self.books, 'trash'),
            )

    def test_library_folder_overlap_is_rejected_in_all_actions(self):
        with self.assertRaises(SyncError):
            cleanup_preview(
                self.books,
                self.manifest,
                set(),
                self.trash,
                library_dir=self.books,
            )
        with self.assertRaises(SyncError):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                set(),
                confirm=True,
                library_dir=self.books,
            )
        with self.assertRaises(SyncError):
            rebuild_manifest(
                self.books,
                self.manifest,
                {},
                library_dir=self.books,
            )
        library = os.path.join(self.root, 'library')
        os.makedirs(library)
        with self.assertRaises(SyncError):
            cleanup_preview(
                self.books,
                self.manifest,
                set(),
                os.path.join(library, 'trash'),
                library_dir=library,
            )

    def test_page_count_template_is_rejected(self):
        with self.assertRaises(SyncError):
            validate_template('[{pages}] {title} - {authors}')
        with self.assertRaises(SyncError):
            validate_template('{#pages} {title} - {authors}')

        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'book', stage=stage)
        expected = {1: {'uuid': 'uuid-1'}}
        with self.assertRaises(SyncError):
            reconcile(
                self.books,
                stage,
                expected,
                self.manifest,
                template='[{pages}] {title} - {authors}',
            )
        self.assertEqual(os.listdir(self.books), [])
        self.assertFalse(manifest_exists(self.manifest))


class ManifestV2Tests(BaseSyncTest):
    def test_v1_manifest_migrates_and_preserves_locked_name(self):
        old_filename = 'Old - Author.epub'
        path = self.write_book(old_filename, b'book')
        digest = sha256_file(path)
        self.write_raw_manifest({
            'version': 1,
            'books': {
                'uuid-1': {
                    'id': 1,
                    'filename': old_filename,
                    'format': 'epub',
                    'sha256': digest,
                    'size': os.path.getsize(path),
                },
            },
        })

        result, _stage, _expected = self.sync_books([
            (1, 'uuid-1', 'New - Author.epub', b'book'),
        ])

        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(len(result['name_drift']), 1)
        manifest = self.read_manifest()
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['generation'], 1)
        entry = manifest['books']['uuid-1']
        self.assertEqual(entry['filename'], old_filename)
        self.assertEqual(entry['filename_locked'], old_filename)
        self.assertEqual(entry['state'], 'active')
        backup = self.read_manifest(self.manifest + '.bak.1')
        self.assertEqual(backup['version'], 1)
        self.assertEqual(backup['books']['uuid-1']['filename'], old_filename)

    def test_dry_run_changes_nothing(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        before = self.read_bytes(self.manifest)
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        result = reconcile(self.books, stage, expected, self.manifest, dry_run=True)

        self.assertTrue(result['dry_run'])
        self.assertEqual(result['would_copy'], 1)
        self.assertEqual(result['copied'], 0)
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'old')
        self.assertEqual(self.read_bytes(self.manifest), before)
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        self.assert_no_temp_files()

        normal = reconcile(self.books, stage, expected, self.manifest)
        self.assertEqual(normal['copied'], 1)
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'new')

    def test_manifest_rebuild_preserves_entries_and_files(self):
        self.write_book('A - Author.epub', b'a')
        self.write_book('B - Author.epub', b'b')
        self.write_book('C - Author.epub', b'c')
        self.save_manifest({
            'version': 2,
            'library_id': 'test-library',
            'generation': 1,
            'host': 'test-host',
            'books': {
                'uuid-d': {
                    'id': 4,
                    'filename': 'D - Author.epub',
                    'filename_locked': 'D - Author.epub',
                    'format': 'epub',
                    'state': 'active',
                },
            },
        })
        before = self.tree_snapshot(self.books)
        expected = {
            1: {'uuid': 'uuid-a'},
            2: {'uuid': 'uuid-b'},
        }
        proposed = {
            'uuid-a': 'A - Author.epub',
            'uuid-b': 'B - Author.epub',
        }

        result = rebuild_manifest(
            self.books, self.manifest, expected, proposed_names=proposed
        )

        self.assertEqual(len(result['matched']), 2)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(result['ambiguous'], [])
        self.assertEqual(self.tree_snapshot(self.books), before)
        manifest = self.read_manifest()
        self.assertIn('uuid-a', manifest['books'])
        self.assertIn('uuid-b', manifest['books'])
        self.assertIn('uuid-d', manifest['books'])
        self.assertEqual(manifest['generation'], 2)

        second = rebuild_manifest(
            self.books, self.manifest, expected, proposed_names=proposed
        )
        self.assertEqual(second['matched'], [])
        self.assertEqual(self.read_manifest()['generation'], 2)

    def test_manifest_rebuild_with_missing_manifest_never_deletes(self):
        self.write_book('A - Author.epub', b'a')
        self.write_book('B - Author.epub', b'b')
        before = self.tree_snapshot(self.books)
        expected = {1: {'uuid': 'uuid-a'}, 2: {'uuid': 'uuid-b'}}
        proposed = {'uuid-a': 'A - Author.epub', 'uuid-b': 'B - Author.epub'}

        result = rebuild_manifest(
            self.books, self.manifest, expected, proposed_names=proposed
        )

        self.assertEqual(len(result['matched']), 2)
        self.assertEqual(self.tree_snapshot(self.books), before)
        self.assertTrue(manifest_exists(self.manifest))
        self.assertEqual(self.read_manifest()['version'], 2)

    def test_manifest_rebuild_aborts_with_empty_export(self):
        self.write_book('A - Author.epub', b'a')
        stage = self.new_stage()
        expected = {1: {'uuid': 'uuid-a'}}
        with self.assertRaises(SyncError):
            rebuild_manifest(self.books, self.manifest, expected, stage_dir=stage)
        self.assertFalse(manifest_exists(self.manifest))
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'A - Author.epub')))

    def test_incomplete_rebuild_blocks_sync_until_resolved(self):
        self.write_book('Wrong Name - Author.epub', b'a')
        stage = self.new_stage()
        self.stage_file(1, 'Right Name - Author.epub', b'a', stage=stage)
        expected = {1: {'uuid': 'uuid-a'}}

        first = rebuild_manifest(
            self.books,
            self.manifest,
            expected,
            proposed_names={'uuid-a': 'Right Name - Author.epub'},
        )

        self.assertTrue(first['incomplete'])
        self.assertIn('Wrong Name - Author.epub', first['extra_books'])
        self.assertTrue(self.read_manifest().get('rebuild_incomplete'))
        with self.assertRaises(ManifestError):
            reconcile(self.books, stage, expected, self.manifest)

        still_incomplete = rebuild_manifest(
            self.books,
            self.manifest,
            expected,
            proposed_names={'uuid-a': 'Right Name - Author.epub'},
        )
        self.assertTrue(still_incomplete['incomplete'])
        self.assertTrue(self.read_manifest().get('rebuild_incomplete'))

        second = rebuild_manifest(
            self.books,
            self.manifest,
            expected,
            proposed_names={'uuid-a': 'Wrong Name - Author.epub'},
        )
        self.assertFalse(second['incomplete'])
        self.assertNotIn('rebuild_incomplete', self.read_manifest())

        result = reconcile(self.books, stage, expected, self.manifest)
        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(len(result['name_drift']), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Wrong Name - Author.epub')))
        self.assertFalse(os.path.exists(os.path.join(self.books, 'Right Name - Author.epub')))

    def test_manifest_rebuild_replaces_corrupt_manifest_only_on_request(self):
        self.write_book('A - Author.epub', b'a')
        os.makedirs(os.path.dirname(self.manifest), exist_ok=True)
        with open(self.manifest, 'w', encoding='utf-8') as stream:
            stream.write('{not valid json')
        expected = {1: {'uuid': 'uuid-a'}}
        proposed = {'uuid-a': 'A - Author.epub'}

        with self.assertRaises(ManifestError):
            rebuild_manifest(self.books, self.manifest, expected, proposed_names=proposed)

        result = rebuild_manifest(
            self.books,
            self.manifest,
            expected,
            proposed_names=proposed,
            replace_corrupt=True,
        )
        self.assertTrue(result['replaced_corrupt'])
        self.assertEqual(self.read_manifest()['version'], 2)
        with open(self.manifest + '.bak.1', 'r', encoding='utf-8') as stream:
            self.assertEqual(stream.read(), '{not valid json')


class JournalRecoveryTests(BaseSyncTest):
    def test_crash_after_replace_rolls_back(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        manifest_before = self.read_manifest()
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def crash(point, **_kwargs):
            if point == 'after_replace':
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            reconcile(self.books, stage, expected, self.manifest, fault=crash)

        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'new')
        self.assertTrue(os.path.exists(journal_path(self.manifest)))
        self.assertEqual(self.read_manifest()['generation'], manifest_before['generation'])

        report = recover_interrupted_run(self.manifest)

        self.assertEqual(report['action'], 'rolled_back')
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'old')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        self.assertFalse(os.path.exists(journal_directory(self.manifest)))
        self.assert_no_temp_files()

        resumed = reconcile(self.books, stage, expected, self.manifest)
        self.assertEqual(resumed['copied'], 1)
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'new')

    def test_rollback_does_not_overwrite_a_file_changed_after_crash(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def crash(point, **_kwargs):
            if point == 'after_replace':
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            reconcile(self.books, stage, expected, self.manifest, fault=crash)

        with open(os.path.join(self.books, 'Book - Author.epub'), 'wb') as stream:
            stream.write(b'user edit after crash')
        report = recover_interrupted_run(self.manifest)

        self.assertEqual(report['action'], 'rolled_back')
        self.assertTrue(report['issues'])
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'user edit after crash')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))

    def test_crash_after_manifest_write_is_committed(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        manifest_before = self.read_manifest()
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def crash(point, **_kwargs):
            if point == 'after_manifest':
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            reconcile(self.books, stage, expected, self.manifest, fault=crash)

        self.assertTrue(os.path.exists(journal_path(self.manifest)))
        self.assertGreater(self.read_manifest()['generation'], manifest_before['generation'])

        report = recover_interrupted_run(self.manifest)

        self.assertEqual(report['action'], 'committed')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'new')

    def test_disk_full_rolls_back(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def full(point, **_kwargs):
            if point == 'after_backup':
                raise OSError(errno.ENOSPC, 'disk full')

        with self.assertRaises(OSError):
            reconcile(self.books, stage, expected, self.manifest, fault=full)

        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'old')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        self.assert_no_temp_files()

    def test_permission_denied_after_replace_rolls_back(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def denied(point, **_kwargs):
            if point == 'after_replace':
                raise PermissionError(errno.EACCES, 'permission denied')

        with self.assertRaises(PermissionError):
            reconcile(self.books, stage, expected, self.manifest, fault=denied)

        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'old')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        self.assert_no_temp_files()


class CleanupTests(BaseSyncTest):
    def _two_books_with_sidecars(self):
        books = [
            (1, 'uuid-1', 'One - Author.epub', b'one'),
            (2, 'uuid-2', 'Two - Author.epub', b'two'),
        ]
        self.sync_books(books)
        self.add_sidecar(books[0][2], b'progress one')
        self.add_sidecar(books[1][2], b'progress two')
        return books

    def test_cleanup_preview_lists_only_removed_books(self):
        books = self._two_books_with_sidecars()
        self.write_book('Extra.epub', b'extra')
        self.write_book('Bad.sync-conflict-20260101-120000-ABCDEF.epub', b'conflict')
        os.makedirs(os.path.join(self.books, 'Orphan.sdr'))

        preview = cleanup_preview(
            self.books,
            self.manifest,
            {'uuid-1'},
            self.trash,
        )

        self.assertEqual(preview['total'], 1)
        item = preview['items'][0]
        self.assertEqual(item['uuid'], 'uuid-2')
        self.assertEqual(item['filename'], books[1][2])
        self.assertEqual(item['sidecar'], sidecar_name(books[1][2]))
        self.assertTrue(item['book_exists'])
        self.assertTrue(item['sidecar_exists'])
        self.assertFalse(os.path.exists(self.trash))

    def test_cleanup_moves_only_approved_book_and_sidecar(self):
        books = self._two_books_with_sidecars()
        self.write_book('Extra.epub', b'extra')
        conflict = 'Bad.sync-conflict-20260101-120000-ABCDEF.epub'
        self.write_book(conflict, b'conflict')
        orphan = os.path.join(self.books, 'Orphan.sdr')
        os.makedirs(orphan)

        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        result = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-1'},
            preview_token=preview['token'],
            confirm=True,
        )

        self.assertEqual(result['total'], 1)
        self.assertEqual(result['books'], 1)
        self.assertEqual(result['sidecars'], 1)
        self.assertFalse(os.path.exists(os.path.join(self.books, books[1][2])))
        self.assertFalse(os.path.exists(os.path.join(self.books, sidecar_name(books[1][2]))))
        self.assertTrue(os.path.isfile(os.path.join(self.books, books[0][2])))
        self.assertTrue(os.path.isdir(os.path.join(self.books, sidecar_name(books[0][2]))))
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Extra.epub')))
        self.assertTrue(os.path.isfile(os.path.join(self.books, conflict)))
        self.assertTrue(os.path.isdir(orphan))
        self.assertTrue(os.path.isfile(os.path.join(result['run_dir'], 'books', books[1][2])))
        self.assertTrue(
            os.path.isdir(os.path.join(result['run_dir'], 'sidecars', sidecar_name(books[1][2])))
        )
        with open(os.path.join(result['run_dir'], 'preview.json'), 'r', encoding='utf-8') as stream:
            stored = json.load(stream)
        self.assertEqual(stored['state'], 'complete')
        manifest = self.read_manifest()
        self.assertEqual(manifest['books']['uuid-1']['state'], 'active')
        self.assertEqual(manifest['books']['uuid-2']['state'], 'trashed')

        runs = list_trash_runs(self.trash)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['run_id'], result['run_id'])
        self.assertEqual(runs[0]['remaining'], 1)

        noop, _stage, _expected = self.sync_books([books[0]])
        self.assertEqual(noop['copied'], 0)
        self.assertEqual(noop['renamed'], 0)
        self.assertEqual(noop['deleted'], 0)
        self.assertEqual(noop['unchanged'], 1)
        self.assertEqual(noop['removed_from_calibre'], [])

    def test_cleanup_requires_confirmation(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        with self.assertRaises(CleanupError):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                {'uuid-1'},
                preview_token=preview['token'],
                confirm=False,
            )
        self.assertTrue(os.path.isfile(os.path.join(self.books, books[1][2])))
        self.assertFalse(os.path.exists(self.trash))

    def test_large_cleanup_needs_second_confirmation(self):
        books = [
            (index, 'uuid-%03d' % index, 'Book %03d - Author.epub' % index, b'book')
            for index in range(1, 12)
        ]
        self.sync_books(books)
        preview = cleanup_preview(self.books, self.manifest, set(), self.trash)
        self.assertEqual(preview['total'], 11)

        with self.assertRaises(CleanupError):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                set(),
                preview_token=preview['token'],
                confirm=True,
                confirm_large=False,
            )
        self.assertEqual(len(os.listdir(self.books)), 11)
        self.assertFalse(os.path.exists(self.trash))

        result = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            set(),
            preview_token=preview['token'],
            confirm=True,
            confirm_large=True,
        )
        self.assertEqual(result['moved'], 11)

    def test_cleanup_failure_rolls_back_the_pair(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)

        def full(point, **_kwargs):
            if point == 'cleanup_after_book_move':
                raise OSError(errno.ENOSPC, 'disk full')

        with self.assertRaises(CleanupError):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                {'uuid-1'},
                preview_token=preview['token'],
                confirm=True,
                fault=full,
            )

        self.assertTrue(os.path.isfile(os.path.join(self.books, books[1][2])))
        self.assertTrue(os.path.isdir(os.path.join(self.books, sidecar_name(books[1][2]))))
        self.assertEqual(self.read_manifest()['books']['uuid-2']['state'], 'active')
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        run_dirs = [
            name for name in os.listdir(self.trash)
            if os.path.isdir(os.path.join(self.trash, name))
        ]
        self.assertEqual(len(run_dirs), 1)
        with open(os.path.join(self.trash, run_dirs[0], 'preview.json'), 'r', encoding='utf-8') as stream:
            stored = json.load(stream)
        self.assertEqual(stored['state'], 'rolled_back')
        self.assertEqual(list_trash_runs(self.trash)[0]['remaining'], 0)

    def test_cleanup_token_is_revalidated(self):
        self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        with self.assertRaises(CleanupError):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                set(),
                preview_token=preview['token'],
                confirm=True,
            )
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Two - Author.epub')))
        self.assertFalse(os.path.exists(self.trash))

    def test_cleanup_requires_manifest_and_trash(self):
        with self.assertRaises(ManifestError):
            cleanup_preview(self.books, self.manifest, set(), self.trash)
        with self.assertRaises(CleanupError):
            cleanup_preview(self.books, self.manifest, set(), '')
        with self.assertRaises(SyncError):
            cleanup_preview(
                self.books,
                self.manifest,
                set(),
                os.path.join(self.books, 'trash'),
            )

    def test_restore_returns_book_sidecar_and_manifest_state(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        cleanup = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-1'},
            preview_token=preview['token'],
            confirm=True,
        )

        with self.assertRaises(RestoreError):
            restore_trash_run(
                self.trash,
                cleanup['run_id'],
                self.books,
                self.manifest,
                confirm=False,
            )
        self.assertFalse(os.path.exists(os.path.join(self.books, books[1][2])))

        result = restore_trash_run(
            self.trash,
            cleanup['run_id'],
            self.books,
            self.manifest,
            confirm=True,
        )

        self.assertEqual(result['restored'], [books[1][2]])
        self.assertEqual(result['conflicts'], [])
        self.assertTrue(os.path.isfile(os.path.join(self.books, books[1][2])))
        self.assertTrue(os.path.isdir(os.path.join(self.books, sidecar_name(books[1][2]))))
        with open(os.path.join(self.books, sidecar_name(books[1][2]), 'metadata.epub.lua'), 'rb') as stream:
            self.assertEqual(stream.read(), b'progress two')
        manifest = self.read_manifest()
        self.assertEqual(manifest['books']['uuid-2']['state'], 'active')
        self.assertNotIn('trashed_run', manifest['books']['uuid-2'])
        runs = list_trash_runs(self.trash)
        self.assertEqual(runs[0]['remaining'], 0)
        self.assertEqual(runs[0]['state'], 'restored')

    def test_restore_refuses_to_overwrite(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        cleanup = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-1'},
            preview_token=preview['token'],
            confirm=True,
        )
        self.write_book(books[1][2], b'user file')

        result = restore_trash_run(
            self.trash,
            cleanup['run_id'],
            self.books,
            self.manifest,
            confirm=True,
        )

        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(result['restored'], [])
        with open(os.path.join(self.books, books[1][2]), 'rb') as stream:
            self.assertEqual(stream.read(), b'user file')
        self.assertTrue(
            os.path.isfile(os.path.join(cleanup['run_dir'], 'books', books[1][2]))
        )
        self.assertEqual(self.read_manifest()['books']['uuid-2']['state'], 'trashed')

        os.remove(os.path.join(self.books, books[1][2]))
        result = restore_trash_run(
            self.trash,
            cleanup['run_id'],
            self.books,
            self.manifest,
            confirm=True,
        )
        self.assertEqual(result['restored'], [books[1][2]])
        self.assertEqual(self.read_manifest()['books']['uuid-2']['state'], 'active')

    def test_restore_requires_a_readable_manifest(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)
        cleanup = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-1'},
            preview_token=preview['token'],
            confirm=True,
        )
        os.remove(self.manifest)
        with self.assertRaises(ManifestError):
            restore_trash_run(
                self.trash,
                cleanup['run_id'],
                self.books,
                self.manifest,
                confirm=True,
            )
        self.assertFalse(os.path.exists(os.path.join(self.books, books[1][2])))
        self.assertTrue(
            os.path.isfile(os.path.join(cleanup['run_dir'], 'books', books[1][2]))
        )

    def test_cleanup_crash_can_be_rolled_back(self):
        books = self._two_books_with_sidecars()
        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)

        def crash(point, **_kwargs):
            if point == 'cleanup_after_book_move':
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            apply_cleanup(
                self.books,
                self.manifest,
                self.trash,
                {'uuid-1'},
                preview_token=preview['token'],
                confirm=True,
                fault=crash,
            )

        self.assertFalse(os.path.exists(os.path.join(self.books, books[1][2])))
        self.assertTrue(os.path.exists(journal_path(self.manifest)))
        report = recover_interrupted_run(self.manifest)
        self.assertEqual(report['action'], 'rolled_back')
        self.assertTrue(os.path.isfile(os.path.join(self.books, books[1][2])))
        self.assertTrue(os.path.isdir(os.path.join(self.books, sidecar_name(books[1][2]))))
        self.assertFalse(os.path.exists(journal_path(self.manifest)))
        self.assertEqual(self.read_manifest()['books']['uuid-2']['state'], 'active')
        run_dirs = [
            name for name in os.listdir(self.trash)
            if os.path.isdir(os.path.join(self.trash, name))
        ]
        self.assertEqual(len(run_dirs), 1)
        with open(os.path.join(self.trash, run_dirs[0], 'preview.json'), 'r', encoding='utf-8') as stream:
            stored = json.load(stream)
        self.assertEqual(stored['state'], 'rolled_back')
        runs = list_trash_runs(self.trash)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['remaining'], 0)


class SourceInvariantTests(unittest.TestCase):
    def _read(self, name):
        path = os.path.join(os.path.dirname(__file__), '..', 'src', name)
        with open(path, 'r', encoding='utf-8') as stream:
            return stream.read()

    def test_no_auto_sync_code_or_setting(self):
        ui_source = self._read('ui.py')
        config_source = self._read('config.py')
        self.assertNotIn('auto_sync', ui_source)
        self.assertNotIn('add_listener', ui_source)
        self.assertNotIn('SYNC_EVENTS', ui_source)
        self.assertNotIn("'auto_sync':", config_source)
        self.assertNotIn("'delete_orphans':", config_source)

    def test_plugin_identity_and_version(self):
        source = self._read('__init__.py')
        self.assertIn("name = 'AnchorSync'", source)
        self.assertIn('version = (0, 1, 0)', source)


if __name__ == '__main__':
    unittest.main()
