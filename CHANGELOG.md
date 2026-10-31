# Changelog

All notable changes to AnchorSync are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-31

First public release under the AnchorSync name.

### Added

- Manifest-locked book names keyed by Calibre UUID. A name is assigned once and
  then frozen.
- Manifest v2 with `library_id`, `generation`, `host`, `filename_locked`,
  `state`, source fingerprints, hash metadata, and backup generation.
- Migration from manifest v1 and rotation of manifest backups (`.bak.1` through
  `.bak.3`).
- Write-ahead journal with resume/rollback, so interrupted syncs and cleanups
  can be recovered safely.
- Separate **Cleanup removed books** action with preview, confirmation, a second
  confirmation above 10 candidates, and a trash-only result.
- **Restore from trash** for previous cleanup runs, with no-overwrite checks.
- **Preview sync (dry run)** that runs the full export and planning path without
  writing books, a manifest, or a journal.
- **Rebuild manifest (read-only)** to adopt existing files without moving,
  renaming, or deleting them.
- Syncthing conflict-copy classification, unmanaged-file reporting, and orphan
  sidecar reporting.
- Safety gates for an empty library, an empty export, a library drop of more
  than 10%, a missing/corrupt manifest with a non-empty book folder, lock
  contention, and unsafe paths.
- Target hash cache so unchanged runs avoid re-hashing the whole book folder.
- Reproducible ZIP packaging with `build_plugin.py`.
- A 53-test standard-library suite covering safety, cleanup, restore, manifests,
  journal recovery, performance, and the full lifecycle.
- GitHub Actions CI for tests and plugin packaging.
- MIT license.

### Changed

- Renamed the project to **AnchorSync**. The Calibre plugin name, import name
  (`anchorsync`), preference namespace, manifest and trash filenames, temporary
  file prefix, and action IDs all use the new name.
- A normal sync copies or updates content only. It does not rename, delete,
  move, or write a sidecar.
- New books use `{title} - {authors}` with a stable ` [<8-char-uuid>]` collision
  suffix. Page-count placeholders are rejected.
- EPUB exports keep deterministic metadata updates; PDFs are copied
  byte-for-byte.
- A book's filename no longer changes when its title, author, template, page
  count, or preferred format changes. Drift is reported instead.
- The manifest is written only after file operations succeed.

### Fixed

- Mass renames caused by mutable page counts in the filename template.
- Sidecar rewrites caused by automatic renames.
- Automatic cleanup treating conflict copies and unmanaged files as deletable.
- PDF churn caused by Calibre writing a fresh XMP `MetadataDate` on every PDF
  export.
- Inconsistent state after crashes during copies or cleanup moves.
- Unbounded hashing cost on large no-op syncs.

### Removed

- Auto-sync: listener, timer, setting, and UI are gone. Every sync is manual.
- Automatic rename and automatic cleanup during a normal sync.
- Legacy page-prefix parsing and page-prefix-based fuzzy matching.
- Support for restoring legacy KOReader Sync trash runs; their preview-less
  layout must be moved back manually.

### Migration notes

- AnchorSync uses its own settings and manifest namespace. It does not read the
  legacy KOReader Sync preferences or manifest.
- To adopt an existing book folder, configure AnchorSync and run **Rebuild
  manifest (read-only)**. It locks the current names without moving, renaming,
  or deleting anything.
- Existing names are frozen by the manifest. Changing the template after the
  rebuild only affects books entering the folder for the first time.
- Manifests and trash are local to the Calibre machine. Only the book folder
  should be transported.

## Historical notes

AnchorSync 0.1.0 is the first release under this name. It is based on the
completed safety-first KOReader Sync work. The one-time recovery and rename
completed on 2026-10-31:

- 1,095 books present: 1,049 EPUB + 46 PDF.
- 787 `.sdr` sidecars, all matched to present books.
- 0 renames and 0 sidecar writes during recovery.
- All book hashes matched the post-run manifest.
- Final recovery no-op: 0 copied, 0 renamed, 1,095 unchanged.
- 1,095 books and 787 sidecars were then renamed to `{title} - {authors}`.
- 1,051 sidecar metadata files were updated to the new `doc_path`.
- All names were verified against the exact Calibre exporter output.
- The one-time rename backup was removed after the install was verified.
