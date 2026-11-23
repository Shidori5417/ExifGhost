"""
logic.py — ExifGhost metadata temizleme motoru
Her format için bağımsız cleaner; ortak CleanResult arayüzü.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import piexif
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
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
    reencoded:     bool = False          # True: piksel verisi yeniden sıkıştırıldı (kayıplı)
    notes:         list[str] = field(default_factory=list)


# ─── Yardımcı ─────────────────────────────────────────────────────────────────
def _out(path: Path) -> Path:
    """Çakışmayan çıktı yolu: x_cleaned.ext, doluysa x_cleaned (2).ext, ...

    Ad atomik olarak (O_CREAT|O_EXCL) rezerve edilir: eş zamanlı iki örnek aynı
    yolu alamaz. Dönen yolda boş bir dosya vardır; temizleyici üzerine yazar,
    hata olursa siler.
    """
    candidate = path.with_stem(f"{path.stem}_cleaned")
    n = 2
    while True:
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            candidate = path.with_stem(f"{path.stem}_cleaned ({n})")
            n += 1


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


# ─── JPEG: kayıpsız marker temizliği ──────────────────────────────────────────
_ORIENT_TAG = 0x0112
# EXIF Orientation -> jpegtran dönüşüm argümanları (görüntüyü dik gösterecek işlem)
_JPEGTRAN_OPS: dict[int, list[str]] = {
    2: ["-flip", "horizontal"],
    3: ["-rotate", "180"],
    4: ["-flip", "vertical"],
    5: ["-transpose"],
    6: ["-rotate", "90"],
    7: ["-transverse"],
    8: ["-rotate", "270"],
}


def _jpegtran() -> str | None:
    return shutil.which("jpegtran")


def _marker_label(m: int) -> str:
    if 0xE0 <= m <= 0xEF:
        return f"APP{m - 0xE0}"
    if m == 0xFE:
        return "COM"
    return f"0xFF{m:02X}"


def _keep_segment(m: int, payload: bytes) -> bytes | None:
    """Segmenti tutmak için (güncellenmiş) payload'ı, atmak için None döner."""
    if m == 0xE0:                                   # APP0
        if not payload.startswith(b"JFIF\0") or len(payload) < 14:
            return None                             # JFXX vb. atılır
        # JFIF içi küçük resim (orijinalin önizlemesi olabilir) atılır
        return payload[:12] + b"\0\0"
    if m == 0xE2:                                   # APP2
        return payload if payload.startswith(b"ICC_PROFILE\0") else None   # MPF vb. atılır
    if m == 0xEE:                                   # APP14
        return payload if payload.startswith(b"Adobe") else None
    if 0xE1 <= m <= 0xEF or m == 0xFE:              # APP1 (EXIF/XMP), APP13, COM, diğer APPn
        return None
    return payload                                  # DQT, SOF*, DHT, DRI, DAC, DNL ...


def strip_jpeg_segments(data: bytes) -> tuple[bytes, list[str]]:
    """JPEG marker akışını yeniden yazar; piksel (entropi) verisine dokunmaz.

    Döner: (temiz bayt dizisi, atılan marker etiketleri). Bozuk/kesik dosyada
    ValueError fırlatır.
    """
    n = len(data)
    if data[:2] != b"\xff\xd8":
        raise ValueError("Geçerli bir JPEG değil (SOI yok)")
    out = bytearray(b"\xff\xd8")
    dropped: list[str] = []
    i = 2
    while True:
        # marker ara: 0xFF, ardından (varsa) dolgu 0xFF'ler, ardından marker kodu
        if i >= n:
            raise ValueError("JPEG kesik: EOI yok")
        if data[i] != 0xFF:
            raise ValueError(f"Beklenmeyen bayt, marker bekleniyordu (ofset {i})")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            raise ValueError("JPEG kesik: EOI yok")
        m = data[i]
        i += 1
        if m == 0x00:
            raise ValueError("Geçersiz marker 0xFF00")
        if m == 0xD9:                               # EOI: sonrasındaki her şey atılır
            out += b"\xff\xd9"
            if data[i:].strip(b"\x00\xff"):
                dropped.append("EOI sonrası veri")
            return bytes(out), dropped
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:   # bağımsız markerlar (uzunluksuz)
            if m == 0xD8:
                raise ValueError("İç içe SOI")
            out += bytes((0xFF, m))
            continue
        if i + 2 > n:
            raise ValueError("JPEG kesik: segment uzunluğu okunamadı")
        length = struct.unpack(">H", data[i:i + 2])[0]
        if length < 2 or i + length > n:
            raise ValueError(f"Geçersiz segment uzunluğu ({_marker_label(m)})")
        payload = data[i + 2:i + length]
        i += length
        kept = _keep_segment(m, payload)
        if kept is None:
            dropped.append(_marker_label(m))
        else:
            out += bytes((0xFF, m)) + struct.pack(">H", len(kept) + 2) + kept
        if m == 0xDA:
            # SOS: entropi verisi bir sonraki gerçek marker'a kadar aynen kopyalanır
            # (FF00 dolgusu ve RSTn entropi verisinin parçasıdır). Aşamalı JPEG'de
            # sonraki taramalar ana döngüde DHT/SOS olarak yeniden işlenir.
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    raise ValueError("JPEG kesik: EOI yok")
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    j += 1 if nxt == 0xFF else 2
                    continue
                break
            out += data[i:j]
            i = j


