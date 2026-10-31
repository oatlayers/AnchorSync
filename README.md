# AnchorSync

AnchorSync is a Calibre plugin that exports EPUB and PDF books from a
Calibre library into one flat folder for KOReader. It is designed around one
rule: **an existing book must stay where it is, under the same name**.

A normal sync only adds new books or updates changed file content. It never
renames or deletes a book, and it never writes into a KOReader `.sdr`
reading-state folder. Removing a book from the folder is a separate, previewed,
trash-only **Cleanup** action.

Transport to the e-reader - Syncthing, USB, Dropbox, a network share, or
anything else - is managed by you. The plugin only maintains the folder.

```text
Calibre library (source of truth)
        |
        |  normal sync: copy or update only
        v
Flat book folder  --- your transport --->  e-reader
        |                                      |
        |  books + .sdr sidecars               |  KOReader owns .sdr
        +--------------------------------------+
```

> **Status:** 0.1.0, first public release. Sync is manual only.

## Contents

- [Quick start](#quick-start)
- [What it is and is not](#what-it-is-and-is-not)
- [Key ideas](#key-ideas)
- [Requirements](#requirements)
- [Install](#install)
- [First run](#first-run)
- [Everyday actions](#everyday-actions)
- [Settings](#settings)
- [What a normal sync does](#what-a-normal-sync-does)
- [Safety guarantees](#safety-guarantees)
- [Book names](#book-names)
- [Cleanup and restore](#cleanup-and-restore)
- [Manifest, journal, and backups](#manifest-journal-and-backups)
- [Preview and manifest rebuild](#preview-and-manifest-rebuild)
- [Data written to disk](#data-written-to-disk)
- [Troubleshooting](#troubleshooting)
- [Repository map](#repository-map)
- [Development](#development)
- [Design invariants](#design-invariants)
- [Out of scope](#out-of-scope)
- [License](#license)
- [Changelog](#changelog)

## Quick start

1. Build the plugin (or download the ZIP from a release):

   ```powershell
   python build_plugin.py
   ```

   The result is `dist/AnchorSync-0.1.0.zip`.

2. In Calibre, open **Preferences -> Plugins -> Load plugin from file** and
   select the ZIP. Restart Calibre.
3. Open **Preferences -> Plugins -> AnchorSync -> Customize plugin**.
4. Set the **Book folder** (the flat destination) and the **Deleted-book trash**
   folder. Keep the filename template as `{title} - {authors}`.
5. Click the **AnchorSync** toolbar button to run a sync. Use the button's
   arrow for preview, cleanup, restore, and manifest rebuild.

Run **Preview sync (dry run)** first if you want to see the plan without
changing anything.

## What it is and is not

**It is:**

- a one-way exporter from Calibre to a flat book folder;
- a rename-free, delete-free updater for books that are already in that folder;
- a separate previewed Cleanup/Restore tool for books removed from Calibre;
- a reporting tool for missing formats, name drift, conflict copies, unmanaged
  files, and orphan sidecars.

**It is not:**

- a two-way sync tool or a Calibre content server;
- a KOReader progress server or a Syncthing manager;
- an automatic/scheduled sync;
- a tool that edits the Calibre library or the KOReader configuration folder;
- a maintenance-rename tool: names are assigned once and then frozen.

## Key ideas

| Term | Meaning |
| --- | --- |
| **Calibre UUID** | The identity of a book. Filenames are not identity. |
| **Book folder** | One flat folder containing exported EPUB/PDF files. |
| **Manifest** | A local JSON index in Calibre's config directory. It locks each UUID's filename and stores hashes and state. It is not synced. |
| **`.sdr` sidecar** | A folder next to a book that KOReader uses for progress, bookmarks, and highlights. The plugin never writes inside it during a normal sync. |
| **Cleanup** | A separate action that moves books removed from Calibre, plus their sidecars, to trash. |
| **Trash** | A holding area outside the book folder. Cleanup never permanently deletes; Restore can move a run back. |

## Requirements

- Calibre 6.0 or newer.
- EPUB and/or PDF formats in the Calibre library.
- A book folder outside the Calibre library folder.
- A trash folder outside both the book folder and the Calibre library folder.
  Required for Cleanup and Restore.
- A transport of your choice. Syncthing is not required or configured.

## Install

### From a release ZIP

1. In Calibre: **Preferences -> Plugins -> Load plugin from file**.
2. Choose `AnchorSync-<version>.zip`.
3. Restart Calibre.
4. Open **Preferences -> Plugins -> AnchorSync -> Customize plugin**.
5. Set the book folder and trash folder.

### From source

```powershell
git clone <repository-url>
cd anchorsync
python build_plugin.py
```

Install `dist/AnchorSync-0.1.0.zip` using the steps above.

The plugin is a Calibre plugin, not a pip package. Installing it means loading
the ZIP through Calibre.

## First run

1. Configure the **Book folder**. It must be outside the Calibre library.
2. Configure the **Deleted-book trash**. It must be outside both the book
   folder and the Calibre library.
3. Run **Preview sync (dry run)** and review the report.
4. Run **Sync to KOReader**. The first run copies every EPUB/PDF book.
5. Review the completion report. It lists anything unusual and leaves it in
   place.

A first sync with an empty book folder creates a new manifest. A sync with a
missing manifest and a non-empty book folder aborts and asks you to rebuild or
restore the manifest instead.

## Everyday actions

The toolbar button runs **Sync to KOReader**. Its dropdown contains:

| Action | What it does |
| --- | --- |
| **Preview sync (dry run)** | Runs the full planning path but writes no books, manifest, or journal. Reports what would be copied. |
| **Cleanup removed books...** | Previews books that are in the manifest but no longer in Calibre, then moves approved books and their sidecars to trash. |
| **Restore from trash...** | Moves a previous cleanup run back when both destinations are free. |
| **Rebuild manifest (read-only)...** | Adopts existing folder files into a missing/corrupt manifest. Never moves, renames, or deletes a book. |

A normal sync is always started by hand. There is no timer, listener, database
hook, or auto-sync setting.

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| **Book folder** | empty | Flat export destination. Required. |
| **New-book filename template** | `{title} - {authors}` | Used only the first time a book enters the folder. Page-count placeholders are rejected. |
| **Deleted-book trash** | `<Calibre config>/anchorsync_trash` | Required for Cleanup and Restore. Must be separate from the book folder and library. |
| **Auto-sync** | removed | No listener, timer, or setting exists. |
| **Remove deleted books during sync** | removed | Cleanup is a separate, previewed action. |

Existing names are frozen by the manifest. Changing the template, a title, an
author, a page count, or the preferred format does not rename an existing book.

AnchorSync uses its own settings and manifest namespace. It does not import
settings or manifests from the legacy KOReader Sync plugin. To adopt an existing
book folder, configure AnchorSync and run **Rebuild manifest (read-only)**; it
locks the current names without moving, renaming, or deleting anything.

## What a normal sync does

1. Validate the book and trash folders.
2. Snapshot the Calibre library and export EPUB/PDF files to a temporary
   staging folder.
   - EPUB metadata updates are enabled.
   - PDFs are copied byte-for-byte, because Calibre writes a fresh XMP
     `MetadataDate` into every PDF export.
3. Match staged files to Calibre UUIDs.
4. For a UUID already in the manifest, reuse its **locked filename**, even if
   the current template would produce a different name. Report the drift.
5. For a new UUID, render the new-book template. On a collision, append
   ` [<8-char-uuid>]` before the extension.
6. Compare SHA-256 hashes and copy only when the target is missing or differs.
7. Never write, move, or delete a `.sdr` directory.
8. Write the manifest only after all file operations succeed.
9. Report missing formats, name drift, format changes, conflict copies,
   unmanaged books, orphan sidecars, removed-from-Calibre books, and files
   missing from the folder. Report-only items are left untouched.

A normal sync reports `Renamed: 0`, `Deleted: 0`, and `Sidecars written: 0`. A
no-op run changes no files and does not rewrite the manifest.

## Safety guarantees

A sync aborts before changing anything when:

- the Calibre library is empty;
- no EPUB or PDF file was staged;
- the current library count is more than 10% below the active manifest count;
- the manifest is missing or corrupt while the book folder contains books;
- another sync holds the single-writer lock;
- a path is unsafe (reserved Windows name, non-portable character, or an
  over-long new-book path).

The 10% library-drop guard is deliberate. If a large batch was intentionally
removed, run **Cleanup removed books** first. Cleanup marks approved removals as
`trashed`, lowering the active manifest count, and the next sync proceeds.

A lost manifest never authorizes deletion or a mass rename. Restore a manifest
backup or run **Rebuild manifest (read-only)**.

## Book names

New books use the configured template, normally `{title} - {authors}`. If two
books collide, append ` [<8-char-uuid>]` before the extension. The name is
assigned once and then frozen.

The plugin treats manifest filenames as opaque strings. It does not parse,
strip, or reconstruct old `[<digits>]` page-count prefixes, and it never
performs maintenance renames.

If a book's preferred format changes (for example EPUB to PDF), the current
filename is kept. The change is reported, and the old file is left in place.

## Cleanup and restore

Cleanup is a separate action, never part of a normal sync.

- **Candidates:** UUIDs in the manifest that are absent from the current Calibre
  library.
- **Preview:** lists each book, its format, and the sidecar that will move.
- **Confirmation:** required. More than 10 candidates require a second
  confirmation.
- **Move:** the book and its attached sidecar move together into a new trash
  run:

  ```text
  <trash>/<run-id>/
      preview.json
      books/<filename>
      sidecars/<stem>.sdr
  ```

- **Failure:** if a move fails, the operation stops and rolls back the moves it
  made. If rollback cannot complete, the partial state is reported and recorded
  in `preview.json`.
- **Manifest state:** approved books are marked `trashed` only after the moves
  and the manifest write succeed.
- **Never permanent:** nothing is deleted permanently, and the plugin never
  empties the trash.

**Restore** moves a selected trash run back only when both the book and sidecar
destinations are free. Nothing is overwritten. Blocked items stay in trash and
are reported. Restore marks the manifest entries `active` again.

Cleanup and Restore require a readable manifest. If it is missing or corrupt,
restore a backup or run **Rebuild manifest (read-only)** first.

Trash runs created by the legacy KOReader Sync 0.1.0 plugin used a different,
preview-less layout. They are not listed by AnchorSync's Restore action; move
those files back manually.

## Manifest, journal, and backups

The manifest lives in Calibre's config directory, is keyed by Calibre UUID, and
is local - it is not synced.

Manifest v2 records:

- `library_id`, `generation`, `backup_generation`, `host`, and `updated_at`;
- `filename` and `filename_locked`;
- `format`, `sha256`, `last_verified_hash`, `size`, `mtime_ns`, `ctime_ns`, and
  `inode` where available;
- `state` (`active` or `trashed`);
- `source_fingerprint` and trash-run metadata when applicable;
- `rebuild_incomplete` while a partial recovery rebuild is unresolved.

Backups rotate to `<manifest>.bak.1`, `.bak.2`, and `.bak.3`; `.bak.1` is the
previous generation. A v1 manifest migrates by copying every filename into
`filename_locked`, marking current books `active`, and never deleting a book.

A normal sync uses a write-ahead journal next to the manifest
(`<manifest>.journal` and `<manifest>.journal.d/`). Before replacing an existing
book, the journal keeps a backup. After a crash:

- if the manifest generation advanced, the run is treated as committed and the
  journal is discarded;
- otherwise, copies and cleanup moves are rolled back.

Re-running sync after recovery is safe and idempotent. Cleanup runs use the same
journal plus `preview.json` inside the trash run.

## Preview and manifest rebuild

- **Preview sync (dry run)** uses the same export and planning path but writes
  no books, no manifest, and no journal.
- **Rebuild manifest (read-only)** matches existing folder files against the
  current Calibre export names and adopts them. It never moves, renames, or
  deletes a book and preserves existing entries. A corrupt manifest is rotated
  to `.bak.1` before replacement.
- If a rebuild leaves existing files unmatched, the manifest is marked
  incomplete and normal sync is blocked until those files are resolved and the
  rebuild is run again.
- Syncthing names containing `.sync-conflict-` are classified as conflict
  copies, never as Calibre books.
- Files not in the manifest or the current export are reported as unmanaged.
- `.sdr` directories with no matching book are reported as orphan sidecars.

## Data written to disk

| Location | Contents |
| --- | --- |
| Calibre config directory | `anchorsync_manifest*.json`, `.bak.1`-`.bak.3`, `.journal`, `.journal.d/`, and the lock file |
| Book folder | Exported EPUB/PDF files only; the plugin never writes `.sdr` contents |
| Trash folder | One directory per cleanup run, containing `preview.json`, `books/`, and `sidecars/` |

The manifest is local to the Calibre machine. Only the book folder should be
transported.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| **Books disappeared from the folder** | Stop transport and KOReader writes. Check the trash folder and any backups. Do not click **Revert Local Changes** on the e-reader. |
| **Conflict files (`*.sync-conflict-*`) appear** | Treat them as conflict artifacts, not new books. Preserve and inspect them; the plugin only reports them. |
| **Reading progress is missing** | Check the e-reader sidecar, trash, and backups before overwriting anything. Do not bulk-copy one side's sidecars over the other. |
| **The manifest cannot be read** | Stop. Restore `.bak.1`, `.bak.2`, or `.bak.3`, or run **Rebuild manifest (read-only)**. Do not run Cleanup or Restore while it is missing or corrupt. |
| **A sync aborts because the library dropped more than 10%** | The guard stopped it before changes. If the removals were intentional, run **Cleanup removed books**, then sync again. |
| **"Another sync is already running"** | Wait for the other sync or close the other Calibre instance. Do not delete a lock file while Calibre is running. |
| **Folder validation errors** | The book folder must be outside the Calibre library; the trash must be outside both. Cleanup requires the book folder to exist; Restore creates it if needed. |
| **A book was removed and re-added** | If its UUID reappears, the next sync copies it back under its locked name. If it was cleaned up, use **Restore from trash**. |
| **PDFs are copied on every run** | This was a legacy KOReader Sync 0.1.0 bug. PDF export must use `update_metadata = False`, as the current UI does. |

For any unexpected file change: stop the transport, preserve the folder and
trash, and inspect the last report before retrying.

## Repository map

```text
.
|-- src/
|   |-- __init__.py                        Calibre plugin metadata and version
|   |-- config.py                          Preferences, migration, settings UI
|   |-- sync_core.py                       Manifest, journal, reconcile, cleanup, restore
|   |-- ui.py                              Toolbar actions, export, dialogs, reports
|   `-- plugin-import-name-anchorsync.txt
|-- tests/
|   |-- support.py                         Shared fixtures
|   |-- test_sync_core.py                  Safety, cleanup, restore, manifest, journal
|   `-- test_hardening.py                  Performance, fault injection, lifecycle
|-- build_plugin.py                        Builds the installable ZIP from src/
|-- dist/                                  Generated ZIP (ignored by Git)
|-- .github/workflows/ci.yml               Test and build on GitHub Actions
|-- CHANGELOG.md                           Release history
|-- LICENSE                                MIT license
`-- README.md
```

Files inside `src/` are packaged at the **root** of the plugin ZIP. Calibre
imports them as `calibre_plugins.anchorsync`, which is why the internal
imports use that name.

## Development

Run the test suite from the repository root:

```powershell
python -m unittest discover -s tests -v
```

Build the installable plugin:

```powershell
python build_plugin.py
```

The build reads the version from `src/__init__.py` and writes
`dist/AnchorSync-<version>.zip`.

The test suite has 53 tests and uses only the standard library plus a fake
Calibre record set. It covers:

- locked names, new-book naming, collision suffixes, template/page-count/title
  drift, Unicode aliases, and format changes;
- normal-sync no-op, no rename, no delete, and no sidecar writes;
- empty export, >10% drop, missing-manifest, lock-contention, and unsafe-path
  guards;
- cleanup preview scope, confirmation, large-cleanup confirmation, move and
  rollback, sidecar pairing, and token revalidation;
- restore success, destination conflicts, and manifest state repair;
- manifest v1 migration, backup rotation, dry run, conflict classification, and
  read-only rebuild;
- journal rollback/commit after simulated crashes, disk full, and permission
  denied;
- a 1,100-book no-op performance test that asserts the target hash cache is used;
- an end-to-end sync -> leave -> cleanup -> restore -> resync lifecycle.

The core tests do not need Calibre installed. Before release, load the built
plugin in a clean Calibre profile and run a sync against a temporary library.

## Design invariants

Contributors and coding agents should preserve these:

1. **UUID is identity.** Filenames are not identity.
2. **Names are locked.** A normal sync never renames an existing book.
3. **No normal-sync deletion.** Only Cleanup may trash a removed book.
4. **No sidecar writes.** The plugin never edits, moves, or deletes `.sdr`
   content during a normal sync.
5. **Trash first.** Cleanup never permanently deletes.
6. **The manifest is not the truth.** A lost or corrupt manifest cannot
   authorize deletion or a mass rename.
7. **Fail closed.** Empty exports, large library drops, errors, and lock
   contention abort before changes.
8. **One writer.** One Calibre instance and one plugin run at a time.
9. **Manual only.** There is no auto-sync listener, timer, setting, or UI.
10. **Opaque filenames.** Do not parse or reconstruct legacy page prefixes.

When changing behavior, update the tests and `CHANGELOG.md` together.

## Out of scope

- Syncthing configuration, pause/resume, and versioning.
- E-reader/PC sidecar merging.
- KOReader progress synchronization.
- Automatic or scheduled syncing.
- Maintenance renaming; existing names are frozen.
- Cleanup of unmanaged books or conflict copies; they are report-only.
- Permanent deletion or automatic trash emptying.
- Editing the Calibre library or the KOReader configuration folder.

## License

MIT. See [LICENSE](LICENSE).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
