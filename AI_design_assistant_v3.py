#
#  BSD 3-Clause License
#
#  Copyright (c) 2026, Honda Research Institute Europe GmbH
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice, this
#     list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its
#     contributors may be used to endorse or promote products derived from
#     this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
"""FreeCAD AI Design Assistant."""

import base64
from html import escape
import io
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import FreeCAD
import FreeCADGui
import Mesh
import requests

try:
    from PySide6 import QtWidgets, QtGui, QtCore, QtNetwork
except ImportError:
    try:
        from PySide2 import QtWidgets, QtGui, QtCore, QtNetwork
    except ImportError:
        from PyQt5 import QtWidgets, QtGui, QtCore, QtNetwork

# --- CONFIGURATION ---
SHOW_MOODBOARD_ALIGNMENT_PLOT = False  # Applies only to the control group.
EVALUATION_REMINDER_INTERVAL_MINUTES = 5
EVALUATION_REMINDER_RESPONSE_MINUTES = 1
EVALUATION_REMINDER_RECENT_MINUTES = 2
# isometric, front, rear, left, right, top, bottom, current, or custom
AESTHETIC_VIEW = "isometric"
# Custom camera coordinates use document units (normally mm).
AESTHETIC_CAMERA_POSITION = (300.0, -300.0, 200.0)
AESTHETIC_CAMERA_DIRECTION = (-300.0, 300.0, -200.0)
AESTHETIC_CAMERA_UP = (0.0, 0.0, 1.0)
# True frames the assembly and may move the specified position.
# False preserves the custom position and uses this visible vertical span.
AESTHETIC_CUSTOM_FIT_ALL = False
AESTHETIC_CAMERA_HEIGHT = 300.0
TIMEOUT = float(os.getenv("HDA_TIMEOUT", "300"))
MESH_TIMEOUT = float(os.getenv("HDA_MESH_TIMEOUT", "1800"))
ASSET_TIMEOUT = float(os.getenv("HDA_ASSET_TIMEOUT", "15"))
SHOT_SIZE = (int(os.getenv("HDA_SHOT_SIZE", "1024")), int(os.getenv("HDA_SHOT_SIZE", "1024")))
THUMB_WIDTH = int(os.getenv("HDA_THUMB_WIDTH", "240"))
ZOOM_STEP = float(os.getenv("HDA_ZOOM_STEP", "1.25"))
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)"

_session = None


def _http():
    """One keep-alive session for the whole dock."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def server_message(resp) -> str:
    """Actionable text out of an error response (the fcserver sends JSON messages)."""
    try:
        return resp.json().get("message") or resp.text
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _json(resp):
    try:
        return resp.json()
    except Exception:
        return {}


class UploadWorker(QtCore.QThread):
    """POST /upload off the GUI thread: metrics + LLM answer + render + radar."""

    result_ready = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    status = QtCore.Signal(str)

    def __init__(self, url, stl_bytes, stl_name, fields, extra_files, endpoint="upload"):
        super().__init__()
        self.url, self.stl_bytes, self.stl_name = url, stl_bytes, stl_name
        self.fields, self.extra_files = fields, extra_files
        self.endpoint = endpoint

    def run(self):
        try:
            if self.stl_bytes:
                self.status.emit("Uploading mesh ...")
            elif self.fields.get("do_render") == "True" and not self.extra_files:
                self.status.emit("No mesh — asking for a text-to-image render ...")
            else:
                self.status.emit("Sending request ...")
            files = list(self.extra_files)
            if self.stl_bytes:
                files.append(("file", (self.stl_name or "model.stl", self.stl_bytes, "application/sla")))
            resp = _http().post(f"{self.url}/{self.endpoint}", files=files,
                                data=self.fields, timeout=max(TIMEOUT, 900) if self.endpoint == "aesthetic-evaluation" else TIMEOUT)
            if resp.status_code != 200:
                self.error.emit(server_message(resp))
                return
            self.status.emit("Done")
            self.result_ready.emit(_json(resp))
        except Exception as exc:
            self.error.emit(str(exc))


class StabilityWorker(QtCore.QThread):
    result_ready = QtCore.Signal(dict)
    error = QtCore.Signal(str)

    def __init__(self, url, stl, fields):
        super().__init__()
        self.url, self.stl, self.fields = url, stl, fields

    def run(self):
        try:
            response = requests.post(self.url + "/stability", data=self.fields, timeout=TIMEOUT,
                                     files={"file": ("assembly.stl", self.stl, "application/sla")})
            if not response.ok:
                raise ValueError(_json(response).get("detail") or server_message(response))
            result = response.json()
            angle = float(result["tilt_angle_deg"])
            if not result.get("evaluation_id"):
                raise ValueError(f"Tilt angle: {angle:.2f}°. The fcserver returned no logging confirmation. "
                                 "Update and restart fcserver, then repeat the evaluation.")
            log = result.get("event_log") or f"{self.fields['user_id']}/{self.fields['session_id']}/events.jsonl"
            result["tilt_angle_deg"] = angle
            result["event_log"] = log
            self.result_ready.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


class RatingWorker(QtCore.QThread):
    saved = QtCore.Signal()
    error = QtCore.Signal(str)

    def __init__(self, proposal, usefulness, surprise):
        super().__init__()
        self.proposal = proposal
        self.scores = {"usefulness": usefulness, "surprise": surprise}

    def run(self):
        try:
            url, prompt_id, user, session = self.proposal
            response = requests.post(f"{url}/proposals/{prompt_id}/rating",
                                     data=dict(self.scores, user_id=user, session_id=session), timeout=30)
            response.raise_for_status()
            self.saved.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class MeshWorker(QtCore.QThread):

    mesh_ready = QtCore.Signal(bytes, str)
    error = QtCore.Signal(str)
    status = QtCore.Signal(str)

    def __init__(self, url, image_b64, reference_stl_bytes, user_id, session_id,
                 steps_sampler, octree_resolution, alignment_mode, archive_url):
        super().__init__()
        self.url = url
        self.image_b64 = image_b64
        self.reference_stl_bytes = reference_stl_bytes
        self.user_id = user_id
        self.session_id = session_id
        self.steps_sampler = steps_sampler
        self.octree_resolution = octree_resolution
        self.alignment_mode = alignment_mode
        self.archive_url = archive_url

    def run(self):
        try:
            img_bytes = base64.b64decode(self.image_b64)

            # Upload image and reference STL together
            files = {
                "init_image": (
                    "input.png",
                    img_bytes,
                    "image/png",
                ),
                "reference_stl": (
                    "reference.stl",
                    self.reference_stl_bytes,
                    "application/octet-stream",
                ),
            }

            data = {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "mode": "img23d",
                "steps_sampler": self.steps_sampler,
                "octree_resolution": self.octree_resolution,
                "guidance": 7.5,
                "cfg_strength": 15.0,
                "cfg_strength_sampler": 3.0,
                "alignment_mode": self.alignment_mode,
            }

            # 1. Submit generation and alignment job
            self.status.emit("Uploading image and reference STL...")

            response = requests.post(
                f"{self.url}/generate",
                files=files,
                data=data,
                timeout=120,
            )
            response.raise_for_status()

            job_id = response.json()["job_id"]

            # 2. Poll until generation AND alignment finish
            deadline = time.monotonic() + MESH_TIMEOUT

            while time.monotonic() < deadline:
                time.sleep(2)

                res = requests.get(
                    f"{self.url}/result/{job_id}",
                    timeout=30,
                )
                res.raise_for_status()

                js = res.json()
                state = js.get("status")

                if state == "done":
                    mesh_b64 = js["images"][0]

                    mesh = base64.b64decode(mesh_b64)
                    archive = requests.post(f"{self.archive_url}/meshes/{job_id}",
                                            data={"user_id":self.user_id, "session_id":self.session_id,
                                                  "parameters":json.dumps(data), "mesh_server":self.url},
                                            files={"file":("generated.stl",mesh,"application/sla")}, timeout=TIMEOUT)
                    archive.raise_for_status()
                    self.mesh_ready.emit(mesh, job_id)
                    return

                elif state == "error":
                    raise RuntimeError(
                        js.get("error", "Unknown server error")
                    )

                elif state == "aligning":
                    self.status.emit("Aligning generated mesh...")

                else:
                    self.status.emit("Generating 3D mesh...")

            raise TimeoutError("Mesh generation timed out")

        except Exception as e:
            self.error.emit(str(e))

class AssetsWorker(QtCore.QThread):
    """Reference design / moodboard management on the fcserver."""

    result_ready = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    status = QtCore.Signal(str)

    def __init__(self, url, action, user_id, kind=None, files=None, replace=False, genai_server=None, session_id=None):
        super().__init__()
        self.url, self.action, self.user_id = url, action, user_id
        self.kind, self.files, self.replace = kind, files or [], replace
        self.genai_server = genai_server
        self.session_id = session_id

    def run(self):
        try:
            base = f"{self.url}/assets"
            if self.action == "list":
                resp = _http().get(base, params={"user_id": self.user_id, "session_id":self.session_id}, timeout=ASSET_TIMEOUT)
            elif self.action == "clear":
                self.status.emit("Clearing assets ...")
                data = {"user_id": self.user_id, "session_id":self.session_id}
                if self.kind:
                    data["kind"] = self.kind
                resp = _http().post(f"{base}/clear", data=data, timeout=ASSET_TIMEOUT)
            else:
                self.status.emit(f"Uploading {len(self.files)} image(s) + CLIP embeddings ...")
                data = {"user_id": self.user_id, "session_id":self.session_id, "kind": self.kind, "genai_server": self.genai_server,
                        "replace": "True" if self.replace else "False"}
                files = [("files", (name, io.BytesIO(blob), "image/png"))
                         for name, blob in self.files]
                resp = _http().post(base, data=data, files=files, timeout=TIMEOUT)
            js = _json(resp)
            if resp.status_code != 200 or js.get("status") != "success":
                self.error.emit(js.get("message") or server_message(resp))
                return
            self.status.emit("Done")
            self.result_ready.emit(js)
        except Exception as exc:
            self.error.emit(str(exc))


class Collapsible(QtWidgets.QGroupBox):
    """A dock section that folds away, so the panel only spends room on what
    the designer is using. A checkable QGroupBox only *disables* its children
    when unticked; hiding the body is what actually frees the space."""

    def __init__(self, title, expanded=True, parent=None):
        super().__init__(title, parent)
        self.setCheckable(True)
        self.setChecked(expanded)
        self.body = QtWidgets.QWidget(self)
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(6, 0, 6, 0)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.addWidget(self.body)
        self.body.setVisible(expanded)
        self.toggled.connect(self.body.setVisible)

    def add(self, thing):
        """Accept a widget or a layout (addWidget() rejects the latter)."""
        if hasattr(thing, "addWidget") and not isinstance(thing, QtWidgets.QWidget):
            self.body_layout.addLayout(thing)
        else:
            self.body_layout.addWidget(thing)
        return thing


class _ClickableLabel(QtWidgets.QLabel):
    """Thumbnail you can double-click to get the full-size view."""

    def __init__(self, on_double, parent=None):
        super().__init__(parent)
        self._on_double = on_double

    def mouseDoubleClickEvent(self, event):          # pragma: no cover - GUI only
        self._on_double()


class ImageCard(QtWidgets.QWidget):
    """One result image in the dock: title, thumbnail, Open / Save."""

    def __init__(self, name, b64_data, on_open, on_save, parent=None):
        super().__init__(parent)
        self.name, self.b64 = name, b64_data
        self.pixmap = QtGui.QPixmap()
        self.pixmap.loadFromData(base64.b64decode(b64_data))
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(2, 2, 2, 2)
        box.addWidget(QtWidgets.QLabel(f"<b>{('Edges' if name == 'top' else name.replace('_', ' ')).upper()}</b>"))
        self.thumb = _ClickableLabel(lambda: on_open(name))
        self.thumb.setPixmap(self.pixmap.scaledToWidth(
            THUMB_WIDTH, QtCore.Qt.SmoothTransformation))
        self.thumb.setToolTip("Double-click (or Open) for the full-size, zoomable view")
        box.addWidget(self.thumb)
        buttons = QtWidgets.QHBoxLayout()
        self.open_button = QtWidgets.QPushButton("Open")
        self.open_button.clicked.connect(lambda checked, n=name: on_open(n))
        self.save_button = QtWidgets.QPushButton("Save…")
        self.save_button.setToolTip("Write this image to a file")
        self.save_button.clicked.connect(lambda checked, n=name, d=b64_data: on_save(n, d))
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.save_button)
        box.addLayout(buttons)


class ResultViewer(QtWidgets.QWidget):
    """Separate, resizable, zoomable window for the results.

    The dock is narrow by nature and a moodboard radar with several labelled
    axes cannot be read at 240 px. This window is a tab per image with zoom
    controls; it is created once and refilled on every request.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HDA results")
        self.pages, self.order = {}, []
        self.zoom, self.fit = 1.0, False
        root = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.currentChanged.connect(self.rezoom)
        root.addWidget(self.tabs, 1)
        bar = QtWidgets.QHBoxLayout()
        for text, slot, tip in (("-", self.zoom_out, "zoom out"),
                                ("1:1", self.zoom_actual, "original size"),
                                ("fit", self.zoom_fit, "fit the window"),
                                ("+", self.zoom_in, "zoom in")):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda checked, s=slot: s())
            bar.addWidget(button)
        self.zoom_label = QtWidgets.QLabel("100 %")
        bar.addWidget(self.zoom_label)
        bar.addStretch(1)
        self.btn_save = QtWidgets.QPushButton("Save current image…")
        self.btn_save.clicked.connect(lambda checked: self.save_current())
        bar.addWidget(self.btn_save)
        root.addLayout(bar)
        self.setMinimumWidth(520)

    def show_images(self, images):
        """Refill with {name: base64}; keeps the tab order of the server."""
        for name, b64 in (images or {}).items():
            pixmap = QtGui.QPixmap()
            pixmap.loadFromData(base64.b64decode(b64))
            if name in self.pages:
                self.pages[name].update(pixmap, b64)
                continue
            page = QtWidgets.QScrollArea()
            page.setWidgetResizable(True)
            label = QtWidgets.QLabel()
            label.setAlignment(QtCore.Qt.AlignCenter)
            page.setWidget(label)
            self.pages[name] = _Tab(pixmap, b64, label, page)
            self.order.append(name)
            self.tabs.addTab(page, ("Edges" if name == "top" else name.replace("_", " ").title()))
        if self.order:
            self.tabs.setCurrentIndex(len(self.order) - 1)
        self.rezoom()

    def current_tab(self):
        index = self.tabs.currentIndex()
        if not self.order or index < 0 or index >= len(self.order):
            return None
        return self.pages[self.order[index]]

    def rezoom(self, *_):
        tab = self.current_tab()
        if tab is None:
            return
        if self.fit:
            target = max(120, self.width() - 60)
        else:
            target = max(1, int(tab.pixmap.width() * self.zoom))
        tab.label.setPixmap(tab.pixmap.scaledToWidth(target, QtCore.Qt.SmoothTransformation))
        self.zoom_label.setText("fit" if self.fit else f"{int(self.zoom * 100)} %")

    def zoom_in(self):
        self.fit, self.zoom = False, min(8.0, self.zoom * ZOOM_STEP)
        self.rezoom()

    def zoom_out(self):
        self.fit, self.zoom = False, max(0.05, self.zoom / ZOOM_STEP)
        self.rezoom()

    def zoom_actual(self):
        self.fit, self.zoom = False, 1.0
        self.rezoom()

    def zoom_fit(self):
        self.fit = True
        self.rezoom()

    def save_current(self):
        tab = self.current_tab()
        index = self.tabs.currentIndex()
        if tab is not None and 0 <= index < len(self.order):
            save_image_dialog(self, self.order[index], tab.b64)


