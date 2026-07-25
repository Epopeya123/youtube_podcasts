"""Deliberately broken app used ONLY to prove tests/lint_kivymd.py is not vacuous.

Every line below is wrong on purpose.  ``test_app_smoke.py`` asserts that the
linter reports each of KVMD001, KVMD002, KVMD003, KVMD004, PYJN001 and LAMB001
against this file.  It is never imported and never shipped.
"""

from kivy.lang import Builder
from kivymd.app import MDApp
from kivymd.uix.list import ThisListItemDoesNotExist  # KVMD004

KV = '''
MDScreen:

    MDBoxLayout:
        orientation: "vertical"

        MDButton:                       # KVMD001 - KivyMD 2.x only
            MDButtonText:               # KVMD001
                text: "go"

        TwoLineAvatarIconListItem:      # KVMD002 - broken in this build
            text: "nope"

        MDTotallyMadeUpWidget:          # KVMD003 - resolves nowhere
            text: "nope"
'''


class BadApp(MDApp):
    def build(self):
        return Builder.load_string(KV)

    def write_manifest(self, resolver, uri, payload):
        stream = resolver.openOutputStream(uri)
        stream.write(payload.encode("utf8"))  # PYJN001 - bytes, not bytearray
        stream.close()

    def build_rows(self, container, items):
        from kivymd.uix.list import OneLineListItem

        for item in items:
            row = OneLineListItem(text=item)
            # LAMB001 - `item` is captured by reference, every row gets the last one
            row.bind(on_release=lambda widget: self.pick(item))
            container.add_widget(row)

    def pick(self, item):
        return item
