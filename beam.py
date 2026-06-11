#!/usr/bin/env python3
"""
Beam — Send files from your phone to your computer via QR code.
Local network only. No accounts. No cloud.
"""
from __future__ import annotations

import io
import sys
import socket
import threading
from pathlib import Path
from typing import List

# GUI
from PySide6.QtCore import Qt, QObject, Signal, QPoint, QTimer
from PySide6.QtGui import (
    QAction, QBrush, QColor, QIcon, QImage,
    QLinearGradient, QPainter, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMenu, QMessageBox,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

# QR code
import qrcode

# Web server
import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

# Desktop notifications
try:
    from plyer import notification as _plyer
    _HAS_NOTIFY = True
except Exception:
    _HAS_NOTIFY = False

PORT = 8000
APP  = "Beam"


# --- Helpers

def set_status(msg):
    global server_status
    server_status = msg

def local_ip() -> str:
    """Detect the machine's LAN IP. Returns 127.0.0.1 if detection fails."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # routes the socket without sending data
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_available(port: int) -> bool:
    """Return True if the given TCP port is free on all interfaces."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def beam_dir() -> Path:
    """Return ~/Downloads/Beam, creating it on first use."""
    d = Path.home() / "Downloads" / "Beam"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_filename(raw: str) -> str:
    """Strip directory components to prevent path traversal attacks."""
    # Replace backslashes so Path works correctly on all platforms
    name = Path(raw.replace("\\", "/")).name
    return name or "upload"


def desktop_notify(msg: str) -> None:
    """Show a Windows desktop notification if plyer is available."""
    if _HAS_NOTIFY:
        try:
            _plyer.notify(title=APP, message=msg, app_name=APP, timeout=5)
        except Exception:
            pass


def make_qr_pixmap(url: str, size: int = 300) -> QPixmap:
    """Generate a QR code for *url* and return it as a QPixmap."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#18181b", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    pix = QPixmap()
    pix.loadFromData(buf.getvalue())
    return pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def make_app_icon() -> QIcon:
    """Draw a gradient lightning-bolt icon programmatically."""
    sz = 64
    img = QImage(sz, sz, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    grad = QLinearGradient(0, 0, sz, sz)
    grad.setColorAt(0.0, QColor("#3b82f6"))
    grad.setColorAt(1.0, QColor("#8b5cf6"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, sz - 4, sz - 4)

    p.setBrush(QBrush(QColor("#ffffff")))
    bolt = QPolygon([
        QPoint(38,  8), QPoint(22, 34), QPoint(33, 34),
        QPoint(26, 56), QPoint(44, 30), QPoint(31, 30),
    ])
    p.drawPolygon(bolt)
    p.end()
    return QIcon(QPixmap.fromImage(img))


# --- Server → Qt bridge

class Bridge(QObject):
    """Carries signals from the HTTP thread into the Qt main thread."""
    files_received = Signal(list)   # list[str] filenames


_bridge = Bridge()


# --- FastAPI

_api = FastAPI()

# Emojis use HTML entities to avoid Python unicode-escape errors.
_UPLOAD_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Beam</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #09090b; color: #fafafa;
  min-height: 100dvh;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  padding: 24px 16px; gap: 12px;
}
.logo {
  font-size: 2rem; font-weight: 800; letter-spacing: -.02em;
  background: linear-gradient(135deg, #60a5fa, #a78bfa);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.sub { color: #71717a; font-size: .9rem; text-align: center; }
.drop {
  width: 100%; max-width: 420px;
  border: 2px dashed #3f3f46; border-radius: 20px;
  padding: 44px 24px; text-align: center;
  cursor: pointer; transition: .2s; background: #18181b;
}
.drop.over, .drop:hover { border-color: #3b82f6; background: #1e2a3a; }
.drop-icon { font-size: 2.4rem; margin-bottom: 10px; }
.drop p { color: #71717a; font-size: .9rem; line-height: 1.6; }
.drop em { color: #60a5fa; font-style: normal; font-weight: 600; }
#fi { display: none; }
.list { width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 6px; }
.row {
  background: #18181b; border: 1px solid #27272a; border-radius: 10px;
  padding: 9px 13px; display: flex; align-items: center; gap: 8px; font-size: .85rem;
}
.row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #e4e4e7; }
.row .size { color: #52525b; font-size: .75rem; flex-shrink: 0; }
.btn {
  width: 100%; max-width: 420px; padding: 15px;
  border: none; border-radius: 14px;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  color: #fff; font-size: 1rem; font-weight: 700;
  cursor: pointer; display: none; letter-spacing: .01em; transition: opacity .15s;
}
.btn:hover { opacity: .88; }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.prog-wrap {
  width: 100%; max-width: 420px; height: 4px;
  background: #27272a; border-radius: 99px; display: none; overflow: hidden;
}
.prog-bar {
  height: 100%; border-radius: 99px;
  background: linear-gradient(90deg, #3b82f6, #6366f1);
  width: 0%; transition: width .2s;
}
.toast {
  width: 100%; max-width: 420px; padding: 12px 16px;
  border-radius: 12px; font-size: .88rem; text-align: center; display: none;
}
.ok  { background: #052e16; color: #4ade80; border: 1px solid #166534; }
.err { background: #2d0a0a; color: #f87171; border: 1px solid #991b1b; }
</style>
</head>
<body>

<div class="logo">&#9889; Beam</div>
<p class="sub">Wirelessly send files to your computer</p>

<div class="drop" id="dz">
  <div class="drop-icon">&#128228;</div>
  <p><em>Tap to choose files</em><br>or drop them here</p>
  <input id="fi" type="file" multiple>
</div>

<div class="list" id="lst"></div>
<div class="prog-wrap" id="pw"><div class="prog-bar" id="pb"></div></div>
<button class="btn" id="btn">Send Files</button>
<div class="toast" id="toast"></div>

<script>
var dz    = document.getElementById('dz');
var fi    = document.getElementById('fi');
var lst   = document.getElementById('lst');
var btn   = document.getElementById('btn');
var pw    = document.getElementById('pw');
var pb    = document.getElementById('pb');
var toast = document.getElementById('toast');

var selected = [];

function fmt(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function render() {
  lst.innerHTML = selected.map(function(f) {
    return (
      '<div class="row">' +
      '<span>&#128196;</span>' +
      '<span class="name">' + f.name + '</span>' +
      '<span class="size">' + fmt(f.size) + '</span>' +
      '</div>'
    );
  }).join('');
  btn.style.display = selected.length ? 'block' : 'none';
}

dz.addEventListener('click', function() { fi.click(); });
fi.addEventListener('change', function(e) {
  selected = Array.from(e.target.files);
  render();
});

['dragover', 'dragenter'].forEach(function(ev) {
  dz.addEventListener(ev, function(e) {
    e.preventDefault();
    dz.classList.add('over');
  });
});
['dragleave', 'dragend'].forEach(function(ev) {
  dz.addEventListener(ev, function() { dz.classList.remove('over'); });
});
dz.addEventListener('drop', function(e) {
  e.preventDefault();
  dz.classList.remove('over');
  selected = Array.from(e.dataTransfer.files);
  render();
});

btn.addEventListener('click', function() {
  if (!selected.length) return;
  var fd = new FormData();
  selected.forEach(function(f) { fd.append('files', f); });

  btn.disabled = true;
  pw.style.display = 'block';
  pb.style.width = '0%';
  toast.style.display = 'none';

  var xhr = new XMLHttpRequest();

  xhr.upload.addEventListener('progress', function(e) {
    if (e.lengthComputable) {
      pb.style.width = (e.loaded / e.total * 100) + '%';
    }
  });

  xhr.addEventListener('load', function() {
    pb.style.width = '100%';
    if (xhr.status === 200) {
      var n = selected.length;
      showToast('ok', 'Sent ' + n + ' file' + (n > 1 ? 's' : '') + ' successfully!');
      selected = [];
      lst.innerHTML = '';
      btn.style.display = 'none';
      fi.value = '';
    } else {
      showToast('err', 'Upload failed. Please try again.');
    }
    btn.disabled = false;
    setTimeout(function() { pw.style.display = 'none'; }, 1500);
  });

  xhr.addEventListener('error', function() {
    showToast('err', 'Network error. Please try again.');
    btn.disabled = false;
    setTimeout(function() { pw.style.display = 'none'; }, 1500);
  });

  xhr.open('POST', '/upload');
  xhr.send(fd);
});

function showToast(cls, msg) {
  toast.className = 'toast ' + cls;
  toast.textContent = msg;
  toast.style.display = 'block';
}
</script>
</body>
</html>"""


@_api.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _UPLOAD_PAGE


@_api.post("/upload")
async def upload(files: List[UploadFile] = File(...)) -> JSONResponse:
    set_status("Receiving Upload...")
    try:
        dest = beam_dir()
    except OSError as e:
        return JSONResponse({"success": False, "error": f"Cannot create save directory: {e}"}, status_code=500)

    saved: list[str] = []
    try:
        for f in files:
            name = sanitize_filename(f.filename or "upload")
            path = dest / name

            # Avoid overwriting: foo.jpg → foo_1.jpg → foo_2.jpg …
            if path.exists():
                stem, suf = Path(name).stem, Path(name).suffix
                i = 1
                while path.exists():
                    path = dest / f"{stem}_{i}{suf}"
                    i += 1

            # Stream to disk in 1 MB chunks — safe for large files
            with open(path, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

            saved.append(path.name)

    except PermissionError as e:
        return JSONResponse({"success": False, "error": f"Permission denied: {e}"}, status_code=500)
    except OSError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Unexpected error: {e}"}, status_code=500)

    set_status(f"Received {len(saved)} files")
    _bridge.files_received.emit(saved)
    set_status("Waiting for Uploads")
    return JSONResponse({"success": True})


# --- HTTP server

class BeamServer:
    def __init__(self, port: int = PORT) -> None:
        self._port = port
        self._server: uvicorn.Server | None = None

    def start(self) -> None:
        set_status("Server Starting...")
        cfg = uvicorn.Config(
            app=_api,
            host="0.0.0.0",
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        set_status(f"Running at http://{local_ip()}:{self._port}")
        self._server = uvicorn.Server(cfg)
        threading.Thread(
            target=self._server.run,
            daemon=True,
            name="beam-http",
        ).start()

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True


# --- Main window

_CSS = """
QMainWindow, #root { background: #09090b; }
QLabel              { color: #e4e4e7; }
#title   { color: #60a5fa; font-size: 26px; font-weight: 800; }
#sub     { color: #71717a; font-size: 13px; }
#url     { color: #a1a1aa;
           font-family: Consolas, 'Courier New', monospace;
           font-size: 14px;
           background: #18181b;
           border: 1px solid #3f3f46;
           border-radius: 8px;
           padding: 7px 16px; }
#status  { color: #71717a; font-size: 12px; }
#warning { color: #f59e0b; font-size: 12px; }
"""


class BeamWindow(QMainWindow):
    def __init__(self, url: str, server: BeamServer, lan_ok: bool = True) -> None:

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(500)

        super().__init__()
        self._server = server
        self.url = url
        self._lan_ok = lan_ok
        self._tray: QSystemTrayIcon | None = None

        self.setWindowTitle(APP)
        self.setFixedWidth(400)
        self.setStyleSheet(_CSS)

        icon = make_app_icon()
        self.setWindowIcon(icon)
        self._build_ui()
        self._build_tray(icon)
        _bridge.files_received.connect(self._on_files)

    # layout
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        lay = QVBoxLayout(root)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignHCenter)

        def lbl(text: str, oid: str, wrap: bool = False) -> QLabel:
            w = QLabel(text)
            w.setObjectName(oid)
            w.setAlignment(Qt.AlignCenter)
            if wrap:
                w.setWordWrap(True)
            return w

        lay.addWidget(lbl("⚡ Beam", "title"))
        lay.addWidget(lbl("Scan to send files from your phone", "sub", wrap=True))
        lay.addSpacing(4)

        try:
            qr_lbl = QLabel()
            qr_lbl.setAlignment(Qt.AlignCenter)
            qr_lbl.setPixmap(make_qr_pixmap(self.url))
            lay.addWidget(qr_lbl)
        except Exception:
            lay.addWidget(lbl("(QR code unavailable)", "sub"))

        url_lbl = lbl(self.url, "url")
        url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(url_lbl)

        lay.addSpacing(4)

        if not self._lan_ok:
            lay.addWidget(lbl(
                "No LAN IP detected. Phone uploads may not work.",
                "warning", wrap=True,
            ))

        self._status = lbl("Waiting for uploads...", "status")
        lay.addWidget(self._status)

    # refresh
    def _refresh_status(self):
        self._status.setText(server_status)

    # system tray

    def _build_tray(self, icon: QIcon) -> None:
        try:
            tray = QSystemTrayIcon(icon, self)
            menu = QMenu()

            show_act = QAction("Show", self)
            show_act.triggered.connect(self._raise)
            quit_act = QAction("Exit", self)
            quit_act.triggered.connect(self._quit)

            menu.addAction(show_act)
            menu.addSeparator()
            menu.addAction(quit_act)

            tray.setContextMenu(menu)
            tray.setToolTip(f"{APP}  ·  {self.url}")
            tray.activated.connect(self._tray_activated)
            tray.show()
            self._tray = tray
        except Exception:
            pass  # tray unavailable; app still works

    # slots

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._raise()

    def _raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._server.stop()
        QApplication.quit()

    def _on_files(self, names: list[str]) -> None:
        n = len(names)
        msg = f"Received: {names[0]}" if n == 1 else f"Received {n} files"
        self._status.setText(msg)
        desktop_notify(msg)

    # close 

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._server.stop()
        QApplication.quit()
        event.accept()


# --- Entry point

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)  # stay alive when window is hidden

    if not port_available(PORT):
        QMessageBox.critical(
            None,
            f"{APP} — Port In Use",
            f"Port {PORT} is already in use.\n\n"
            "Please close the conflicting application and try again.",
        )
        sys.exit(1)

    ip     = local_ip()
    lan_ok = ip != "127.0.0.1"
    url    = f"http://{ip}:{PORT}"

    server = BeamServer()
    server.start()
    win = BeamWindow(url, server, lan_ok=lan_ok)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()