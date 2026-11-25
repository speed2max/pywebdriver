# Copyright (C) 2026 Speed2Max
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Tests for /hw_proxy/status_json as the Odoo 19 POS reads it (Speed2Max #490).

The POS navbar (proxy_status.js) reads the receipt printer as
drivers.printer and shows "Printer Offline" unless its status is
"connected" or "connecting".

Run from the repository root:

    python3 -I -m unittest discover -s tests -v
"""

import importlib.util
import os
import sys
import types
import unittest

ODOO8_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "pywebdriver",
    "plugins",
    "odoo8.py",
)


class FakeDriver:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def get_status(self, **params):
        self.calls += 1
        return {"status": self.status, "messages": []}


def load_odoo8(drivers):
    class FakeApp:
        def route(self, *a, **k):
            return lambda f: f  # routes stay plain functions of the module

    pkg = types.ModuleType("pywebdriver")
    pkg.__path__ = []
    pkg.app, pkg.drivers = FakeApp(), drivers
    flask = types.ModuleType("flask")
    flask.jsonify = lambda **kw: kw  # the JSON-RPC body, as a dict
    flask.make_response = lambda body: body
    flask.request = types.SimpleNamespace(json={"params": {}})
    sys.modules.update({"pywebdriver": pkg, "flask": flask})
    spec = importlib.util.spec_from_file_location("pywebdriver.plugins.odoo8", ODOO8_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pos_printer_offline(drivers):
    """The Odoo 19 POS navbar's test for its receipt printer."""
    return drivers.get("printer", {}).get("status") not in ("connected", "connecting")


class StatusJsonTest(unittest.TestCase):
    def status_json(self, **drivers):
        return load_odoo8(drivers).status_json()["result"]

    def test_pos_sees_connected_escpos_printer(self):
        statuses = self.status_json(escpos=FakeDriver("connected"))
        self.assertFalse(pos_printer_offline(statuses))

    def test_pos_sees_unplugged_escpos_printer_offline(self):
        statuses = self.status_json(escpos=FakeDriver("disconnected"))
        self.assertTrue(pos_printer_offline(statuses))
        self.assertEqual(statuses["printer"]["status"], "disconnected")

    def test_printer_status_asked_once(self):
        # A status asks the printer itself (about 1 s): never twice per poll.
        escpos = FakeDriver("connected")
        statuses = self.status_json(escpos=escpos)
        self.assertEqual(escpos.calls, 1)
        self.assertEqual(statuses["escpos"], statuses["printer"])

    def test_no_escpos_driver_no_printer_entry(self):
        statuses = self.status_json(scale=FakeDriver("connected"))
        self.assertNotIn("printer", statuses)


if __name__ == "__main__":
    unittest.main(verbosity=2)
