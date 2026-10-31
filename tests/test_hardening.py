import os
import unittest

try:
    from support import BaseSyncTest, SimulatedCrash
except ImportError:
    from tests.support import BaseSyncTest, SimulatedCrash

from sync_core import (
    apply_cleanup,
    cleanup_preview,
    journal_path,
    reconcile,
    recover_interrupted_run,
    restore_trash_run,
    sidecar_name,
)


class HardeningTests(BaseSyncTest):
    def test_noop_performance_with_1100_books_uses_hash_cache(self):
        books = [
            (
                index,
                'uuid-%04d' % index,
                'Book %04d - Author.epub' % index,
                ('content-%04d' % index).encode(),
            )
            for index in range(1, 1101)
        ]
        first, stage, expected = self.sync_books(books)
        self.assertEqual(first['copied'], 1100)
        self.assertEqual(first['unchanged'], 0)
        manifest_before = self.read_bytes(self.manifest)
        generation_before = self.read_manifest()['generation']

        second = reconcile(self.books, stage, expected, self.manifest)

        self.assertEqual(second['copied'], 0)
        self.assertEqual(second['new_files'], 0)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(second['unchanged'], 1100)
        self.assertEqual(second['renamed'], 0)
        self.assertEqual(second['deleted'], 0)
        self.assertEqual(second['sidecars_written'], 0)
        self.assertEqual(second['stage_hashes'], 1100)
        self.assertEqual(second['hash_cache_hits'], 1100)
        self.assertEqual(second['hash_cache_misses'], 0)
        self.assertEqual(second['generation'], generation_before)
        self.assertEqual(self.read_bytes(self.manifest), manifest_before)

    def test_crash_after_temporary_copy_rolls_back(self):
        self.sync_books([(1, 'uuid-1', 'Book - Author.epub', b'old')])
        stage = self.new_stage()
        self.stage_file(1, 'Book - Author.epub', b'new', stage=stage)
        expected = self.expected_for([(1, 'uuid-1', 'Book - Author.epub', b'old')])

        def crash(point, **_kwargs):
            if point == 'after_copy_temp':
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            reconcile(self.books, stage, expected, self.manifest, fault=crash)

        self.assertTrue(os.path.exists(journal_path(self.manifest)))
        report = recover_interrupted_run(self.manifest)
        self.assertEqual(report['action'], 'rolled_back')
        with open(os.path.join(self.books, 'Book - Author.epub'), 'rb') as stream:
            self.assertEqual(stream.read(), b'old')
        self.assert_no_temp_files()

    def test_full_lifecycle_sync_leave_cleanup_restore_resync(self):
        books = [
            (index, 'uuid-%02d' % index, 'Book %02d - Author.epub' % index, ('book %d' % index).encode())
            for index in range(1, 11)
        ]
        first, _stage, _expected = self.sync_books(books)
        self.assertEqual(first['copied'], 10)
        removed = books[-1]
        sidecar, _metadata = self.add_sidecar(removed[2], b'progress = 99')

        second, _stage2, _expected2 = self.sync_books(books[:-1])
        self.assertEqual(second['deleted'], 0)
        self.assertEqual(second['renamed'], 0)
        self.assertTrue(os.path.isfile(os.path.join(self.books, removed[2])))
        self.assertTrue(os.path.isdir(sidecar))
        self.assertEqual(len(second['removed_from_calibre']), 1)

        preview = cleanup_preview(
            self.books,
            self.manifest,
            {'uuid-%02d' % index for index in range(1, 10)},
            self.trash,
        )
        self.assertEqual(preview['total'], 1)
        self.assertEqual(preview['items'][0]['uuid'], removed[1])
        cleanup = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-%02d' % index for index in range(1, 10)},
            preview_token=preview['token'],
            confirm=True,
        )
        self.assertFalse(os.path.exists(os.path.join(self.books, removed[2])))
        self.assertFalse(os.path.exists(sidecar))
        self.assertEqual(self.read_manifest()['books'][removed[1]]['state'], 'trashed')

        restored = restore_trash_run(
            self.trash,
            cleanup['run_id'],
            self.books,
            self.manifest,
            confirm=True,
        )
        self.assertEqual(restored['restored'], [removed[2]])
        self.assertTrue(os.path.isfile(os.path.join(self.books, removed[2])))
        self.assertTrue(os.path.isdir(sidecar))
        with open(os.path.join(sidecar, 'metadata.epub.lua'), 'rb') as stream:
            self.assertEqual(stream.read(), b'progress = 99')
        self.assertEqual(self.read_manifest()['books'][removed[1]]['state'], 'active')

        final, _stage3, _expected3 = self.sync_books(books)
        self.assertEqual(final['copied'], 0)
        self.assertEqual(final['unchanged'], 10)
        self.assertEqual(final['renamed'], 0)
        self.assertEqual(final['deleted'], 0)
        self.assertEqual(final['sidecars_written'], 0)

    def test_cleanup_never_includes_unmanaged_or_conflict_files(self):
        self.sync_books([
            (1, 'uuid-1', 'One - Author.epub', b'one'),
            (2, 'uuid-2', 'Two - Author.epub', b'two'),
        ])
        self.write_book('Unmanaged.epub', b'unmanaged')
        self.write_book('Conflict.sync-conflict-20260101-120000-AAAAAA.epub', b'conflict')
        os.makedirs(os.path.join(self.books, 'Orphan.sdr'))

        preview = cleanup_preview(self.books, self.manifest, {'uuid-1'}, self.trash)

        self.assertEqual([item['uuid'] for item in preview['items']], ['uuid-2'])
        result = apply_cleanup(
            self.books,
            self.manifest,
            self.trash,
            {'uuid-1'},
            preview_token=preview['token'],
            confirm=True,
        )
        self.assertEqual(result['moved'], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.books, 'Unmanaged.epub')))
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.books, 'Conflict.sync-conflict-20260101-120000-AAAAAA.epub')
            )
        )
        self.assertTrue(os.path.isdir(os.path.join(self.books, 'Orphan.sdr')))


if __name__ == '__main__':
    unittest.main()
