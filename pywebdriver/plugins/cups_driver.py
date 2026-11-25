# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# @author Sébastien BEAU <sebastien.beau@akretion.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import base64
import binascii
import inspect
import logging
import tempfile

import cups
from flask import jsonify, make_response, request

from pywebdriver import app, config, drivers

from .base_driver import AbstractDriver

try:
    import usb.core
except ImportError:  # pragma: no cover - pyusb is in requirements.txt
    usb = None

_logger = logging.getLogger(__name__)

# Speed2Max #580: CUPS printer-state value of a stopped printer.
PRINTER_STATE_STOPPED = 5


class ExtendedCups(cups.Connection):
    def printData(self, printer, data, title="Pywebdriver", options=None):
        with tempfile.NamedTemporaryFile() as f:
            f.write(base64.b64decode(data))
            f.flush()
            res = self.printFile(printer, f.name, title, options)
        return res

    def printFile(self, printer, filename, title="Pywebdriver", options=None):
        if options is None:
            options = {}
        string_options = {}
        for key, value in options.items():
            string_options[str(key)] = str(value)
        return super(ExtendedCups, self).printFile(
            printer, filename, title, string_options
        )

    def printFiles(self, printer, filenames, title="Pywebdriver", options=None):
        if options is None:
            options = {}
        string_options = {}
        for key, value in options.items():
            string_options[str(key)] = str(value)
        return super(ExtendedCups, self).printFiles(
            printer, filenames, title, string_options
        )


class CupsDriver(AbstractDriver):
    def getConnection(self):
        try:
            return ExtendedCups()
        except BaseException:
            return False

    def get_vendor_product(self):
        return "cups-icon"

    def get_status(self, **params):
        messages = []
        mapstate = {
            3: "Idle",
            4: "Printing",
            5: "Stopped",
        }
        conn = self.getConnection()
        if not conn:
            return {
                "status": "disconnected",
                "messages": ["Cups Sever is not running"],
            }
        for printer, value in self.getConnection().getPrinters().items():
            messages.append("{} : {}".format(printer, mapstate[value["printer-state"]]))
        state = {
            "status": "connected",
            "messages": messages,
        }
        return state


def usb_printer_present(vendor_id):
    """Whether a USB device of this vendor is plugged in (Speed2Max #580)."""
    return usb is not None and usb.core.find(idVendor=vendor_id) is not None


def configured_usb_vendor_id():
    """[cups_driver] usb_vendor_id as an int, or None when not set."""
    value = config.get("cups_driver", "usb_vendor_id", fallback="").strip()
    return int(value, 16) if value else None


def queue_problem(conn, printer):
    """Why this CUPS queue cannot print now, as (code, message), or None.

    Speed2Max #580: CUPS accepts a job on any existing queue and keeps it
    waiting, even when the USB printer is unplugged (the queue stays idle and
    accepting). The console must be told instead, so check the queue first.
    """
    queue = conn.getPrinters().get(printer)
    if queue is None:
        return "PrinterNotFound", "No CUPS queue named %s." % printer
    if queue.get("printer-state") == PRINTER_STATE_STOPPED:
        return "PrinterStopped", "The printer %s is stopped." % printer
    if not queue.get("printer-is-accepting-jobs", True):
        return "NotAcceptingJobs", "The printer %s does not accept jobs." % printer
    reasons = queue.get("printer-state-reasons") or []
    if any(reason.startswith("offline-report") for reason in reasons):
        return "PrinterOffline", "The printer %s is offline." % printer
    vendor_id = configured_usb_vendor_id()
    if (
        vendor_id is not None
        and queue.get("device-uri", "").startswith("usb://")
        and not usb_printer_present(vendor_id)
    ):
        return "PrinterOffline", "The USB printer %s is not plugged in." % printer
    return None


def cups_error(code, description, status):
    return make_response(
        jsonify(
            {
                "cups_error": code,
                "cups_error_status": code,
                "cups_error_description": description,
            }
        ),
        status,
    )


def origin_allowed(origin, allowed):
    """Whether a request from this Origin may print (Speed2Max #580).

    allowed is [flask] cors_origins: "*" (any origin, upstream's default) or a
    comma-separated list of exact origins, compared without normalisation.
    """
    entries = [entry.strip() for entry in allowed.split(",") if entry.strip()]
    if "*" in entries:
        return True
    return origin is not None and origin in entries


# Speed2Max #580: POST only. A GET (or a simple cross-site form POST) needs no
# CORS pre-check, so any web page could make the console print: the request
# must be JSON, from the box's Odoo server (no shared token: a local program
# can still print, which #580 accepts).
@app.route("/cups/printData", methods=["POST"])
@app.route("/printers/printData", methods=["POST"])
def printersapi():
    if not request.is_json:
        return cups_error("JsonRequired", "The request must be JSON.", 415)
    if not origin_allowed(
        request.headers.get("Origin"), config.get("flask", "cors_origins")
    ):
        return cups_error("OriginRefused", "This page may not print here.", 403)
    args = []
    kwargs = {}
    if request.json:
        args = request.json.get("args", [])
        kwargs = request.json.get("kwargs", {})
    # Speed2Max #580: check everything before CUPS sees a job, and answer an
    # error the caller can show (503 = printer not ready) rather than a job
    # that waits forever or a bare 500.
    try:
        call = inspect.signature(ExtendedCups.printData).bind(None, *args, **kwargs)
    except TypeError as e:
        return cups_error("BadData", str(e), 400)
    try:
        base64.b64decode(call.arguments["data"], validate=True)
    except (binascii.Error, TypeError, ValueError):
        return cups_error("BadData", "The data is not valid base64.", 400)
    conn = drivers["cups"].getConnection()
    if not conn:
        return cups_error("CupsUnavailable", "The CUPS server is not running.", 503)
    try:
        problem = queue_problem(conn, call.arguments["printer"])
        if problem:
            return cups_error(problem[0], problem[1], 503)
        result = conn.printData(*args, **kwargs)
    # TODO we should implement all cups error
    except cups.IPPError as e:
        err = str(e)
        return make_response(
            jsonify(
                {
                    "cups_error": "IPPError",
                    "cups_error_status": err,
                    "cups_error_description": err,
                }
            ),
            400,
        )
    except Exception as e:
        _logger.exception("Printing on %s failed", call.arguments["printer"])
        return cups_error("PrintFailed", str(e), 500)

    return jsonify(jsonrpc="2.0", result=result)


drivers["cups"] = CupsDriver()
