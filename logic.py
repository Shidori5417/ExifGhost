"""
logic.py — ExifGhost metadata temizleme motoru
Her format için bağımsız cleaner; ortak CleanResult arayüzü.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import piexif
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError

# ─── Desteklenen formatlar ────────────────────────────────────────────────────
SUPPORTED_IMAGE = {".jpg", ".jpeg", ".png"}
SUPPORTED_AUDIO = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav"}
SUPPORTED_VIDEO = {".mp4", ".mov", ".m4v"}
SUPPORTED_ALL   = SUPPORTED_IMAGE | SUPPORTED_AUDIO | SUPPORTED_VIDEO


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_ALL


# ─── Sonuç modeli ─────────────────────────────────────────────────────────────
@dataclass
class CleanResult:
    original_path: Path
    cleaned_path:  Path | None = None
    status:        Literal["success", "skipped", "error"] = "skipped"
    removed_fields: list[str] = field(default_factory=list)
    error_msg:     str | None = None


# ─── Yardımcı ─────────────────────────────────────────────────────────────────
def _out(path: Path) -> Path:
    """Aynı klasörde filename_cleaned.ext döndürür."""
    return path.with_stem(f"{path.stem}_cleaned")


def _collect_exif_keys(img: Image.Image) -> list[str]:
    """EXIF varsa alan isimlerini toplar (raporlama için)."""
    keys: list[str] = []
    raw = img.info.get("exif")
    if not raw:
        return keys
    try:
        exif = piexif.load(raw)
        for ifd_name in ("0th", "Exif", "GPS", "1st", "Interop"):
            ifd = exif.get(ifd_name, {})
            for tag_id in ifd:
                tag_info = piexif.TAGS.get(ifd_name, {}).get(tag_id, {})
                keys.append(tag_info.get("name", str(tag_id)))
    except Exception:
        keys.append("EXIF Data")
    return keys


# ─── Temizleyiciler ───────────────────────────────────────────────────────────
def _clean_jpeg(path: Path) -> CleanResult:
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        img = Image.open(path)
        removed = _collect_exif_keys(img)

        # EXIF olmadan yeniden kaydet
        img_clean = img.copy()
        save_kw: dict = {"format": "JPEG", "quality": 95}
        img_clean.save(output, **save_kw)

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(EXIF zaten boş)"]
    except Exception as exc:
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _clean_png(path: Path) -> CleanResult:
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        img = Image.open(path)
        removed = list(img.info.keys()) if img.info else []

        img_clean = img.copy()
        img_clean.save(output, format="PNG", pnginfo=PngInfo())

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(Metadata zaten boş)"]
    except Exception as exc:
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _clean_mutagen(path: Path) -> CleanResult:
    """MP3 / FLAC / OGG / M4A / MP4 / MOV için mutagen tabanlı temizleyici."""
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        shutil.copy2(path, output)  # önce kopyala, kopyayı değiştir

        # MP3: ID3 direkt sil
        if path.suffix.lower() == ".mp3":
            try:
                tags = ID3(output)
                removed = list(tags.keys())
                tags.delete(output)
            except ID3NoHeaderError:
                removed = []
            result.status = "success"
            result.cleaned_path = output
            result.removed_fields = removed or ["(Tag bulunamadı)"]
            return result

        # Diğer formatlar
        audio = MutagenFile(output, easy=False)
        if audio is None:
            result.status = "skipped"
            result.error_msg = "Mutagen bu formatı tanımadı"
            output.unlink(missing_ok=True)
            return result

        removed = []
        if audio.tags:
            try:
                removed = list(audio.tags.keys())
            except Exception:
                removed = ["tags"]
            audio.delete()   # tag'leri diskten sil

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(Tag zaten boş)"]

    except Exception as exc:
        output.unlink(missing_ok=True)
        result.status = "error"
        result.error_msg = str(exc)
    return result


# ─── Ana giriş noktası ────────────────────────────────────────────────────────
def clean_file(path: Path) -> CleanResult:
    """Format tespiti yapıp uygun temizleyiciye yönlendirir."""
    if not path.exists():
        return CleanResult(original_path=path, status="error", error_msg="Dosya bulunamadı")

    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return _clean_jpeg(path)
    elif ext == ".png":
        return _clean_png(path)
    elif ext in SUPPORTED_AUDIO | SUPPORTED_VIDEO:
        return _clean_mutagen(path)
    else:
        return CleanResult(original_path=path, status="skipped", error_msg="Desteklenmeyen format")
