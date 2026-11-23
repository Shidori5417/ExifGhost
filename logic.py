"""
logic.py — ExifGhost metadata temizleme motoru
Her format için bağımsız cleaner; ortak CleanResult arayüzü.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import piexif
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError
from PIL import Image, ImageOps, ImageSequence
from PIL.PngImagePlugin import PngInfo

# ─── Desteklenen formatlar ────────────────────────────────────────────────────
SUPPORTED_IMAGE = {".jpg", ".jpeg", ".png"}
SUPPORTED_AUDIO = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav"}
SUPPORTED_VIDEO = {".mp4", ".mov", ".m4v"}
SUPPORTED_ALL   = SUPPORTED_IMAGE | SUPPORTED_AUDIO | SUPPORTED_VIDEO

# PNG'de metadata sayılmayan, görüntünün doğru çizilmesi için gereken alanlar
_PNG_STRUCTURAL = {"icc_profile", "transparency", "duration", "loop",
                   "default_image", "blend", "disposal"}
# WAV'da korunan yapısal chunk'lar; geri kalan her şey (LIST/INFO, id3, bext,
# iXML, XMP, cue vb.) atılır.
_WAV_KEEP = {b"fmt ", b"fact", b"data"}


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
    """Çakışmayan çıktı yolu: x_cleaned.ext, doluysa x_cleaned (2).ext, ..."""
    candidate = path.with_stem(f"{path.stem}_cleaned")
    n = 2
    while candidate.exists():
        candidate = path.with_stem(f"{path.stem}_cleaned ({n})")
        n += 1
    return candidate


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


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


def _collect_jpeg_extras(img: Image.Image) -> list[str]:
    """EXIF dışındaki JPEG metadata segmentleri: XMP, IPTC/Photoshop, COM."""
    extras: list[str] = []
    applist = getattr(img, "applist", [])
    if any(data.startswith(b"http://ns.adobe.com/xap/1.0/") for _, data in applist) \
            or img.info.get("xmp"):
        extras.append("XMP")
    if any(marker == "APP13" for marker, _ in applist) or img.info.get("photoshop"):
        extras.append("IPTC/Photoshop")
    if img.info.get("comment"):
        extras.append("COM (yorum)")
    return extras


# ─── Temizleyiciler ───────────────────────────────────────────────────────────
def _clean_jpeg(path: Path) -> CleanResult:
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        with Image.open(path) as img:
            removed = _collect_exif_keys(img) + _collect_jpeg_extras(img)
            icc = img.info.get("icc_profile")
            # Orientation EXIF'i atılacağı için döndürmeyi piksellere uygula
            clean = ImageOps.exif_transpose(img)
            # Pillow info içindeki comment/xmp vb. alanları yazabilir; sadece ICC kalsın
            clean.info = {"icc_profile": icc} if icc else {}
            clean.save(output, format="JPEG", quality=95,
                       **({"icc_profile": icc} if icc else {}))

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(EXIF zaten boş)"]
    except Exception as exc:
        output.unlink(missing_ok=True)
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _clean_png(path: Path) -> CleanResult:
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        with Image.open(path) as img:
            icc = img.info.get("icc_profile")
            save_kw: dict = {"format": "PNG", "pnginfo": PngInfo()}
            if icc:
                save_kw["icc_profile"] = icc

            if getattr(img, "n_frames", 1) > 1:
                # APNG: tüm kareleri süre ve döngü bilgisiyle koru
                frames, durations = [], []
                for frame in ImageSequence.Iterator(img):
                    durations.append(frame.info.get("duration", 0))
                    frames.append(frame.copy())
                img.load()
                removed = [k for k in img.info if k not in _PNG_STRUCTURAL]
                frames[0].save(output, save_all=True, append_images=frames[1:],
                               duration=durations, loop=img.info.get("loop", 0),
                               **save_kw)
            else:
                img.load()
                removed = [k for k in img.info if k not in _PNG_STRUCTURAL]
                img.copy().save(output, **save_kw)

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(Metadata zaten boş)"]
    except Exception as exc:
        output.unlink(missing_ok=True)
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _probe_tags(path: Path) -> list[str]:
    """ffprobe ile konteyner/akış etiketlerini listeler (raporlama için)."""
    probe = _ffprobe()
    if not probe:
        return []
    try:
        proc = subprocess.run(
            [probe, "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
    except Exception:
        return []
    found: list[str] = []
    for key in (data.get("format", {}).get("tags") or {}):
        found.append(key)
    for st in data.get("streams", []):
        idx = st.get("index")
        if st.get("codec_type") == "data":
            found.append(f"stream{idx}:veri akışı")
        elif st.get("disposition", {}).get("attached_pic"):
            found.append(f"stream{idx}:kapak görseli")
        for key in (st.get("tags") or {}):
            if key not in {"handler_name", "language", "vendor_id"}:
                found.append(f"stream{idx}:{key}")
    return found


def _clean_ffmpeg(path: Path) -> CleanResult:
    """MP4 / MOV / M4V: ffmpeg ile akışlar aynen kopyalanır, metadata atılır."""
    result = CleanResult(original_path=path)
    exe = _ffmpeg()
    if not exe:
        result.status = "error"
        result.error_msg = ("ffmpeg bulunamadı: video dosyalarının metadata'sı "
                            "(GPS konumu dahil) ffmpeg olmadan güvenle silinemez. "
                            "ffmpeg kurup tekrar deneyin.")
        return result
    output = _out(path)
    try:
        removed = _probe_tags(path)
        cmd = [exe, "-nostdin", "-v", "error", "-i", str(path),
               # Video/ses/altyazı akışları; kapak görseli ve veri akışları (GPS
               # gibi zamanlı metadata) alınmaz.
               "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
               "-map_metadata", "-1", "-map_chapters", "-1",
               "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1",
               "-map_metadata:s:s", "-1",
               "-c", "copy", "-fflags", "+bitexact",
               "-flags:v", "+bitexact", "-flags:a", "+bitexact",
               str(output)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not output.exists():
            raise RuntimeError(f"ffmpeg hata verdi: {proc.stderr.strip()[-500:]}")
        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(Tag bulunamadı)"]
    except Exception as exc:
        output.unlink(missing_ok=True)
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _clean_wav(path: Path) -> CleanResult:
    """WAV: RIFF dosyası yeniden yazılır; yalnızca fmt/fact/data chunk'ları kalır."""
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        removed: list[str] = []
        size_total = path.stat().st_size
        with open(path, "rb") as src, open(output, "wb") as dst:
            head = src.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                raise ValueError("Geçerli bir RIFF/WAVE dosyası değil (RF64 desteklenmez)")
            dst.write(b"RIFF\0\0\0\0WAVE")
            while True:
                hdr = src.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                size = min(size, size_total - src.tell())  # bozuk/akış boyutları
                if cid in _WAV_KEEP:
                    dst.write(cid + struct.pack("<I", size))
                    left = size
                    while left:
                        buf = src.read(min(left, 1 << 20))
                        if not buf:
                            break
                        dst.write(buf)
                        left -= len(buf)
                    if size % 2:
                        dst.write(b"\0")
                        src.read(1)
                else:
                    body = src.read(size)
                    if size % 2:
                        src.read(1)
                    name = cid.decode("latin-1").strip()
                    if cid == b"LIST" and body[:4] == b"INFO":
                        subs, pos = [], 4
                        while pos + 8 <= len(body):
                            sid = body[pos:pos + 4].decode("latin-1")
                            slen = struct.unpack("<I", body[pos + 4:pos + 8])[0]
                            subs.append(sid)
                            pos += 8 + slen + (slen % 2)
                        name = "LIST/INFO: " + ", ".join(subs) if subs else "LIST/INFO"
                    removed.append(name)
            end = dst.tell()
            dst.seek(4)
            dst.write(struct.pack("<I", end - 8))

        result.status = "success"
        result.cleaned_path = output
        result.removed_fields = removed or ["(Tag bulunamadı)"]
    except Exception as exc:
        output.unlink(missing_ok=True)
        result.status = "error"
        result.error_msg = str(exc)
    return result


def _clean_mutagen(path: Path) -> CleanResult:
    """MP3 / FLAC / OGG / M4A / AAC için mutagen tabanlı temizleyici."""
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        # copyfile: dosya sistemi xattr/ek metadata'sı çıktıya taşınmaz
        shutil.copyfile(path, output)  # önce kopyala, kopyayı değiştir

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
    elif ext == ".wav":
        return _clean_wav(path)
    elif ext in SUPPORTED_VIDEO:
        return _clean_ffmpeg(path)
    elif ext in SUPPORTED_AUDIO:
        return _clean_mutagen(path)
    else:
        return CleanResult(original_path=path, status="skipped", error_msg="Desteklenmeyen format")
