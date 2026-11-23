"""GUI duman testi: ana pencere kurulur, tek dosya GUI'nin kullandığı işçi/kuyruk
yolundan işlenir, pencere yok edilir. Ekran (DISPLAY) ve çalışan bir Tk gerekir;
CI'da `xvfb-run -a pytest` ile çalışır, yerelde ekran yoksa atlanır."""
from __future__ import annotations

import os
import shutil
import sys

import pytest
from PIL import Image

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    if shutil.which("xvfb-run"):
        pytest.skip("DISPLAY yok; testi `xvfb-run -a pytest` ile çalıştırın", allow_module_level=True)
    pytest.skip("DISPLAY ve xvfb yok; GUI testi atlandı", allow_module_level=True)

tk = pytest.importorskip("tkinter", reason="tkinter kurulu değil", exc_type=ImportError)
pytest.importorskip("customtkinter", reason="customtkinter kurulu değil", exc_type=ImportError)


@pytest.fixture
def app(monkeypatch):
    import gui
    try:
        window = gui.App()
    except tk.TclError as exc:                      # ekran açılamıyor
        pytest.skip(f"Tk başlatılamadı: {exc}")
    window.withdraw()
    # Özet penceresi testi bloklamasın
    shown: list[str] = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, msg, **kw: shown.append(msg))
    window.shown = shown
    yield window
    window.update_idletasks()
    window.destroy()


def test_main_window_processes_a_file_via_worker_queue(app, tmp_path):
    src = tmp_path / "foto.jpg"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(src, "JPEG")

    app.add_files([src, tmp_path / "x.unsupported"])
    assert app.files == [src] and len(app.file_rows) == 1

    # Tıklama yok: _start_cleaning'in iş parçacığında çalıştırdığı _worker doğrudan çağrılır,
    # ardından GUI'nin kuyruk tüketicisi (_poll_queue) çalıştırılır.
    app._cleaning = True
    app._worker()
    app._poll_queue()

    assert not app._cleaning
    result = app._results[0]
    assert result.status == "success"
    assert result.cleaned_path.exists() and result.cleaned_path.name == "foto_cleaned.jpg"
    assert app.shown and "Başarılı  : 1" in app.shown[0]
