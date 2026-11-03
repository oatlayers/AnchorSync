#!/usr/bin/env python

from calibre.customize import InterfaceActionBase


class AnchorSyncPlugin(InterfaceActionBase):
    name = 'AnchorSync'
    description = (
        'Export a Calibre library to a flat KOReader folder while preserving .sdr sidecars. '
        'Existing filenames are frozen; cleanup is separate, previewed, and trash-only.'
    )
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'OpenCode'
    version = (0, 1, 1)
    minimum_calibre_version = (6, 0, 0)
    actual_plugin = 'calibre_plugins.anchorsync.ui:InterfacePlugin'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.anchorsync.config import ConfigWidget

        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