def _jpeg_orientation(img: Image.Image) -> int:
    try:
        return int(img.getexif().get(_ORIENT_TAG, 1) or 1)
    except Exception:
        return 1


def _jpegtran_rotate(stripped: bytes, op: list[str]) -> bytes | None:
    """jpegtran ile kayıpsız dönüşüm. Yoksa/başarısızsa (ör. -perfect) None."""
    exe = _jpegtran()
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="exifghost-") as tmp:
        src, dst = Path(tmp) / "in.jpg", Path(tmp) / "out.jpg"
        src.write_bytes(stripped)
        proc = subprocess.run([exe, "-copy", "icc", *op, "-perfect", "-outfile", str(dst), str(src)],
                              capture_output=True)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            return None
        return dst.read_bytes()


def _reencode_upright(img: Image.Image, output: Path, icc: bytes | None) -> None:
    """Yedek yol: pikselleri döndür ve kaynağın kalite tablolarıyla yeniden kodla."""
    clean = ImageOps.exif_transpose(img)
    clean.info = {"icc_profile": icc} if icc else {}
    kw = {"icc_profile": icc} if icc else {}
    try:
        # quality="keep" yalnızca JPEG'den açılmış görüntüde çalışır; dönmüş kopyaya
        # kaynağın nicemleme tablosu ve örnekleme bilgisini aktar.
        clean.format = "JPEG"
        clean.quantization = img.quantization              # type: ignore[attr-defined]
        for attr in ("layers", "layer"):
            if hasattr(img, attr):
                setattr(clean, attr, getattr(img, attr))
        clean.save(output, format="JPEG", quality="keep", **kw)
    except Exception:
        clean.save(output, format="JPEG", quality=95, **kw)


# ─── Temizleyiciler ───────────────────────────────────────────────────────────
def _clean_jpeg(path: Path) -> CleanResult:
    result = CleanResult(original_path=path)
    output = _out(path)
    try:
        data = path.read_bytes()
        stripped, dropped = strip_jpeg_segments(data)    # bozuk dosyada erken hata

        with Image.open(path) as img:
            removed = _collect_exif_keys(img) + _collect_jpeg_extras(img)
            orientation = _jpeg_orientation(img)
            icc = img.info.get("icc_profile")
            covered = {"APP1", "APP13", "COM"}
            removed += [d for d in dropped if d not in covered]
            if "APP1" in dropped and not removed:
                removed.append("APP1")

            if orientation in _JPEGTRAN_OPS:
                rotated = _jpegtran_rotate(stripped, _JPEGTRAN_OPS[orientation])
                if rotated is not None:
                    final, _ = strip_jpeg_segments(rotated)   # jpegtran çıktısını da doğrula
                    output.write_bytes(final)
                    result.notes.append(f"Yön (Orientation={orientation}) jpegtran ile kayıpsız uygulandı")
                else:
                    _reencode_upright(img, output, icc)
                    result.reencoded = True
                    result.notes.append(
                        f"Yön (Orientation={orientation}) kayıpsız uygulanamadı (jpegtran yok veya "
                        "boyutlar MCU'ya uymuyor); görüntü yeniden kodlandı (kayıplı)")
            else:
                output.write_bytes(stripped)

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
                # default_image: ilk kare animasyonun parçası olmayan "poster"dır;
                # süreleri yalnızca gerçek animasyon karelerine ait olmalı.
                poster = bool(img.info.get("default_image"))
                frames, durations = [], []
                for i, frame in enumerate(ImageSequence.Iterator(img)):
                    if not (poster and i == 0):
                        durations.append(frame.info.get("duration", 0))
                    frames.append(frame.copy())
                img.load()
                removed = [k for k in img.info if k not in _PNG_STRUCTURAL]
                if poster:
                    save_kw["default_image"] = True
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
               "-c", "copy", "-fflags", "+bitexact", "-y",
               "-flags:v", "+bitexact", "-flags:a", "+bitexact",
               str(output)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
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
        if isinstance(audio, FLAC) and audio.pictures:
            # FLAC PICTURE blokları Vorbis yorumundan ayrıdır; delete() onlara dokunmaz
            removed += [f"PICTURE (tür {p.type}, {p.mime})" for p in audio.pictures]
            audio.clear_pictures()
            audio.save()

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