class _Tab:
    """One results-window tab: the original pixmap, its base64, label, page."""

    def __init__(self, pixmap, b64, label, page):
        self.pixmap, self.b64, self.label, self.page = pixmap, b64, label, page

    def update(self, pixmap, b64):
        self.pixmap, self.b64 = pixmap, b64


def save_image_dialog(parent, name, b64_data):
    """QFileDialog wrapper; returns the written path ('' when cancelled)."""
    path, _selected = QtWidgets.QFileDialog.getSaveFileName(
        parent, "Save image", f"{name.replace(' ', '_')}.png", IMAGE_FILTER)
    if not path:
        return ""
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(b64_data))
    return path


class EditObserver:
    def __init__(self, dock):
        self.dock = dock

    def edit(self, action, obj, prop=None):
        if not self.dock.study_identity or obj.Document != FreeCAD.ActiveDocument or self.dock.importing:
            return
        data = {"action":action, "document":obj.Document.Name, "object":obj.Name, "type":obj.TypeId}
        if prop:
            data["property"] = prop
            value = getattr(obj, prop, None)
            if isinstance(value, (str, int, float, bool, FreeCAD.Vector, FreeCAD.Units.Quantity)) or prop == "Placement":
                data["value"] = str(value)[:1024]
        self.dock.record_event("cad_edit", data)

    def slotCreatedObject(self, obj):
        self.edit("create", obj)

    def slotDeletedObject(self, obj):
        self.edit("delete", obj)

    def slotChangedObject(self, obj, prop):
        self.edit("change", obj, prop)

    def transaction(self, action, doc):
        if self.dock.study_identity and doc == FreeCAD.ActiveDocument and not self.dock.importing:
            self.dock.record_event("cad_edit", {"action":action, "document":doc.Name})

    def slotOpenTransaction(self, doc, label):
        if self.dock.study_identity and doc == FreeCAD.ActiveDocument and not self.dock.importing:
            self.dock.record_event("cad_edit", {"action":"begin_transaction", "document":doc.Name, "label":label})

    def slotUndoDocument(self, doc):
        self.transaction("undo", doc)

    def slotRedoDocument(self, doc):
        self.transaction("redo", doc)


