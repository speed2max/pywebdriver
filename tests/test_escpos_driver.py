# Copyright (C) 2026 Speed2Max
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Regression tests for Speed2Max #490 against a simulated USB printer.

Run from the repository root:

    python3 -I -m unittest discover -s tests -v

ESCPOS_DRIVER=<path to escpos_driver.py> runs them against another copy of
the driver, for instance the unpatched one, to see the failures they catch.
"""

import builtins
import configparser
import importlib.util
import logging
import os
import sys
import threading
import time
import types
import unittest

PLUGINS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "pywebdriver", "plugins"
)
DRIVER_PATH = os.environ.get(
    "ESCPOS_DRIVER", os.path.join(PLUGINS_DIR, "escpos_driver.py")
)
BASE_DRIVER_PATH = os.path.join(PLUGINS_DIR, "base_driver.py")

NO_DEVICE = "[Errno 19] No such device (it may have been disconnected)"


class USBError(Exception):
    pass


class FakeDevice:
    def __init__(self, printer):
        self.printer = printer
        self.alive = True
        self.busy = threading.Lock()

    def write(self, data):
        if FakeUsb.hang is not None:  # printer stalled: the write never returns
            FakeUsb.hang.wait()
        if FakeUsb.unplug_on_next_write:  # cable pulled during this write
            FakeUsb.unplug_on_next_write = False
            FakeUsb.plugged = False
            self.alive = False
        if not self.alive:
            raise USBError(NO_DEVICE)
        if not self.busy.acquire(blocking=False):
            self.printer.collisions += 1
            raise USBError("concurrent access to the USB handle")
        try:
            time.sleep(0.02)
            if not self.alive:
                raise USBError(NO_DEVICE)
            self.printer.written.append(data)
        finally:
            self.busy.release()


class FakeUsb:
    """Mimics python-escpos 3.0a8 escpos.printer.Usb."""

    plugged = True
    unplug_on_next_write = False
    hang = None  # a threading.Event: writes block until it is set

    def __init__(self, idVendor, idProduct, timeout=0, in_ep=0x82, out_ep=0x01, **kw):
        self.resets = 0
        self.collisions = 0
        self.written = []
        self.timeout = timeout
        self.open({"idVendor": idVendor, "idProduct": idProduct})

    def open(self, usb_args):
        if not FakeUsb.plugged:
            self.device = None
            raise USBError("Device not found or cable not plugged in.")
        self.device = FakeDevice(self)
        self.resets += 1  # Usb.open() calls device.reset()

    def close(self):
        if self.device:
            self.device.alive = False  # dispose_resources()
        self.device = None

    def _raw(self, msg):
        self.device.write(msg)

    def is_online(self):
        self._raw(b"\x10\x04\x01")
        time.sleep(0.05)
        return True

    def cut(self):
        self._raw(b"CUT")

    def cashdraw(self, pin):
        self._raw(b"DRAWER")


def load_driver():
    config = configparser.ConfigParser()
    config.read_string("[flask]\nport = 8069\n[escpos_driver]\n")

    class FakeApp:
        config = {}
        logger = logging.getLogger("test_escpos_driver")

        def route(self, *a, **k):
            return lambda f: f  # routes stay plain functions of the module

    pkg = types.ModuleType("pywebdriver")
    pkg.__path__ = []
    pkg.app, pkg.config, pkg.drivers = FakeApp(), config, {}
    plugins = types.ModuleType("pywebdriver.plugins")
    plugins.__path__ = []

    usb = types.ModuleType("usb")
    usb_core = types.ModuleType("usb.core")
    usb_core.find = lambda idVendor, idProduct: (
        object()
        if FakeUsb.plugged and (idVendor, idProduct) == (0x04B8, 0x0E15)
        else None
    )
    usb_core.USBError = USBError
    usb.core = usb_core

    flask = types.ModuleType("flask")
    flask.jsonify = lambda **kw: kw  # the JSON-RPC body, as a dict
    flask.render_template = flask.request = None
    netifaces = types.ModuleType("netifaces")
    netifaces.AF_INET, netifaces.ifaddresses, netifaces.interfaces = 2, None, None

    class Layout:
        def __init__(self, content):
            self.content = content

        def format(self, printer):
            printer._raw(self.content.encode())

    xmlescpos = types.ModuleType("xmlescpos")
    xmlescpos.Layout = Layout
    escpos = types.ModuleType("escpos")
    escpos_printer = types.ModuleType("escpos.printer")
    escpos_printer.Usb = FakeUsb

    sys.modules.update(
        {
            "pywebdriver": pkg,
            "pywebdriver.plugins": plugins,
            "usb": usb,
            "usb.core": usb_core,
            "flask": flask,
            "netifaces": netifaces,
            "xmlescpos": xmlescpos,
            "escpos": escpos,
            "escpos.printer": escpos_printer,
        }
    )
    # The real ThreadDriver: print jobs go through its queue and thread, as on
    # the till.
    load_module("pywebdriver.plugins.base_driver", BASE_DRIVER_PATH)
    builtins._ = lambda s: s
    return load_module("pywebdriver.plugins.escpos_driver", DRIVER_PATH)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def receipt_request(receipt="ticket"):
    """What the Odoo 19 POS posts to /hw_proxy/default_printer_action."""
    params = {"data": {"action": "print_receipt", "receipt": receipt}}
    return types.SimpleNamespace(json={"params": params})


# What /hw_proxy/default_printer_action must answer when the ticket did not
# print: the Odoo 19 POS (BasePrinter.printReceipt) then shows "Printing
# failed: The printer is not reachable." with a Retry button.
PRINT_FAILED = {
    "result": False,
    "errorCode": "PRINTER_NOT_REACHABLE",
    "canRetry": True,
}


class EscposDriverTest(unittest.TestCase):
    def setUp(self):
        FakeUsb.plugged = True
        FakeUsb.unplug_on_next_write = False
        FakeUsb.hang = None
        self.module = load_driver()
        self.driver = self.module.driver

    def tearDown(self):
        if FakeUsb.hang is not None:
            FakeUsb.hang.set()  # free the driver thread stuck in a write

    def post_receipt(self, receipt="ticket"):
        self.module.request = receipt_request(receipt)
        return self.module.default_printer_action()["result"]

    def printed(self, receipt):
        """Whether a ticket posted by the POS reached the printer."""
        return any(receipt.encode() in data for data in self.driver.written)

    def test_pos_receipt_printed_reports_success(self):
        self.assertIs(self.post_receipt("ticket ok"), True)
        self.assertTrue(self.printed("ticket ok"))

    def test_pos_receipt_printer_unplugged_reports_failure(self):
        self.driver.get_status()  # printer open, as after the POS's polls
        FakeUsb.unplug_on_next_write = True
        self.assertEqual(self.post_receipt(), PRINT_FAILED)
        # Still unplugged: the reopen fails too, and the POS is told so.
        self.assertEqual(self.post_receipt(), PRINT_FAILED)
        FakeUsb.plugged = True  # cable back, the POS's Retry prints
        self.assertIs(self.post_receipt("retry"), True)
        self.assertTrue(self.printed("retry"))

    def test_pos_receipt_printer_stalled_reports_failure_in_time(self):
        self.module.PRINT_RESULT_TIMEOUT = 0.3
        FakeUsb.hang = threading.Event()
        start = time.monotonic()
        self.assertEqual(self.post_receipt(), PRINT_FAILED)
        self.assertLess(time.monotonic() - start, 2)
        # A ticket queued behind the stalled one fails as well...
        self.assertEqual(self.post_receipt("queued"), PRINT_FAILED)
        FakeUsb.hang.set()  # printer unstuck
        FakeUsb.hang = None
        self.assertIs(self.post_receipt("next"), True)
        # ...and never comes out late: the POS has already reported it lost.
        self.assertFalse(self.printed("queued"))
        self.assertTrue(self.printed("next"))

    def test_status_polls_do_not_reset_the_printer(self):
        start = self.driver.resets
        for _ in range(10):
            self.assertEqual(self.driver.get_status()["status"], "connected")
        self.assertLessEqual(self.driver.resets - start, 1)

    def test_receipt_after_unplug_and_replug_prints(self):
        self.driver.receipt("before")
        FakeUsb.unplug_on_next_write = True
        with self.assertRaises(USBError):
            self.driver.receipt("cut mid-print")
        FakeUsb.plugged = True  # cable back
        self.driver.receipt("after replug")
        self.assertIn(b"after replug", self.driver.written)

    def test_status_poll_during_receipt_does_not_break_it(self):
        errors = []

        def poll():
            for _ in range(20):
                self.driver.get_status()

        def print_receipts():
            for i in range(20):
                try:
                    self.driver.receipt("ticket %d" % i)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=poll),
            threading.Thread(target=print_receipts),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.driver.collisions, 0)
        tickets = [w for w in self.driver.written if w.startswith(b"ticket")]
        self.assertEqual(len(tickets), 20)

    def test_status_of_unplugged_printer_is_offline_for_the_pos(self):
        # The Odoo 19 POS shows "Printer Offline" unless connected/connecting.
        self.assertEqual(self.driver.get_status()["status"], "connected")
        FakeUsb.plugged = False
        self.driver.device.alive = False  # cable pulled under the open handle
        for _ in range(3):  # the failing poll, then the polls that follow
            status = self.driver.get_status()["status"]
            self.assertNotIn(status, ("connected", "connecting"))
        FakeUsb.plugged = True
        self.assertEqual(self.driver.get_status()["status"], "connected")

    def test_cashbox_opens(self):
        self.driver.open_cashbox(None)
        self.assertIn(b"DRAWER", self.driver.written)


if __name__ == "__main__":
    unittest.main(verbosity=2)
