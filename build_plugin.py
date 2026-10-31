#!/usr/bin/env python
"""Build the installable Calibre plugin ZIP from src/."""

import os
import re
import zipfile


ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(ROOT, 'src')
OUTPUT_DIR = os.path.join(ROOT, 'dist')
PLUGIN_NAME = 'AnchorSync'
FILES = (
    '__init__.py',
    'config.py',
    'sync_core.py',
    'ui.py',
    'plugin-import-name-anchorsync.txt',
)
VERSION_PATTERN = re.compile(
    r'^\s*version\s*=\s*\((\d+),\s*(\d+),\s*(\d+)\)\s*$',
    re.MULTILINE,
)


def plugin_version():
    """Read the version tuple from the plugin metadata."""
    path = os.path.join(SOURCE_DIR, '__init__.py')
    with open(path, 'r', encoding='utf-8') as stream:
        match = VERSION_PATTERN.search(stream.read())
    if match is None:
        raise RuntimeError('Cannot find the plugin version in %s' % path)
    return '.'.join(match.groups())


def build(output=None):
    output = output or os.path.join(
        OUTPUT_DIR, '%s-%s.zip' % (PLUGIN_NAME, plugin_version())
    )
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(os.path.join(SOURCE_DIR, name), name)
    return output


if __name__ == '__main__':
    print(build())