class RemoteAnalysisDock(QtWidgets.QDockWidget):
    def __init__(self):
        super().__init__("AI Design Assistant")
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.main_widget = QtWidgets.QWidget()
        self.layout = QtWidgets.QVBoxLayout(self.main_widget)

        self.workers = []
        self.checking_servers = False
        self.session_revision = 0
        self.generating = False
        self.render_requested = False
        self.aesthetic_assets_identity = None
        self.aesthetic_render_data = None
        self.aesthetic_evaluation_locked = False
        self.last_stability_result = None
        self.proposal = None
        self.rating_required = False
        self.settings = QtCore.QSettings("FreeCAD", "RemoteShapeAnalyzer")
        self.init_image_bytes = None
        self.init_image_name = ""
        self.last_images = {}
        self.image_cards = {}
        self.viewer = None

        self.study_identity = None
        self.importing = False
        self.setObjectName("RemoteAnalysisStudyDock")
        self._build_ui()
        # Manual CAD edit logging is disabled because the document observer can crash FreeCAD.
        self.observer = None
        # FreeCAD.addDocumentObserver(EditObserver(self))
        self.outbox = Path(FreeCAD.getUserAppDataDir())/"RemoteAnalysisStudyEvents"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.event_reply = None
        self.retry_urls = {}
        self.event_timer = QtCore.QTimer(self)
        self.event_timer.timeout.connect(self.send_next_event)
        self.event_timer.start(500)
        self.evaluation_pending = None
        self.evaluation_last_clicked = {}
        self.evaluation_popup = None
        self.evaluation_timer = QtCore.QTimer(self)
        self.evaluation_timer.setSingleShot(True)
        self.evaluation_timer.timeout.connect(self.show_evaluation_reminder)
        self.btn_aesthetic.clicked.connect(lambda checked=False: self.record_evaluation_click("aesthetics"))
        self.btn_stability.clicked.connect(lambda checked=False: self.record_evaluation_click("stability"))
        self.refresh_assets()

    # ------------------------------------------------------------------- build
    def _build_ui(self):
        self.activity_label = QtWidgets.QLabel()
        self.update_activity()
        self.layout.addWidget(self.activity_label)
        self.layout.addWidget(self._action_bar())

        self.layout.addWidget(self._controls_scroll(), 1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        self.layout.addWidget(self.status_label)
        self.setWidget(self.main_widget)

    def _action_bar(self):
        bar_widget = QtWidgets.QWidget()
        bar = QtWidgets.QHBoxLayout(bar_widget)
        bar.setContentsMargins(0, 0, 0, 0)
        self.btn_results = QtWidgets.QPushButton("Open results window")
        self.btn_results.setToolTip("Render, edge map and radar in a resizable, zoomable window")
        self.btn_results.clicked.connect(lambda checked: self.open_results())
        bar.addWidget(self.btn_results, 1)
        return bar_widget

    def _controls_scroll(self):
        """Every input, in foldable sections, inside a vertical scroll area."""
        form = QtWidgets.QWidget()
        form_layout = QtWidgets.QVBoxLayout(form)

        servers = Collapsible("Server and Session Settings")
        self.network_manager = QtNetwork.QNetworkAccessManager(self)
        self.server_status = {}
        self.servers = {}
        for name, default_port in [("Image Server", 8001), ("Mesh Server", 8000)]:
            row = QtWidgets.QHBoxLayout()
            host = QtWidgets.QLineEdit("127.0.0.1")
            host.setPlaceholderText("IP address")
            port = QtWidgets.QSpinBox()
            port.setRange(1, 65535)
            port.setValue(default_port)
            self.servers[name] = (host, port)
            row.addWidget(QtWidgets.QLabel(name))
            row.addWidget(host, 1)
            row.addWidget(QtWidgets.QLabel("Port:"))
            row.addWidget(port)
            self.server_status[name] = QtWidgets.QLabel()
            row.addWidget(self.server_status[name])
            self.set_server_status(name, "Not checked", "gray")
            servers.add(row)
            host.textChanged.connect(lambda _, n=name: self.set_server_status(n, "Not checked", "gray"))
            port.valueChanged.connect(lambda _, n=name: self.set_server_status(n, "Not checked", "gray"))
            if name == "Image Server":
                host.editingFinished.connect(self.refresh_assets)
                port.editingFinished.connect(self.refresh_assets)
        fields = QtWidgets.QFormLayout()
        self.user_id_input = QtWidgets.QLineEdit(self.settings.value("user_id", ""))
        self.user_id_input.setPlaceholderText("Enter your unique ID")
        self.user_id_input.editingFinished.connect(self.refresh_assets)
        self.session_id_input = QtWidgets.QLineEdit(self.settings.value("session_id", "session_001"))
        self.user_id_input.textChanged.connect(lambda text: self.settings.setValue("user_id", text.strip()))
        self.session_id_input.textChanged.connect(lambda text: self.settings.setValue("session_id", text.strip()))
        self.user_id_input.textChanged.connect(self.session_changed)
        self.session_id_input.textChanged.connect(self.session_changed)
        self.session_id_input.editingFinished.connect(self.refresh_assets)
        fields.addRow("User ID:", self.user_id_input)
        fields.addRow("Session ID:", self.session_id_input)
        servers.add(fields)
        group_row = QtWidgets.QHBoxLayout()
        self.ai_assisted_group = QtWidgets.QRadioButton("AI-assisted group")
        self.control_group = QtWidgets.QRadioButton("Control group")
        self.study_group_buttons = QtWidgets.QButtonGroup(self)
        for button in (self.ai_assisted_group, self.control_group):
            self.study_group_buttons.addButton(button)
            group_row.addWidget(button)
        self.ai_assisted_group.setChecked(True)
        servers.add(group_row)
        experiment_buttons = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect / Start experiment")
        self.btn_connect.clicked.connect(self.connect_servers)
        experiment_buttons.addWidget(self.btn_connect, 1)
        self.btn_stop_experiment = QtWidgets.QPushButton("Stop experiment")
        self.btn_stop_experiment.clicked.connect(self.stop_experiment)
        experiment_buttons.addWidget(self.btn_stop_experiment, 1)
        servers.add(experiment_buttons)
        form_layout.addWidget(servers)

        generation = Collapsible("Image Generation")
        self.target_desc_input = QtWidgets.QTextEdit()
        self.target_desc_input.setPlaceholderText(
            "What are you trying to create?"
        )
        self.target_desc_input.setMaximumHeight(60)
        generation.add(self.target_desc_input)

        bottom_layout = QtWidgets.QHBoxLayout()

        self.guid_cont, self.guid_slider, self.guid_is_float = self.create_slider(
            "Guidance:", 0, 100, 90, is_float=True
        )
        bottom_layout.addWidget(self.guid_cont, stretch=1)

        self.sketch_path = None
        generation.add(bottom_layout)
        self.init_row = self._file_row("Initial image:", "Choose alternative initial image...",
                                       self.pick_initial_image, self.clear_initial_image)
        self.btn_use_proposal = QtWidgets.QPushButton("Use last proposal")
        self.btn_use_proposal.setToolTip("Use the latest generated image in Results as the initial image.")
        self.btn_use_proposal.setEnabled(False)
        self.btn_use_proposal.clicked.connect(self.use_last_proposal)
        self.init_row.layout().insertWidget(self.init_row.layout().indexOf(self.init_row.pick_button), self.btn_use_proposal)
        generation.add(self.init_row)
        self.sketch_row = self._file_row("Edge reference:", "Choose edge reference...",
                                         self.on_upload_sketch_clicked, self.clear_sketch)
        generation.add(self.sketch_row)
        self.btn_render = self._primary_button("Generate")
        self.btn_render.clicked.connect(lambda checked: self.execute_request(True, False))
        generation.add(self.btn_render)
        form_layout.addWidget(generation)

        expert = Collapsible("AI Expert", expanded=False)
        self.question_input = QtWidgets.QLineEdit()
        self.question_input.setPlaceholderText("What would you like to ask about the design?")
        expert.add(self.question_input)
        self.btn_ask = QtWidgets.QPushButton("Ask the AI Expert")
        self.btn_ask.clicked.connect(lambda checked: self.execute_request(False, True))
        expert.add(self.btn_ask)
        self.response_area = QtWidgets.QTextEdit()
        self.response_area.setReadOnly(True)
        self.response_area.setMaximumHeight(96)
        expert.add(self.response_area)
        form_layout.addWidget(expert)

        images = Collapsible("Moodboard", expanded=False)
        self.ref_row = self._file_row(
            "Reference design (radar):", "Choose reference design...",
            lambda: self.pick_assets("reference", single=True),
            lambda: self.clear_assets("reference"))
        images.add(self.ref_row)
        self.mood_row = self._file_row(
            "Moodboard images (radar):", "Choose moodboard images...",
            lambda: self.pick_assets("moodboard", single=False),
            lambda: self.clear_assets("moodboard"))
        images.add(self.mood_row)
        self.assets_label = QtWidgets.QLabel("On the server: -")
        self.assets_label.setWordWrap(True)
        images.add(self.assets_label)
        form_layout.addWidget(images)

        settings = Collapsible("3D Generation Settings", expanded=False)
        steps, self.mesh_steps_slider, _ = self.create_slider("Iterations:", 10, 50, 20)
        octree, self.octree_slider, _ = self.create_slider("Resolution:", 64, 256, 128)
        settings.add(steps)
        settings.add(octree)
        alignment_row = QtWidgets.QFormLayout()
        self.alignment_mode_combo = QtWidgets.QComboBox()
        self.alignment_mode_combo.addItems(["isotropic", "anisotropic"])
        self.alignment_mode_combo.setCurrentText("anisotropic")
        alignment_row.addRow("Alignment mode:", self.alignment_mode_combo)
        settings.add(alignment_row)
        form_layout.addWidget(settings)

        transforms = Collapsible("STL Transformations", expanded=False)
        direction = QtWidgets.QFormLayout()
        self.scale_axis = QtWidgets.QComboBox()
        self.scale_axis.addItems(["Isotropic (all axes)", "Local X", "Local Y", "Local Z"])
        direction.addRow("Scale direction:", self.scale_axis)
        transforms.add(direction)
        scale, self.scale_slider, _ = self.create_slider("Scale factor:", 10, 300, 100, is_float=True)
        transforms.add(scale)
        apply_scale = QtWidgets.QPushButton("Scale selected mesh")
        apply_scale.clicked.connect(self.scale_selected_mesh)
        transforms.add(apply_scale)
        form_layout.addWidget(transforms)
        form_layout.addWidget(self._results_pane())
        aesthetic = Collapsible("Aesthetic Evaluation")
        self.aesthetic_section = aesthetic
        self.aesthetic_prompt = QtWidgets.QTextEdit()
        self.aesthetic_prompt.setPlaceholderText("How should your design look? Describe the materials, colors, surface finishes and lighting.")
        self.aesthetic_prompt.setMaximumHeight(80)
        aesthetic.add(QtWidgets.QLabel("Rendering prompt:"))
        aesthetic.add(self.aesthetic_prompt)
        self.btn_aesthetic_render = QtWidgets.QPushButton("Render visible design")
        self.btn_aesthetic_render.clicked.connect(lambda checked=False: self.evaluate_aesthetics(render_only=True))
        aesthetic.add(self.btn_aesthetic_render)
        self.aesthetic_render_layout = QtWidgets.QVBoxLayout()
        aesthetic.add(self.aesthetic_render_layout)
        self.btn_aesthetic = QtWidgets.QPushButton("Evaluate visible design")
        self.btn_aesthetic.setEnabled(False)
        self.btn_aesthetic.clicked.connect(self.evaluate_aesthetics)
        aesthetic.add(self.btn_aesthetic)
        self.aesthetic_note = QtWidgets.QLabel()
        self.aesthetic_note.setWordWrap(True)
        aesthetic.add(self.aesthetic_note)
        self.aesthetic_plot_layout = QtWidgets.QVBoxLayout()
        aesthetic.add(self.aesthetic_plot_layout)
        form_layout.addWidget(aesthetic)
        stability = Collapsible("Stability Evaluation")
        self.stability_section = stability
        self.btn_stability = QtWidgets.QPushButton("Compute tilt angle")
        self.btn_stability.clicked.connect(self.evaluate_stability)
        stability.add(self.btn_stability)
        self.stability_result = QtWidgets.QLabel("Tilt angle: —")
        self.stability_result.setWordWrap(True)
        stability.add(self.stability_result)
        form_layout.addWidget(stability)
        self.ai_sections = (generation, expert, images, settings, transforms, self.results_section)

        form_layout.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setWidget(form)
        scroll.setMinimumHeight(120)
        self.controls_scroll = scroll
        return scroll

    def _primary_button(self, text):
        button = QtWidgets.QPushButton(text)
        button.setMinimumHeight(32)
        font = button.font()
        font.setBold(True)
        button.setFont(font)
        return button

    def _results_pane(self):
        pane = Collapsible("Results")
        self.results_section = pane
        self.btn_mesh = self._primary_button("Generate 3D Object")
        self.btn_mesh.setEnabled(False)
        self.btn_mesh.setToolTip("Generate an isometric image, then select a reference object for alignment.")
        self.btn_mesh.clicked.connect(self.request_mesh_generation)
        pane.add(self.btn_mesh)
        self.rating_box = QtWidgets.QGroupBox("Rate this proposal (1 = low, 5 = high)")
        rating_form = QtWidgets.QFormLayout(self.rating_box)
        self.usefulness = QtWidgets.QComboBox()
        self.surprise = QtWidgets.QComboBox()
        for label, combo in (("Usefulness", self.usefulness), ("Surprise", self.surprise)):
            combo.addItems(["Choose…", "1", "2", "3", "4", "5"])
            rating_form.addRow(label, combo)
        save = QtWidgets.QPushButton("Submit rating")
        save.clicked.connect(self.submit_rating)
        rating_form.addRow(save)
        self.rating_box.hide()
        pane.add(self.rating_box)
        self.metrics_label = QtWidgets.QLabel("Metrics: N/A")
        self.metrics_label.setWordWrap(True)
        pane.add(self.metrics_label)
        self.image_count_label = QtWidgets.QLabel("")
        pane.add(self.image_count_label)

        self.image_scroll = QtWidgets.QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.image_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.image_scroll.setMinimumHeight(180)
        self.image_scroll.setMaximumHeight(320)
        self.image_container = QtWidgets.QWidget()
        self.image_layout = QtWidgets.QVBoxLayout(self.image_container)
        self.image_layout.setAlignment(QtCore.Qt.AlignTop)
        self.image_layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
        self.image_layout.setContentsMargins(2, 2, 2, 2)
        self.image_scroll.setWidget(self.image_container)
        self.image_scroll.hide()
        pane.add(self.image_scroll)
        return pane

    def _file_row(self, label_text, button_text, on_pick, on_clear):
        """label | value | [choose] [clear] — one row of the Images group."""
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel(label_text))
        value = QtWidgets.QLabel("-")
        value.setWordWrap(True)
        value.setTextFormat(QtCore.Qt.PlainText)
        value.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(value, 1)
        btn_pick = QtWidgets.QPushButton("Choose...")
        btn_pick.setToolTip(button_text)
        btn_pick.clicked.connect(lambda checked: on_pick())
        layout.addWidget(btn_pick)
        btn_clear = QtWidgets.QPushButton("Clear")
        btn_clear.clicked.connect(lambda checked: on_clear())
        layout.addWidget(btn_clear)
        row.value_label = value
        row.pick_button = btn_pick
        return row

    def create_slider(self, label_text, min_val, max_val, default_val,
                      is_float=False, decimal=2):
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QtWidgets.QLabel(label_text)
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setMinimum(min_val)
        slider.setMaximum(max_val)
        slider.setValue(default_val)
        value_label = QtWidgets.QLabel(str(default_val))

        def update_label():
            val = slider.value()
            if is_float:
                actual_val = round(val / 100.0, decimal)
                value_label.setText(str(actual_val))
            else:
                value_label.setText(str(val))

        update_label()
        slider.valueChanged.connect(update_label)
        layout.addWidget(label)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        return container, slider, is_float

    # ------------------------------------------------------------------ helpers
    def server_url(self, name):
        host, port = self.servers[name]
        address = host.text().strip() or "127.0.0.1"
        if ":" in address and not address.startswith("["):
            address = f"[{address}]"
        return f"http://{address}:{port.value()}"

    def fc_url(self):
        return self.server_url("Image Server")

    def set_server_status(self, name, text, color, detail=""):
        label = self.server_status[name]
        label.setText(f"● {text}")
        label.setStyleSheet(f"color: {color}; font-weight: bold;")
        label.setToolTip(detail)

    def identity(self):
        return self.fc_url(), self.user_id_input.text().strip(), self.session_id_input.text().strip()

    @staticmethod
    def document_bytes():
        doc = FreeCAD.ActiveDocument
        if not doc:
            raise ValueError("Open a FreeCAD document first.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"design.FCStd"
            doc.saveCopy(str(path))
            return path.read_bytes()

    def record_event(self, kind, data, proposal=None):
        if proposal:
            url, prompt, user, session = proposal
        else:
            url, user, session = self.study_identity
            prompt = None
        eid = str(uuid4())
        payload = dict(event_type=kind, data=data, event_id=eid, user_id=user, session_id=session,
                       prompt_id=prompt, client_timestamp=datetime.now(timezone.utc).isoformat())
        path = self.outbox/f"{time.time_ns()}_{eid}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps([url, payload]), encoding="utf-8")
        os.replace(tmp,path)

    def send_next_event(self):
        if self.event_reply is not None:
            return
        for path in sorted(self.outbox.glob("*.json")):
            try:
                url, record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if self.retry_urls.get(url, 0) <= time.monotonic():
                break
        else:
            return
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(url+"/study/events"))
        request.setRawHeader(b"Content-Type", b"application/json")
        reply = self.network_manager.post(request, json.dumps(record).encode())
        self.event_reply = reply
        QtCore.QTimer.singleShot(30000, lambda: reply.abort() if self.event_reply is reply else None)
        def finished():
            if reply.error() == QtNetwork.QNetworkReply.NoError:
                path.unlink(missing_ok=True)
                self.event_timer.start(100)
            else:
                self.status_label.setText("Study event delivery pending: "+reply.errorString())
                self.retry_urls[url] = time.monotonic()+5
                self.event_timer.start(500)
            self.event_reply = None
            reply.deleteLater()
        reply.finished.connect(finished)

    def session_changed(self, _):
        self.evaluation_last_clicked.clear()
        self.last_stability_result = None
        self.stop_evaluation_reminders()
        self.clear_aesthetic_render()
        self.aesthetic_assets_identity = None
        self.update_aesthetic_button()
        self.clear_aesthetic_plot()
        self.ref_row.value_label.setText("-")
        self.mood_row.value_label.setText("-")
        self.assets_label.setText("On the server: -")
        self.study_identity = None
        self.session_revision += 1
        for name in self.servers:
            self.set_server_status(name, "Disconnected", "#c23b36", "User or session changed. Click Connect.")

    def valid_session(self):
        for field, name in ((self.user_id_input, "User ID"), (self.session_id_input, "Session ID")):
            if not field.text().strip():
                QtWidgets.QMessageBox.warning(self, "Session settings", f"Please enter a {name}.")
                field.setFocus()
                return False
        return True

    def stop_evaluation_reminders(self):
        self.evaluation_timer.stop()
        self.evaluation_pending = None
        if self.evaluation_popup is not None:
            self.evaluation_popup.close()

    def start_evaluation_reminders(self):
        self.stop_evaluation_reminders()
        self.evaluation_timer.start(max(1, int(EVALUATION_REMINDER_INTERVAL_MINUTES * 60000)))

    def show_evaluation_reminder(self):
        recent_seconds = EVALUATION_REMINDER_RECENT_MINUTES * 60
        now = time.monotonic()
        if all(now - self.evaluation_last_clicked.get(name, float("-inf")) <= recent_seconds
               for name in ("aesthetics", "stability")):
            self.start_evaluation_reminders()
            return
        if self.evaluation_pending is None:
            self.evaluation_pending = {"aesthetics", "stability"}
        self.aesthetic_section.setChecked(True)
        self.stability_section.setChecked(True)
        if self.evaluation_popup is None:
            self.evaluation_popup = QtWidgets.QMessageBox(self)
            self.evaluation_popup.setWindowTitle("Design evaluation reminder")
            self.evaluation_popup.setIcon(QtWidgets.QMessageBox.Information)
            self.evaluation_popup.setStandardButtons(QtWidgets.QMessageBox.Ok)
            self.evaluation_popup.setModal(False)
        remaining = []
        if "aesthetics" in self.evaluation_pending:
            remaining.append('evaluate aesthetics ("Evaluate visible design")')
        if "stability" in self.evaluation_pending:
            remaining.append('compute stability ("Compute tilt angle")')
        minutes = EVALUATION_REMINDER_RESPONSE_MINUTES
        unit = "minute" if minutes == 1 else "minutes"
        self.evaluation_popup.setText(
            f'Please {" and ".join(remaining)} within {minutes:g} {unit}.\n\n'
            "Use the evaluation buttons in the macro. This reminder will repeat until both buttons have been clicked.")
        self.evaluation_popup.show()
        self.evaluation_popup.raise_()
        self.evaluation_popup.activateWindow()
        self.evaluation_timer.start(max(1, int(minutes * 60000)))

    def record_evaluation_click(self, evaluation):
        self.evaluation_last_clicked[evaluation] = time.monotonic()
        if self.evaluation_pending is None:
            return
        self.evaluation_pending.discard(evaluation)
        if not self.evaluation_pending:
            self.start_evaluation_reminders()

    def stop_experiment(self, checked=False):
        answer = QtWidgets.QMessageBox.question(
            self,
            "Stop experiment",
            "Are you sure you want to stop the experiment?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.stop_evaluation_reminders()
        url, user, session = self.identity()
        if user and session:
            self.record_event("experiment_stopped", {}, (url, None, user, session))

    def connect_servers(self):
        for section in self.ai_sections:
            section.setVisible(self.ai_assisted_group.isChecked())
        if not self.valid_session():
            return
        url, user, session = self.identity()
        self.record_event("experiment_started", {}, (url, None, user, session))
        self.start_evaluation_reminders()
        self.btn_connect.setEnabled(False)
        self.checking_servers = True
        self.ping_revision = self.session_revision
        self.update_activity()
        self.ping_results = {}
        for name, (host, port) in self.servers.items():
            self.set_server_status(name, "Checking…", "#b36b00")
            host.setEnabled(False)
            port.setEnabled(False)
            url = QtCore.QUrl(self.server_url(name) + "/ping")
            reply = self.network_manager.get(QtNetwork.QNetworkRequest(url))
            timer = QtCore.QTimer(reply)
            timer.setSingleShot(True)
            timer.timeout.connect(reply.abort)
            reply.finished.connect(lambda n=name, r=reply, t=timer: self.on_ping_finished(n, r, t))
            timer.start(5000)

    def on_ping_finished(self, name, reply, timer):
        timed_out = not timer.isActive()
        timer.stop()
        code = reply.attribute(QtNetwork.QNetworkRequest.HttpStatusCodeAttribute)
        if timed_out:
            result = "timed out"
        elif code == 200 and reply.error() == QtNetwork.QNetworkReply.NoError:
            result = "Connected"
        elif code:
            result = f"HTTP {code}"
        else:
            result = reply.errorString()
        reply.deleteLater()
        self.ping_results[name] = result
        if self.ping_revision == self.session_revision:
            if result == "Connected":
                if name == "Image Server":
                    self.study_identity = self.identity()
                self.set_server_status(name, result, "#288343")
            else:
                self.set_server_status(name, "Disconnected", "#c23b36", result)
        if len(self.ping_results) == len(self.servers):
            for host, port in self.servers.values():
                host.setEnabled(True)
                port.setEnabled(True)
            self.btn_connect.setEnabled(True)
            self.checking_servers = False
            self.update_activity()
            self.refresh_assets()

    def _show_init_image(self):
        self.init_row.value_label.setText(
            self.init_image_name or "-")

    def clear_images(self):
        for i in reversed(range(self.image_layout.count())):
            widget = self.image_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        self.image_cards = {}

    def add_image(self, name, b64_data):
        """One thumbnail in the (horizontally scrollable) result strip."""
        card = ImageCard(name, b64_data, self.show_image, self.save_image)
        self.image_cards[name] = card
        self.image_layout.addWidget(card)

    def show_image(self, name):
        """Open the zoomable window on one image (double-click / Open)."""
        b64 = self.last_images.get(name)
        if not b64:
            self._status(f"no image named {name!r} to show")
            return
        self.open_results({name: b64})

    def save_image(self, name, b64_data):
        path = save_image_dialog(self, name, b64_data)
        if path:
            self._status(f"saved {path}")

    def open_results(self, images=None):
        """The results window: bigger than the dock, zoomable, one tab per image."""
        payload = images if images is not None else self.last_images
        if not payload:
            self._status("nothing to show yet — generate a render first")
            return
        if self.viewer is None:
            self.viewer = ResultViewer()
        self.viewer.show_images(payload)
        self.viewer.show()
        self.viewer.raise_()

    def open_log_page(self):
        """The fcserver's HTML report of everything the models said and made."""
        url = self.fc_url() + "/viewer?" + urlencode({"user_id": self.user_id_input.text().strip(),
                                                     "session_id": self.session_id_input.text().strip()})
        try:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        except Exception as exc:                 # no desktop services (e.g. AppImage headless)
            self._status(f"open this in a browser: {url} ({exc})")
            return
        self._status(f"opened the conversation report: {url}")

    def _set_buttons_enabled(self, enabled):
        self.generating = not enabled
        self.btn_render.setEnabled(enabled and not self.rating_required)
        self.btn_ask.setEnabled(enabled and not self.rating_required)
        self.btn_render.setText("Generate" if enabled else "Working...")
        self.btn_ask.setText("Ask the AI Expert" if enabled else "Working...")
        self.sketch_row.setEnabled(enabled)
        self.init_row.setEnabled(enabled)
        self.btn_use_proposal.setEnabled(enabled and bool(self.last_images.get("isometric")))
        self.btn_mesh.setEnabled(enabled and not self.rating_required and bool(self.last_images.get("isometric")))
        self.update_activity()

    def update_activity(self):
        busy = self.workers or self.generating or self.checking_servers
        self.activity_label.setText("Status: Busy" if busy else "Status: Idle")
        color = "#b36b00" if busy else "#288343"
        self.activity_label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _status(self, text):
        self.status_label.setText(text)
        FreeCAD.Console.PrintMessage(f"[{datetime.now(timezone.utc).isoformat()}] [HDA] {text}\n")
        FreeCADGui.updateGui()

    def _register(self, worker):
        self.workers.append(worker)
        self.update_activity()
        worker.finished.connect(self._drop_worker)
        return worker

    def _drop_worker(self):
        worker = self.sender()
        if worker in self.workers:
            self.workers.remove(worker)
        self.update_activity()
        self.update_aesthetic_button()

    @staticmethod
    def _read(path):
        with open(path, "rb") as fh:
            return fh.read()

    # ------------------------------------------------------------------ camera
    def extract_camera(self):
        camera_data = None

        def ensure_vector(obj):
            if obj is None:
                return [0, 0, 0]
            if hasattr(obj, "Base"):
                obj = obj.Base
            try:
                return list(obj)
            except TypeError:
                return [obj.x, obj.y, obj.z]

        try:
            gui_doc = FreeCADGui.ActiveDocument
            if gui_doc and gui_doc.ActiveView:
                view = gui_doc.ActiveView
                raw_pos, raw_dir, raw_up = (view.viewPosition(), view.getViewDirection(),
                                            view.getUpDirection())
                pos, direction, up = ensure_vector(raw_pos), ensure_vector(raw_dir), ensure_vector(raw_up)
                focal_point = [p + d for p, d in zip(pos, direction)]
                camera_data = {"position": pos, "focal_point": focal_point, "view_up": up}
        except Exception as e:
            FreeCAD.Console.PrintMessage(f"[{datetime.now(timezone.utc).isoformat()}] Camera extraction failed: {str(e)}\n")
        return camera_data

    def grab_viewport(self, fit_view=None):
        """Capture the active view, optionally framing the visible design for CLIP."""
        gui_doc = getattr(FreeCADGui, "ActiveDocument", None)
        view = getattr(gui_doc, "ActiveView", None)
        if view is None:
            return None
        fd, path = tempfile.mkstemp(prefix="hda_shot_", suffix=".png")
        os.close(fd)
        try:
            if fit_view is not None:
                directions = {"isometric":"viewIsometric", "front":"viewFront", "rear":"viewRear",
                              "left":"viewLeft", "right":"viewRight", "top":"viewTop", "bottom":"viewBottom"}
                if fit_view not in ("current", "custom") and fit_view not in directions:
                    raise ValueError("Unknown AESTHETIC_VIEW: "+str(fit_view))
                camera = view.getCamera()
                projection = view.getCameraType()
                animation = view.isAnimationEnabled()
                try:
                    view.setAnimationEnabled(False)
                    view.setCameraType("Orthographic")
                    if fit_view == "custom":
                        from pivy import coin  # Load Coin bindings for getCameraNode().
                        values = (*AESTHETIC_CAMERA_POSITION, *AESTHETIC_CAMERA_DIRECTION,
                                  *AESTHETIC_CAMERA_UP, AESTHETIC_CAMERA_HEIGHT)
                        if not all(math.isfinite(v) for v in values) or AESTHETIC_CAMERA_HEIGHT <= 0:
                            raise ValueError("Camera values must be finite and height must be positive")
                        direction = FreeCAD.Vector(*AESTHETIC_CAMERA_DIRECTION)
                        up = FreeCAD.Vector(*AESTHETIC_CAMERA_UP)
                        if direction.Length == 0 or up.Length == 0:
                            raise ValueError("Camera direction and up must be nonzero")
                        direction.normalize()
                        up.normalize()
                        right = direction.cross(up)
                        if right.Length < 1e-8:
                            raise ValueError("Camera direction and up must not be parallel")
                        rotation = FreeCAD.Rotation(right, up, -direction, "ZYX")
                        view.setCameraOrientation(rotation.Q)
                        node = view.getCameraNode()
                        node.position.setValue(*AESTHETIC_CAMERA_POSITION)
                        node.height.setValue(AESTHETIC_CAMERA_HEIGHT)
                    elif fit_view != "current":
                        getattr(view, directions[fit_view])()
                    if fit_view != "custom" or AESTHETIC_CUSTOM_FIT_ALL:
                        view.fitAll()
                    view.redraw()
                    # Fit and render at the same aspect ratio. A square export
                    # directly from a wide fitted view can clip outer components.
                    width, height = view.getSize()
                    scale = min(SHOT_SIZE[0] / width, SHOT_SIZE[1] / height)
                    size = (max(1, round(width * scale)), max(1, round(height * scale)))
                    view.saveImage(path, *size, "White")
                finally:
                    try:
                        view.setCameraType(projection)
                        view.setCamera(camera)
                        view.redraw()
                    finally:
                        view.setAnimationEnabled(animation)
                if size != SHOT_SIZE:
                    frame = QtGui.QImage(path)
                    if frame.isNull():
                        raise ValueError("FreeCAD produced no viewport image")
                    canvas = QtGui.QImage(*SHOT_SIZE, QtGui.QImage.Format_RGB32)
                    canvas.fill(QtGui.QColor("white"))
                    painter = QtGui.QPainter(canvas)
                    try:
                        painter.drawImage((canvas.width()-frame.width())//2,
                                          (canvas.height()-frame.height())//2, frame)
                    finally:
                        painter.end()
                    if not canvas.save(path, "PNG"):
                        raise OSError("Could not save the fitted viewport PNG")
            else:
                view.saveImage(path, SHOT_SIZE[0], SHOT_SIZE[1], "White")
            return self._read(path)
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"[{datetime.now(timezone.utc).isoformat()}] [HDA] viewport capture failed: {e}\n")
            return None
        finally:
            if os.path.exists(path):
                os.remove(path)

    # ------------------------------------------------------------- image picker
    def on_upload_sketch_clicked(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Edge Reference",
            "",
            "Image Files (*.png *.jpg *.jpeg *.bmp *.webp)"
        )

        if file_path:
            self.sketch_path = file_path
            self.sketch_row.value_label.setText(os.path.basename(file_path))
            self.sketch_row.value_label.setToolTip(file_path)

    def clear_sketch(self):
        self.sketch_path = None
        self.sketch_row.value_label.setText("-")
        self.sketch_row.value_label.setToolTip("")

    def pick_initial_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Alternative initial image for the render", "", IMAGE_FILTER)
        if not path:
            return
        try:
            self.init_image_bytes = self._read(path)
            self.init_image_name = os.path.basename(path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Image Error", str(e))
            return
        self._show_init_image()
        self._status(f"Initial image for the render: {self.init_image_name}")

    def use_last_proposal(self):
        encoded = self.last_images.get("isometric")
        if not encoded:
            return
        try:
            image = base64.b64decode(encoded, validate=True)
            if not image:
                raise ValueError("The last proposal image is empty.")
        except (ValueError, TypeError) as exc:
            QtWidgets.QMessageBox.critical(self, "Image Error", str(exc))
            return
        self.init_image_bytes = image
        self.init_image_name = "last_proposal.png"
        self._show_init_image()
        self._status("Initial image set to the last proposal from Results.")

    def clear_initial_image(self):
        self.init_image_bytes = None
        self.init_image_name = ""
        self._show_init_image()

    def pick_assets(self, kind, single):
        if not self.valid_session():
            return
        if single:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Reference design", "", IMAGE_FILTER)
            paths = [path] if path else []
        else:
            paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
                self, "Moodboard images", "", IMAGE_FILTER)
        if not paths:
            return
        try:
            files = [(os.path.basename(p), self._read(p)) for p in paths]
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Image Error", str(e))
            return
        worker = self._register(AssetsWorker(
            self.fc_url(), "upload", self.user_id_input.text().strip(), kind=kind,
            files=files, replace=single, genai_server=self.server_url("Mesh Server"),
            session_id=self.session_id_input.text().strip() or None))
        worker.status.connect(self._status)
        worker.error.connect(self._on_assets_error)
        worker.result_ready.connect(self._on_assets_done)
        worker.start()

    def clear_assets(self, kind=None):
        if not self.valid_session():
            return
        worker = self._register(AssetsWorker(
            self.fc_url(), "clear", self.user_id_input.text().strip(), kind=kind,
            session_id=self.session_id_input.text().strip()))
        worker.status.connect(self._status)
        worker.error.connect(self._on_assets_error)
        worker.result_ready.connect(self._on_assets_done)
        worker.start()

    def refresh_assets(self):
        user_id = self.user_id_input.text().strip()
        self.ref_row.value_label.setText("-")
        self.mood_row.value_label.setText("-")
        session_id = self.session_id_input.text().strip()
        self.update_aesthetic_button()
        if not user_id or not session_id:
            self.assets_label.setText("On the server: -")
            return
        worker = self._register(AssetsWorker(self.fc_url(), "list", user_id, session_id=session_id))
        worker.error.connect(self._on_assets_error)
        worker.result_ready.connect(self._on_assets_done)
        worker.start()

    def _on_assets_done(self, js):
        worker = self.sender()
        if isinstance(worker, AssetsWorker) and (worker.url,worker.user_id,worker.session_id) != self.identity():
            return
        assets = js.get("assets") or {}
        self.aesthetic_assets_identity = self.identity() if any(assets.get(k) for k in ("reference", "moodboard")) else None
        self.update_aesthetic_button()
        ref = [a.get("file") or a["label"] for a in assets.get("reference", [])]
        mood = [a.get("file") or a["label"] for a in assets.get("moodboard", [])]
        for kind, row, labels in (("reference", self.ref_row, ref), ("moodboard", self.mood_row, mood)):
            default = any(item.get("source") == "backend defaults" for item in assets.get(kind, []))
            row.value_label.setText(", ".join(labels) or "-")
            row.value_label.setToolTip("\n".join(labels) + ("\nLoaded from backend defaults" if default else ""))
        missing = [a.get("file") or a["label"] for a in (assets.get("reference", []) + assets.get("moodboard", []))
                   if not a.get("embedded")]
        text = (f"On the server — reference: {escape(', '.join(ref)) or 'none'} | "
                f"moodboard ({len(mood)}): {escape(', '.join(mood)) or 'none'}")
        if missing:
            text += f"<br><span style='color:#b00'>not embedded: {escape(', '.join(missing))}</span>"
        self.assets_label.setText(text)
        self._status(f"Assets: {len(ref)} reference, {len(mood)} moodboard images")

    def _on_assets_error(self, msg):
        worker = self.sender()
        if (worker.url,worker.user_id,worker.session_id) != self.identity():
            return
        if worker.action == "list":
            self.assets_label.setText(f"On the server: unavailable ({msg})")
            return
        QtWidgets.QMessageBox.critical(self, "Asset Error", msg)

    # ------------------------------------------------------------------ /upload
    def execute_request(self, do_render, do_ai):
        if not self.valid_session():
            return
        selection = FreeCADGui.Selection.getSelection()
        user_id = self.user_id_input.text().strip()

        # v9: an empty selection is no longer an error. Nothing selected means
        # there is no geometry to condition the render on, so the request goes out
        # without an STL and the fcserver answers with its text-to-image pipeline.
        obj = selection[0] if selection else None
        self._set_buttons_enabled(False)

        stl_bytes = None
        if obj is not None:
            temp_dir = tempfile.gettempdir()
            stl_path = os.path.join(temp_dir, f"{obj.Name}.stl")
            try:
                Mesh.export([obj], stl_path)
                stl_bytes = self._read(stl_path)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Export Error", str(e))
                self._set_buttons_enabled(True)
                return
            finally:
                if os.path.exists(stl_path):
                    os.remove(stl_path)

        camera_data = self.extract_camera() if obj is not None else None
        extra_files = {}
        if FreeCAD.ActiveDocument:
            try:
                extra_files["design_document"] = ("current.FCStd", self.document_bytes(), "application/octet-stream")
            except Exception as exc:
                self._on_upload_error("Cannot snapshot the current design: "+str(exc))
                return
        if do_render and self.init_image_bytes:
            extra_files["init_image"] = (self.init_image_name or "initial.png",
                                         self.init_image_bytes, "image/png")
        if obj is not None:
            shot = self.grab_viewport()
            if shot:
                extra_files["viewport_image"] = ("viewport.png", shot, "image/png")

        if do_render and self.sketch_path:
            try:
                from PIL import Image
                with Image.open(self.sketch_path) as image:
                    buffer = io.BytesIO()
                    image.convert("L").convert("RGB").save(buffer, format="PNG")
                extra_files["sketch"] = ("sketch.png", buffer.getvalue(), "image/png")
            except Exception as exc:
                self._on_upload_error(f"Cannot read edge reference: {exc}")
                return

        fields = {
            "user_id": user_id,
            "require_rating": str(do_render),
            "genai_server": self.server_url("Mesh Server"),
            "session_id": self.session_id_input.text().strip(),
            "target_description": self.target_desc_input.toPlainText(),
            "description": "metal",
            "question": self.question_input.text() if do_ai else "",
            "camera_position": str(camera_data["position"]) if camera_data else "",
            "camera_focal": str(camera_data["focal_point"]) if camera_data else "",
            "camera_up": str(camera_data["view_up"]) if camera_data else "",
            "steps": 20,
            "canny_low": 80,
            "canny_high": 150,
            "guidance": self.guid_slider.value() / 100.0,
            "strength": 0.98,
            "do_render": str(do_render),
            "do_ai": str(do_ai),
            "do_plot": "False",
        }

        if obj is None and do_render and not extra_files:
            self._status("Nothing selected — rendering from the text description "
                         "(text-to-image pipeline)")
        self.study_identity = self.identity()
        self.request_document = FreeCAD.ActiveDocument
        self.request_fc_url = self.fc_url()
        worker = self._register(UploadWorker(
            self.request_fc_url, stl_bytes, f"{obj.Name}.stl" if obj else None,
            fields, list(extra_files.items())))
        worker.status.connect(self._status)
        worker.error.connect(self._on_upload_error)
        self.render_requested = do_render
        worker.result_ready.connect(self._on_upload_finished)
        worker.start()

    def _on_upload_finished(self, res):
        try:
            if res.get("status", "success") != "success" or not res:
                self._on_upload_error(res.get("message", "unknown server error"))
                return
            if res.get("ai_response"):
                self.response_area.clear()
                self.response_area.setText(res["ai_response"])
            m = res.get("metrics") or {}
            if m:
                if m.get("object") == "none" or "volume" not in m:
                    line = "No object — generated from the text description"
                else:
                    line = (f"Vol: {m['volume']:.2f} | Area: {m['surface_area']:.2f} "
                            f"| Verts: {m['vertices']}")
                if m.get("alignment_source"):
                    line += f" | Aligned on: {m['alignment_source'].split(' (')[0]}"
                if m.get("render_mode"):
                    line += f" | Render: {m['render_mode']} ({m.get('edge_source')})"
                if m.get("clip_score") is not None:
                    line += f" | CLIP: {m['clip_score']:.3f}"
                if m.get("moodboard_axes"):
                    line += f" | Moodboard axes: {m['moodboard_axes']}"
                # v7: the cosines are also a classification, so say which axis the
                # design is most likely to fit and with what confidence
                align = m.get("alignments") or {}
                share = max((p for p in (align.get("current_percent") or [])
                             if p is not None), default=None)
                if align.get("current_winner") and share is not None:
                    mode_name = {"clip": "CLIP scale",
                                 "z": "calibrated"}.get(
                        (align.get("probability") or {}).get("mode"), "")
                    line += (f" | Likely axis: {align['current_winner']} "
                             f"({share:.0f}%"
                             + (f", {mode_name}" if mode_name else "") + ")")
                self.metrics_label.setText(line)
            images = dict(res.get("images") or {})
            if self.control_group.isChecked() and not SHOW_MOODBOARD_ALIGNMENT_PLOT:
                images.pop("analysis_plot", None)
            if images:
                if "isometric" in images:
                    self.proposal = (self.request_fc_url, res["prompt_id"], res["user_id"], res["session_id"])
                    self.proposal_document = None if res.get("pending_rating") else self.request_document
                    self.rating_required = True
                    self.usefulness.setCurrentIndex(0)
                    self.surprise.setCurrentIndex(0)
                    self.rating_box.show()
                    self.results_section.setChecked(True)
                    self._status("Rate the usefulness and surprise of this proposal to continue.")
                self.clear_images()
                self.last_images = dict(images)
                for name, b64 in images.items():
                    self.add_image(name, b64)
                if self.render_requested and not res.get("pending_rating"):
                    self.clear_sketch()
                    self.clear_initial_image()
                self.image_scroll.show()
                self.image_count_label.setText(
                    f"{len(images)} — scroll down, double-click to zoom")
                if self.viewer is not None:
                    self.viewer.show_images(images)
            if res.get("assets") and (self.request_fc_url, res.get("user_id"), res.get("session_id")) == self.identity():
                self._on_assets_done({"assets": res["assets"]})
        finally:
            self._set_buttons_enabled(True)

    def submit_rating(self):
        usefulness, surprise = self.usefulness.currentIndex(), self.surprise.currentIndex()
        if not usefulness or not surprise:
            QtWidgets.QMessageBox.information(self, "Proposal evaluation", "Choose both scores before submitting.")
            return
        self.rating_box.setEnabled(False)
        worker = self._register(RatingWorker(self.proposal, usefulness, surprise))
        worker.saved.connect(self._rating_saved)
        worker.error.connect(self._rating_error)
        worker.start()

    def _rating_saved(self):
        self.rating_required = False
        self.rating_box.setEnabled(True)
        self.rating_box.hide()
        self._set_buttons_enabled(True)
        self._status("Proposal rating saved.")

    def _rating_error(self, message):
        self.rating_box.setEnabled(True)
        self._status(f"Rating could not be saved. Retry submission: {message}")

    def _on_upload_error(self, msg):
        self.response_area.clear()
        self.response_area.setText(f"<b>Error:</b> {msg}")
        self._status(f"Upload failed: {msg}")
        self._set_buttons_enabled(True)

    # ------------------------------------------------------------------ /mesh
    @staticmethod
    def visible_meshes(doc):
        for obj in doc.Objects:
            if not obj.Visibility or (obj.isDerivedFrom("App::Part") and
                                      not obj.isDerivedFrom("PartDesign::Body")):
                continue
            parent = obj.getParentGeoFeatureGroup()
            while parent and parent.Visibility and not parent.isDerivedFrom("PartDesign::Body"):
                parent = parent.getParentGeoFeatureGroup()
            if parent:
                continue
            if obj.isDerivedFrom("Mesh::Feature"):
                part = obj.Mesh.copy()
            elif hasattr(obj, "Shape") and obj.Shape.Faces:
                vertices, faces = obj.Shape.tessellate(0.1)
                part = Mesh.Mesh((vertices, faces))
            else:
                continue
            parent = obj.getParentGeoFeatureGroup()
            placement = part.Placement
            if parent:
                placement = parent.getGlobalPlacement().multiply(placement)
            part.Placement = FreeCAD.Placement()
            part.transform(placement.toMatrix())
            if part.CountFacets:
                yield obj.Name, part

    def clear_aesthetic_plot(self):
        while self.aesthetic_plot_layout.count():
            item = self.aesthetic_plot_layout.takeAt(0)
            item.widget().deleteLater()
        self.aesthetic_note.clear()

    def clear_aesthetic_render(self):
        self.aesthetic_render_data = None
        while self.aesthetic_render_layout.count():
            self.aesthetic_render_layout.takeAt(0).widget().deleteLater()

    def update_aesthetic_button(self):
        identity = self.identity()
        busy = any(isinstance(worker, UploadWorker) and worker.endpoint == "aesthetic-evaluation"
                   and (worker.url, worker.fields["user_id"], worker.fields["session_id"]) == identity
                   for worker in self.workers)
        self.btn_aesthetic_render.setEnabled(not busy)
        ready = self.aesthetic_render_data is not None and self.aesthetic_render_data[0] == identity
        self.btn_aesthetic.setEnabled(
            self.aesthetic_assets_identity == identity and ready and not busy
            and not self.aesthetic_evaluation_locked
        )

    def evaluate_aesthetics(self, render_only=False):
        if render_only:
            self.aesthetic_evaluation_locked = False
        self.update_aesthetic_button()
        button = self.btn_aesthetic_render if render_only else self.btn_aesthetic
        if not button.isEnabled() or not self.valid_session():
            return
        if not render_only:
            self.aesthetic_evaluation_locked = True
            self.btn_aesthetic.setEnabled(False)
        self.clear_aesthetic_plot()
        doc = FreeCAD.ActiveDocument
        if not doc:
            self.aesthetic_note.setText("Open a document with visible parts.")
            return
        identity = self.identity()
        url, user, session = identity
        try:
            if render_only:
                prompt = self.aesthetic_prompt.toPlainText().strip()
                if not prompt:
                    self.aesthetic_note.setText("Enter a rendering prompt first.")
                    return
                self.clear_aesthetic_render()
                screenshot = self.grab_viewport(fit_view=AESTHETIC_VIEW)
            else:
                _, rendered_doc, screenshot, preview, prompt = self.aesthetic_render_data
                if rendered_doc != doc:
                    self.aesthetic_note.setText("The active document changed. Render the current design first.")
                    return
            if not screenshot:
                self.aesthetic_note.setText("Could not capture the current FreeCAD view.")
                return
            files = [("viewport_image", ("viewport.png", screenshot, "image/png"))]
            fields = {"user_id":user, "session_id":session, "genai_server":self.server_url("Mesh Server"),
                      "evaluation_view":AESTHETIC_VIEW, "render_only":str(render_only), "render_prompt":prompt}
            if not render_only:
                files.append(("rendered_image", ("render.png", base64.b64decode(preview), "image/png")))
            if AESTHETIC_VIEW == "custom":
                fields["evaluation_camera"] = json.dumps({
                    "position":AESTHETIC_CAMERA_POSITION, "direction":AESTHETIC_CAMERA_DIRECTION,
                    "up":AESTHETIC_CAMERA_UP, "height":AESTHETIC_CAMERA_HEIGHT,
                    "fit_all":AESTHETIC_CUSTOM_FIT_ALL})
            if self.proposal and self.proposal_document == doc and (self.proposal[0], *self.proposal[2:]) == identity:
                fields["proposal_id"] = self.proposal[1]
            worker = self._register(UploadWorker(url, None, None, fields, files, endpoint="aesthetic-evaluation"))
        except Exception as exc:
            self.aesthetic_note.setText(str(exc))
            return
        self.btn_aesthetic.setEnabled(False)
        self.btn_aesthetic_render.setEnabled(False)
        self.aesthetic_note.setText("Rendering design…" if render_only else "Evaluating design aesthetics…")
        revision = self.session_revision
        def completed(result):
            if self.identity() != identity or self.session_revision != revision:
                return
            if FreeCAD.ActiveDocument != doc:
                self.aesthetic_note.setText("The active document changed. Render the current design again.")
                return
            if render_only:
                preview = result.get("visualization")
                if not preview:
                    self.aesthetic_note.setText("No render returned. Try rendering again.")
                    return
                self.aesthetic_render_data = (identity, doc, screenshot, preview, prompt)
                card = ImageCard("Rendered design", preview,
                                 lambda title: self.open_results({title:preview}), self.save_image)
                self.aesthetic_render_layout.addWidget(card)
                self.aesthetic_note.setText("Render ready. Click Evaluate visible design to evaluate this image.")
                return
            plot = result.get("analysis_plot")
            note = result.get("plot_note") or ("Evaluation complete." if plot else "No plot available.")
            if result.get("render_reused"):
                note += " Reused the visualization for this design."
            self.aesthetic_note.setText(note)
            for name, image in (("Moodboard alignment", plot),):
                if name == "Moodboard alignment" and self.control_group.isChecked() and not SHOW_MOODBOARD_ALIGNMENT_PLOT:
                    continue
                if image:
                    card = ImageCard(name, image,
                                     lambda title, payload=image: self.open_results({title:payload}), self.save_image)
                    self.aesthetic_plot_layout.addWidget(card)
        worker.result_ready.connect(completed)
        worker.error.connect(lambda msg: self.aesthetic_note.setText(msg) if self.identity() == identity else None)
        worker.start()

    def evaluate_stability(self):
        self.last_stability_result = None
        if not self.valid_session():
            return
        identity = self.identity()
        url, user, session = identity
        doc = FreeCAD.ActiveDocument
        if not doc:
            self.stability_result.setText("Open a document with visible parts.")
            return
        self.btn_stability.setEnabled(False)
        self.stability_result.setText("Evaluating visible parts…")
        try:
            assembly = Mesh.Mesh()
            for _, part in self.visible_meshes(doc):
                assembly.addMesh(part)
            if not assembly.CountFacets:
                raise ValueError("No visible mesh or solid parts to evaluate.")
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "assembly.stl")
                assembly.write(path)
                fields = {"user_id":user, "session_id":session, "document_name":doc.Name, "document_label":doc.Label}
                if self.proposal and self.proposal_document == doc and (self.proposal[0], *self.proposal[2:]) == identity:
                    fields["proposal_id"] = self.proposal[1]
                worker = self._register(StabilityWorker(url, self._read(path), fields))
            worker.result_ready.connect(self._show_stability)
            worker.error.connect(self.stability_result.setText)
            worker.finished.connect(self._stability_finished)
            worker.start()
        except Exception as exc:
            self.stability_result.setText(str(exc))
            self._stability_finished()

    def _show_stability(self, result):
        self.last_stability_result = dict(result)
        angle = result["tilt_angle_deg"]
        evaluation_id = result["evaluation_id"]
        log = result["event_log"]
        text = f"Tilt angle: {angle:.2f}°\nEvaluation ID: {evaluation_id}\nServer log: {log}"
        passed = result.get("stability_passed")
        if isinstance(passed, bool):
            text += "\nStability: " + ("Passed" if passed else "Failed")
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            text += "\n" + message.strip()
        self.stability_result.setTextFormat(QtCore.Qt.PlainText)
        self.stability_result.setText(text)

    def _stability_finished(self):
        self.btn_stability.setEnabled(True)

    def scale_selected_mesh(self):
        selection = FreeCADGui.Selection.getSelection()
        if len(selection) != 1 or not selection[0].isDerivedFrom("Mesh::Feature"):
            QtWidgets.QMessageBox.information(self, "STL transformations", "Select one mesh to scale.")
            return
        obj = selection[0]
        factor = self.scale_slider.value() / 100.0
        if factor == 1.0:
            return
        axis = self.scale_axis.currentIndex()
        doc = obj.Document
        doc.openTransaction("Scale mesh")
        try:
            mesh = obj.Mesh.copy()
            placement = mesh.Placement
            mesh.Placement = FreeCAD.Placement()
            center = mesh.BoundBox.Center
            matrix = FreeCAD.Matrix()
            matrix.scale(*(factor if axis in (0, i) else 1.0 for i in (1, 2, 3)))
            offset = center - matrix.multVec(center)
            matrix.A14, matrix.A24, matrix.A34 = offset.x, offset.y, offset.z
            mesh.transform(matrix)
            mesh.Placement = placement
            obj.Mesh = mesh
            doc.recompute()
            doc.commitTransaction()
        except Exception as exc:
            doc.abortTransaction()
            QtWidgets.QMessageBox.critical(self, "Scale mesh", str(exc))
            return
        if self.study_identity:
            self.record_event("cad_edit", {"action":"scale_mesh", "document":doc.Name, "object":obj.Name,
                                          "axis":self.scale_axis.currentText(), "factor":factor})
        self.scale_slider.setValue(100)

    def request_mesh_generation(self):
        image_b64 = self.last_images.get("isometric")
        if not image_b64 or not self.valid_session():
            return
        selection = FreeCADGui.Selection.getSelection()
        if not selection:
            QtWidgets.QMessageBox.warning(self, "Mesh Error", "Select a reference object first.")
            return
        try:
            reference = selection[0]
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "reference.stl")
                Mesh.export([reference], path)
                reference_stl = self._read(path)
            if not reference_stl:
                raise ValueError("Reference STL is empty.")
            self.mesh_document = reference.Document
            url, prompt_id, user, session = self.proposal
            worker = self._register(MeshWorker(
                self.server_url("Mesh Server"), image_b64, reference_stl,
                user, session,
                self.mesh_steps_slider.value(), self.octree_slider.value(),
                self.alignment_mode_combo.currentText(), f"{url}/proposals/{prompt_id}"))
            worker.status.connect(self._status)
            worker.error.connect(self._on_mesh_error)
            worker.mesh_ready.connect(self.on_mesh_finished)
            self._set_buttons_enabled(False)
            worker.start()
        except Exception as exc:
            self._on_mesh_error(str(exc))

    def _on_mesh_error(self, err_msg):
        QtWidgets.QMessageBox.critical(self, "Mesh Error", err_msg)
        self._set_buttons_enabled(True)

    def on_mesh_finished(self, mesh_bytes, job_id):
        try:
            self.importing = True
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "registered.stl")
                with open(path, "wb") as fh:
                    fh.write(mesh_bytes)
                mesh = Mesh.read(path)
            if not mesh.CountFacets:
                raise ValueError("The registered STL contains no faces.")
            center = mesh.BoundBox.Center
            to_local = FreeCAD.Matrix()
            to_local.move(-center)
            mesh.transform(to_local)
            doc = self.mesh_document
            obj = doc.addObject("Mesh::Feature", f"GenMesh_{int(time.time())}")
            obj.Mesh = mesh
            obj.Placement = FreeCAD.Placement(center, FreeCAD.Rotation())
            doc.recompute()
            if FreeCAD.ActiveDocument == doc:
                FreeCADGui.activeDocument().activeView().fitAll()
            FreeCADGui.updateGui()
            self.record_event("mesh_imported", {"job_id":job_id, "document":doc.Name, "object":obj.Name}, self.proposal)
            self._status(f"Imported registered mesh {obj.Name}")
            QtWidgets.QMessageBox.information(self, "3D object imported",
                                             f"Added {obj.Label} to {doc.Label}.")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Import Error", str(exc))
        finally:
            self.importing = False
            self._set_buttons_enabled(True)


def init_plugin():
    mw = FreeCADGui.getMainWindow()
    for widget in mw.findChildren(QtWidgets.QDockWidget):
        if widget.objectName() != "RemoteAnalysisStudyDock" and not isinstance(widget, RemoteAnalysisDock):
            continue
        if getattr(widget, "observer", None) is not None:
            FreeCAD.removeDocumentObserver(widget.observer)
        if hasattr(widget, "event_timer"):
            widget.event_timer.stop()
        if hasattr(widget, "evaluation_timer"):
            widget.stop_evaluation_reminders()
        mw.removeDockWidget(widget)
    dock = RemoteAnalysisDock()
    mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
    dock.show()


if __name__ == "__main__":
    init_plugin()
