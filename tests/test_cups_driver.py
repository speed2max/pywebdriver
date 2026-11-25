# Copyright (C) 2026 Speed2Max
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Tests for /cups/printData against a simulated CUPS (Speed2Max #580).

The operator console sends its PDF sheets to /cups/printData. It must print
only on a queue that is ready, and answer an error the console can show
otherwise, instead of a job that waits forever or a bare 500.

Run from the repository root:

    python3 -I -m unittest discover -s tests -v
"""

import base64
import configparser
import importlib.util
import logging
import os
import sys
import types
import unittest

PLUGINS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "pywebdriver", "plugins"
)
CUPS_DRIVER_PATH = os.environ.get(
    "CUPS_DRIVER", os.path.join(PLUGINS_DIR, "cups_driver.py")
)
BASE_DRIVER_PATH = os.path.join(PLUGINS_DIR, "base_driver.py")

QUEUE = "s2m-a4"
BROTHER = 0x04F9
PDF = b"%PDF-1.4 result sheet"


class IPPError(Exception):
    pass


class FakeCups:
    """State of the simulated CUPS server, reset by each test."""

    down = False
    printers = {}
    jobs = []
    print_error = None


def ready_queue(**attrs):
    queue = {
        "printer-state": 3,  # idle
        "printer-state-reasons": ["none"],
        "printer-is-accepting-jobs": True,
        "device-uri": "usb://Brother/HL-L2350DW%20series?serial=X",
    }
    queue.update(attrs)
    return queue


class Connection:
    """Mimics cups.Connection (pycups 2.0)."""

    def __init__(self, *args, **kwargs):
        if FakeCups.down:
            raise RuntimeError("cupsd is not running")

    def getPrinters(self):
        return FakeCups.printers

    def printFile(self, printer, filename, title, options):
        if printer not in FakeCups.printers:
            raise IPPError(1030, "The printer or class does not exist.")
        if FakeCups.print_error:
            raise FakeCups.print_error
        with open(filename, "rb") as f:
            FakeCups.jobs.append((printer, f.read(), title))
        return 12


class FakeUsb:
    present = set()

    @staticmethod
    def find(idVendor=None, **kwargs):
        return object() if idVendor in FakeUsb.present else None


CONSOLE = "http://trakx.local"


def load_cups_driver(usb_vendor_id="04f9", cors_origins="*"):
    config = configparser.ConfigParser()
    config.read_string(
        "[flask]\ncors_origins = %s\n[cups_driver]\nusb_vendor_id = %s\n"
        % (cors_origins, usb_vendor_id)
    )

    class FakeApp:
        logger = logging.getLogger("test_cups_driver")
        routes = {}  # path -> accepted methods

        def route(self, path, methods=None, **k):
            self.routes[path] = methods

            return lambda f: f  # routes stay plain functions of the module

    pkg = types.ModuleType("pywebdriver")
    pkg.__path__ = []
    pkg.app, pkg.config, pkg.drivers = FakeApp(), config, {}
    plugins = types.ModuleType("pywebdriver.plugins")
    plugins.__path__ = []

    flask = types.ModuleType("flask")
    # jsonify(**kw) or jsonify(dict): the JSON body, as a dict.
    flask.jsonify = lambda *a, **kw: kw or a[0]
    flask.make_response = lambda body, status=200: (body, status)
    flask.request = None

    cups = types.ModuleType("cups")
    cups.Connection = Connection
    cups.IPPError = IPPError

    usb = types.ModuleType("usb")
    usb_core = types.ModuleType("usb.core")
    usb_core.find = FakeUsb.find
    usb.core = usb_core

    sys.modules.update(
        {
            "pywebdriver": pkg,
            "pywebdriver.plugins": plugins,
            "flask": flask,
            "cups": cups,
            "usb": usb,
            "usb.core": usb_core,
        }
    )
    load_module("pywebdriver.plugins.base_driver", BASE_DRIVER_PATH)
    return load_module("pywebdriver.plugins.cups_driver", CUPS_DRIVER_PATH)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CupsDriverTest(unittest.TestCase):
    def setUp(self):
        FakeCups.down = False
        FakeCups.printers = {QUEUE: ready_queue()}
        FakeCups.jobs = []
        FakeCups.print_error = None
        FakeUsb.present = {BROTHER}
        self.module = load_cups_driver()

    def print_data(
        self, data=None, printer=QUEUE, origin=CONSOLE, is_json=True, query=None
    ):
        """POST /cups/printData as the console does; returns (body, status)."""
        if data is None:
            data = base64.b64encode(PDF).decode()
        json = {"args": [printer, data, "Result"]} if query is None else {}
        self.module.request = types.SimpleNamespace(
            json=json if is_json else None,
            args=query or {},
            is_json=is_json,
            headers={"Origin": origin} if origin is not None else {},
        )
        answer = self.module.printersapi()
        return answer if isinstance(answer, tuple) else (answer, 200)

    def assertRefused(self, answer, status, code):
        body, got_status = answer
        self.assertEqual((got_status, body.get("cups_error")), (status, code))
        self.assertTrue(body.get("cups_error_description"))
        self.assertEqual(FakeCups.jobs, [])

    def test_ready_queue_prints(self):
        body, status = self.print_data()
        self.assertEqual((status, body["result"]), (200, 12))
        self.assertEqual(FakeCups.jobs, [(QUEUE, PDF, "Result")])

    def test_unknown_queue_is_refused(self):
        self.assertRefused(self.print_data(printer="other"), 503, "PrinterNotFound")

    def test_stopped_printer_is_refused(self):
        FakeCups.printers[QUEUE] = ready_queue(**{"printer-state": 5})
        self.assertRefused(self.print_data(), 503, "PrinterStopped")

    def test_not_accepting_is_refused(self):
        FakeCups.printers[QUEUE] = ready_queue(**{"printer-is-accepting-jobs": False})
        self.assertRefused(self.print_data(), 503, "NotAcceptingJobs")

    def test_unplugged_usb_printer_is_offline(self):
        # CUPS keeps an unplugged USB queue idle and accepting: the job would
        # wait forever. Only the USB bus tells.
        FakeUsb.present = set()
        self.assertRefused(self.print_data(), 503, "PrinterOffline")

    def test_offline_report_is_offline(self):
        FakeCups.printers[QUEUE] = ready_queue(
            **{"printer-state-reasons": ["offline-report"]}
        )
        self.assertRefused(self.print_data(), 503, "PrinterOffline")

    def test_no_vendor_configured_skips_usb_check(self):
        self.module = load_cups_driver(usb_vendor_id="")
        FakeUsb.present = set()
        body, status = self.print_data()
        self.assertEqual((status, body["result"]), (200, 12))

    def test_cups_down_is_unavailable(self):
        FakeCups.down = True
        self.assertRefused(self.print_data(), 503, "CupsUnavailable")

    def test_bad_base64_is_refused(self):
        self.assertRefused(self.print_data(data="%%%"), 400, "BadData")

    def test_unexpected_error_is_print_failed(self):
        FakeCups.print_error = RuntimeError("disk full")
        self.assertRefused(self.print_data(), 500, "PrintFailed")


class CupsOriginTest(CupsDriverTest):
    """Only the box's Odoo server may print (Speed2Max #580, no shared token)."""

    def setUp(self):
        super().setUp()
        self.module = load_cups_driver(cors_origins=CONSOLE)

    def test_get_is_refused(self):
        routes = self.module.app.routes
        self.assertEqual(routes["/cups/printData"], ["POST"])
        self.assertEqual(routes["/printers/printData"], ["POST"])

    def test_query_string_args_are_ignored(self):
        data = base64.b64encode(PDF).decode()
        self.print_data(query={"printer": QUEUE, "data": data})
        self.assertEqual(FakeCups.jobs, [])

    def test_non_json_body_is_refused(self):
        self.assertRefused(self.print_data(is_json=False), 415, "JsonRequired")

    def test_other_origin_is_refused(self):
        answer = self.print_data(origin="http://evil.example")
        self.assertRefused(answer, 403, "OriginRefused")

    def test_origin_must_match_exactly(self):
        for origin in ("http://trakx.local:8069", "http://trakx.local/"):
            with self.subTest(origin=origin):
                self.assertRefused(self.print_data(origin=origin), 403, "OriginRefused")

    def test_missing_origin_is_refused(self):
        for origin in (None, "null"):
            with self.subTest(origin=origin):
                self.assertRefused(self.print_data(origin=origin), 403, "OriginRefused")

    def test_allowed_origin_prints(self):
        body, status = self.print_data()
        self.assertEqual((status, body["result"]), (200, 12))

    def test_star_allows_any_origin(self):
        self.module = load_cups_driver(cors_origins="*")
        body, status = self.print_data(origin=None)
        self.assertEqual((status, body["result"]), (200, 12))


if __name__ == "__main__":
    unittest.main(verbosity=2)
