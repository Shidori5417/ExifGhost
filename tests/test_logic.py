"""logic.py testleri — ekran gerektirmez (GUI modülü import edilmez)."""
from __future__ import annotations

import base64
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import piexif
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis
from mutagen.mp3 import MP3
from PIL import Image, ImageChops, ImageOps, ImageSequence

import logic
from logic import clean_file

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe kurulu değil")


MP3_FRAMES = (b"\xff\xfb\x90\x00" + b"\x00" * 413) * 10   # 128 kbps / 44.1 kHz


def sha(path: Path) -> bytes:
    return path.read_bytes()


def make_jpeg(path: Path, orientation: int | None = None, comment: bytes | None = None,
              gps: bool = False) -> None:
    img = Image.new("RGB", (40, 20), (255, 0, 0))     # sol yarı kırmızı
    img.paste((0, 0, 255), (20, 0, 40, 20))           # sağ yarı mavi
    zeroth = {piexif.ImageIFD.Make: b"SecretCam"}
    if orientation:
        zeroth[piexif.ImageIFD.Orientation] = orientation
    exif = {"0th": zeroth, "Exif": {}, "GPS": {}, "1st": {}}
    if gps:
        exif["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N",
                       piexif.GPSIFD.GPSLatitude: ((41, 1), (0, 1), (0, 1))}
    kw = {"comment": comment} if comment else {}
    img.save(path, "JPEG", exif=piexif.dump(exif), **kw)


# ─── JPEG ─────────────────────────────────────────────────────────────────────
def test_jpeg_exif_gps_removed_and_original_untouched(tmp_path):
    src = tmp_path / "a.jpg"
    make_jpeg(src, gps=True)
    before = sha(src)
    res = clean_file(src)
    assert res.status == "success"
    assert sha(src) == before
    with Image.open(res.cleaned_path) as out:
        assert not out.info.get("exif")
        assert out.getexif() == {} or len(out.getexif()) == 0
    assert "GPSLatitude" in res.removed_fields and "Make" in res.removed_fields


def test_jpeg_orientation_applied_to_pixels(tmp_path):
    src = tmp_path / "o.jpg"
    make_jpeg(src, orientation=6)    # 90 derece saat yönünde gösterilmeli
    res = clean_file(src)
    with Image.open(res.cleaned_path) as out:
        assert out.size == (20, 40)                      # 40x20 -> 20x40
        assert 6 != out.getexif().get(0x0112)
        top, bottom = out.getpixel((10, 2)), out.getpixel((10, 37))
    # orientation 6: orijinalin solu üste gelir -> üst kırmızı, alt mavi
    assert top[0] > 200 and top[2] < 60
    assert bottom[2] > 200 and bottom[0] < 60


def test_jpeg_comment_removed_and_reported(tmp_path):
    src = tmp_path / "c.jpg"
    make_jpeg(src, comment=b"gizli yorum")
    assert b"gizli yorum" in sha(src)
    res = clean_file(src)
    assert b"gizli yorum" not in sha(res.cleaned_path)
    assert any("COM" in f for f in res.removed_fields)


def test_jpeg_xmp_removed_and_reported(tmp_path):
    src = tmp_path / "x.jpg"
    Image.new("RGB", (8, 8)).save(src, "JPEG")
    data = src.read_bytes()
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>GIZLI_XMP</x:xmpmeta>"
    seg = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
    src.write_bytes(data[:2] + seg + data[2:])
    res = clean_file(src)
    assert res.status == "success"
    assert b"GIZLI_XMP" not in sha(res.cleaned_path)
    assert "XMP" in res.removed_fields


def test_jpeg_icc_profile_kept(tmp_path):
    from PIL import ImageCms
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "i.jpg"
    Image.new("RGB", (8, 8)).save(src, "JPEG", icc_profile=icc)
    res = clean_file(src)
    with Image.open(res.cleaned_path) as out:
        assert out.info.get("icc_profile") == icc


# ─── PNG / APNG ───────────────────────────────────────────────────────────────
def test_png_text_removed(tmp_path):
    from PIL.PngImagePlugin import PngInfo
    src = tmp_path / "t.png"
    info = PngInfo()
    info.add_text("Author", "GIZLI_YAZAR")
    Image.new("RGB", (8, 8), (1, 2, 3)).save(src, pnginfo=info)
    assert b"GIZLI_YAZAR" in sha(src)
    res = clean_file(src)
    assert b"GIZLI_YAZAR" not in sha(res.cleaned_path)
    assert "Author" in res.removed_fields
    with Image.open(res.cleaned_path) as out:
        assert out.getpixel((0, 0)) == (1, 2, 3)


def test_apng_frames_and_timing_preserved(tmp_path):
    from PIL.PngImagePlugin import PngInfo
    colors = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
    frames = [Image.new("RGBA", (10, 10), c) for c in colors]
    src = tmp_path / "anim.png"
    info = PngInfo()
    info.add_text("Comment", "GIZLI_APNG")
    frames[0].save(src, save_all=True, append_images=frames[1:],
                   duration=[100, 200, 300], loop=3, pnginfo=info)
    res = clean_file(src)
    assert res.status == "success"
    assert b"GIZLI_APNG" not in sha(res.cleaned_path)
    with Image.open(res.cleaned_path) as out:
        assert out.n_frames == 3
        assert out.info.get("loop") == 3
        got = [(f.info.get("duration"), f.convert("RGBA").getpixel((0, 0)))
               for f in ImageSequence.Iterator(out)]
    assert got == [(100, colors[0]), (200, colors[1]), (300, colors[2])]


def test_png_transparency_kept(tmp_path):
    src = tmp_path / "p.png"
    Image.new("RGBA", (4, 4), (10, 20, 30, 0)).save(src)
    res = clean_file(src)
    with Image.open(res.cleaned_path) as out:
        assert out.convert("RGBA").getpixel((0, 0))[3] == 0


# ─── Çıktı adı / kopya ────────────────────────────────────────────────────────
def test_existing_cleaned_not_overwritten(tmp_path):
    src = tmp_path / "a.png"
    Image.new("RGB", (4, 4)).save(src)
    existing = tmp_path / "a_cleaned.png"
    existing.write_bytes(b"benim dosyam")
    res = clean_file(src)
    assert existing.read_bytes() == b"benim dosyam"
    assert res.cleaned_path.name == "a_cleaned (2).png"
    res2 = clean_file(src)
    assert res2.cleaned_path.name == "a_cleaned (3).png"


def test_mutagen_copy_does_not_use_copy2(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("copy2 kullanılmamalı (xattr taşır)")
    monkeypatch.setattr(shutil, "copy2", boom)
    src = tmp_path / "s.mp3"
    src.write_bytes(MP3_FRAMES)
    res = clean_file(src)
    assert res.status == "success" and res.cleaned_path != src


def test_mp3_id3_removed(tmp_path):
    src = tmp_path / "m.mp3"
    src.write_bytes(MP3_FRAMES)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="GIZLI_BASLIK"))
    tags.save(src)
    assert b"GIZLI_BASLIK" in sha(src)
    before = sha(src)
    res = clean_file(src)
    assert res.status == "success"
    assert sha(src) == before
    assert b"GIZLI_BASLIK" not in sha(res.cleaned_path)
    assert MP3(res.cleaned_path).tags is None


def test_unsupported_and_missing(tmp_path):
    assert clean_file(tmp_path / "yok.jpg").status == "error"
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert clean_file(f).status == "skipped"


# ─── WAV ──────────────────────────────────────────────────────────────────────
def make_wav_with_info(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 801)       # tek sayıda bayt değil, ama çift
    data = path.read_bytes()
    info = b"INFO" + b"INAM" + struct.pack("<I", 12) + b"GIZLI_BASLIK\x00"[:12] \
        + b"ICMT" + struct.pack("<I", 4) + b"yeni"
    lst = b"LIST" + struct.pack("<I", len(info)) + info
    id3 = b"id3 " + struct.pack("<I", 5) + b"GIZLI" + b"\x00"   # tek boyut + pad
    body = data[4 + 4:] + lst + id3
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 0) + body)
    raw = bytearray(path.read_bytes())
    raw[4:8] = struct.pack("<I", len(raw) - 8)
    path.write_bytes(bytes(raw))


def test_wav_info_and_id3_chunks_removed(tmp_path):
    src = tmp_path / "w.wav"
    make_wav_with_info(src)
    assert b"GIZLI" in sha(src)
    before = sha(src)
    res = clean_file(src)
    assert res.status == "success"
    assert sha(src) == before
    out = sha(res.cleaned_path)
    assert b"GIZLI" not in out and b"LIST" not in out and b"id3 " not in out
    assert any("INAM" in f for f in res.removed_fields)
    assert any("id3" in f for f in res.removed_fields)
    assert "(Tag zaten boş)" not in res.removed_fields
    with wave.open(str(res.cleaned_path)) as w, wave.open(str(src)) as orig:
        assert w.getparams() == orig.getparams()
        assert w.readframes(10**6) == orig.readframes(10**6)
    # RIFF boyutu tutarlı
    assert struct.unpack("<I", out[4:8])[0] == len(out) - 8


def test_wav_without_tags_reported_empty(tmp_path):
    src = tmp_path / "e.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 100)
    res = clean_file(src)
    assert res.status == "success"
    assert res.removed_fields == ["(Tag bulunamadı)"]


def test_wav_invalid_is_error_and_no_output(tmp_path):
    src = tmp_path / "bad.wav"
    src.write_bytes(b"not a wav at all")
    res = clean_file(src)
    assert res.status == "error"
    assert not list(tmp_path.glob("*_cleaned*"))


# ─── MP4 / MOV (ffmpeg) ───────────────────────────────────────────────────────
def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def make_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-metadata", "location=+41.0082+028.9784/",
         "-metadata", "com.apple.quicktime.location.ISO6709=+41.0082+028.9784/",
         "-metadata", "title=GIZLI_BASLIK",
         "-metadata:s:v:0", "comment=GIZLI_AKIS",
         "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path)],
        check=True)


def all_tags(info: dict) -> dict:
    tags = dict(info["format"].get("tags", {}))
    for st in info["streams"]:
        for k, v in st.get("tags", {}).items():
            tags[f"s{st['index']}:{k}"] = v
    return tags


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp4", ".mov"])
def test_video_location_and_tags_removed(tmp_path, ext):
    src = tmp_path / f"v{ext}"
    make_video(src)
    raw = sha(src)
    assert b"GIZLI_BASLIK" in raw
    assert b"loci" in raw or b"41.0082" in raw      # mp4: loci atomu, mov: \xa9xyz
    tags_before = all_tags(probe(src))
    assert any("location" in k.lower() for k in tags_before)

    res = clean_file(src)
    assert res.status == "success", res.error_msg
    assert sha(src) == raw
    out = sha(res.cleaned_path)
    assert b"GIZLI_BASLIK" not in out
    assert b"41.0082" not in out and b"loci" not in out and b"\xa9xyz" not in out
    leftover = {k: v for k, v in all_tags(probe(res.cleaned_path)).items()
                if not k.endswith(("handler_name", "language", "vendor_id", "major_brand",
                                  "minor_version", "compatible_brands"))}
    assert leftover == {}
    info = probe(res.cleaned_path)
    assert sorted(s["codec_type"] for s in info["streams"]) == ["audio", "video"]
    assert any("location" in f.lower() for f in res.removed_fields)


@needs_ffmpeg
def test_video_without_ffmpeg_fails_clearly(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    make_video(src)
    monkeypatch.setattr(logic, "_ffmpeg", lambda: None)
    res = clean_file(src)
    assert res.status == "error"
    assert "ffmpeg" in res.error_msg
    assert res.cleaned_path is None
    assert not list(tmp_path.glob("*_cleaned*"))


# ─── Kayıpsız JPEG temizliği ──────────────────────────────────────────────────
needs_jpegtran = pytest.mark.skipif(not shutil.which("jpegtran"), reason="jpegtran kurulu değil")


def noisy_image(size=(64, 48), seed=1) -> Image.Image:
    import random
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(size[0] * size[1])])
    return img


def inject(path: Path, *segments: tuple[int, bytes]) -> None:
    data = path.read_bytes()
    segs = b"".join(b"\xff" + bytes([m]) + struct.pack(">H", len(p) + 2) + p for m, p in segments)
    path.write_bytes(data[:2] + segs + data[2:])


def pixels(path: Path) -> tuple:
    with Image.open(path) as im:
        im.load()
        return im.mode, im.size, im.tobytes()


def make_dirty_jpeg(path: Path, progressive: bool, icc: bytes | None = None) -> None:
    kw = {"progressive": True} if progressive else {}
    if icc:
        kw["icc_profile"] = icc
    noisy_image().save(path, "JPEG", quality=80, comment=b"GIZLI_COM",
                       exif=piexif.dump({"0th": {piexif.ImageIFD.Make: b"GIZLI_EXIF"}}), **kw)
    inject(path,
           (0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>GIZLI_XMP</x:xmpmeta>"),
           (0xED, b"Photoshop 3.0\x008BIM\x04\x04GIZLI_IPTC"),
           (0xE5, b"GIZLI_APP5"))


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_lossless_pixels_identical_and_metadata_gone(tmp_path, progressive):
    src = tmp_path / "p.jpg"
    make_dirty_jpeg(src, progressive)
    res = clean_file(src)
    assert res.status == "success" and not res.reencoded
    out = res.cleaned_path.read_bytes()
    for secret in (b"GIZLI_COM", b"GIZLI_EXIF", b"GIZLI_XMP", b"GIZLI_IPTC", b"GIZLI_APP5", b"Exif"):
        assert secret not in out
    assert pixels(res.cleaned_path) == pixels(src)          # piksel-piksel aynı
    with Image.open(res.cleaned_path) as im:
        assert bool(im.info.get("progressive") or im.info.get("progression")) == progressive
    # entropi verisi bayt bayt aynı: çıktı yalnızca orijinalin alt dizisi olmalı
    assert len(out) < len(src.read_bytes())
    for f in ("XMP", "IPTC/Photoshop", "COM (yorum)", "Make"):
        assert f in res.removed_fields


def test_jpeg_lossless_keeps_icc_jfif_and_adobe(tmp_path):
    from PIL import ImageCms
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "k.jpg"
    make_dirty_jpeg(src, False, icc=icc)
    inject(src, (0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01"))
    res = clean_file(src)
    out = res.cleaned_path.read_bytes()
    assert b"JFIF\x00" in out and b"Adobe" in out
    with Image.open(res.cleaned_path) as im:
        assert im.info.get("icc_profile") == icc
    assert pixels(res.cleaned_path) == pixels(src)


def test_strip_jpeg_segments_parsing_edge_cases():
    # dolgu 0xFF'ler, FF00 / RSTn entropi içinde, EOI sonrası çöp
    import io
    buf = io.BytesIO()
    noisy_image((48, 32)).save(buf, "JPEG", restart_marker_blocks=1)
    data = buf.getvalue()
    assert b"\xff\xd0" in data                               # RST işaretleri gerçekten var
    padded = data[:2] + b"\xff\xff\xff" + data[2:] + b"GARBAGE" + b"\xff\xd8" + b"EKLENTI"
    clean, dropped = logic.strip_jpeg_segments(padded)
    assert clean.endswith(b"\xff\xd9") and b"GARBAGE" not in clean and b"EKLENTI" not in clean
    assert "EOI sonrası veri" in dropped
    a, b = io.BytesIO(data), io.BytesIO(clean)
    with Image.open(a) as ia, Image.open(b) as ib:
        assert ia.tobytes() == ib.tobytes()


@pytest.mark.parametrize("bad", [b"", b"\x89PNG", b"\xff\xd8", b"\xff\xd8\xff\xe1\x00\x02",
                                 b"\xff\xd8\xff\xe1\xff\xff\x00"])
def test_strip_jpeg_segments_rejects_corrupt(bad):
    with pytest.raises(ValueError):
        logic.strip_jpeg_segments(bad)


def test_corrupt_jpeg_gives_error_and_no_output(tmp_path):
    src = tmp_path / "bad.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe1\xff\xff\x00")
    res = clean_file(src)
    assert res.status == "error" and not list(tmp_path.glob("*_cleaned*"))


def test_jfif_thumbnail_dropped(tmp_path):
    src = tmp_path / "t.jpg"
    Image.new("RGB", (16, 16)).save(src, "JPEG")
    data = src.read_bytes()
    assert data[2:4] == b"\xff\xe0"
    seg_len = struct.unpack(">H", data[4:6])[0]
    jfif = bytearray(data[6:4 + seg_len])
    jfif[12:14] = b"\x02\x02"
    jfif += b"THUMBNAIL_SECRET" * 0 + b"\x01" * 12                # 2x2 RGB
    src.write_bytes(data[:2] + b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + bytes(jfif)
                    + data[4 + seg_len:])
    res = clean_file(src)
    out = res.cleaned_path.read_bytes()
    assert b"\x01" * 12 not in out[:40]
    assert out[4 + 2 + 12:4 + 2 + 14] == b"\x00\x00"


@needs_jpegtran
@pytest.mark.parametrize("orientation", range(2, 9))
def test_jpeg_orientation_lossless_via_jpegtran(tmp_path, orientation):
    src = tmp_path / "o.jpg"
    # 32x16, 4:4:4 -> MCU 8x8'e tam oturur, -perfect geçer
    img = Image.new("RGB", (32, 16), (255, 0, 0))
    img.paste((0, 0, 255), (16, 0, 32, 8))
    img.paste((0, 255, 0), (0, 8, 8, 16))
    img.save(src, "JPEG", quality=95, subsampling=0,
             exif=piexif.dump({"0th": {piexif.ImageIFD.Orientation: orientation}}))
    res = clean_file(src)
    assert res.status == "success" and not res.reencoded
    with Image.open(src) as a, Image.open(res.cleaned_path) as b:
        expected = ImageOps.exif_transpose(a)
        assert b.size == expected.size
        assert not b.getexif().get(0x0112)
        delta = ImageChops.difference(expected.convert("RGB"), b.convert("RGB"))
        diff = max(hi for _, hi in delta.getextrema())
    assert diff <= 3


def test_jpeg_orientation_fallback_reencodes_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(logic, "_jpegtran", lambda: None)
    src = tmp_path / "f.jpg"
    make_jpeg(src, orientation=6)
    res = clean_file(src)
    assert res.status == "success" and res.reencoded
    assert any("yeniden kodlan" in n for n in res.notes)
    with Image.open(res.cleaned_path) as out:
        assert out.size == (20, 40)
        assert out.getpixel((10, 2))[0] > 200 and out.getpixel((10, 37))[2] > 200
    # kalite tabloları kaynaktan alınmış olmalı
    with Image.open(src) as a, Image.open(res.cleaned_path) as b:
        assert a.quantization == b.quantization


@needs_jpegtran
def test_jpeg_orientation_non_mcu_falls_back_when_perfect_fails(tmp_path):
    src = tmp_path / "n.jpg"
    make_jpeg(src, orientation=6)       # 40x20, 4:2:0 -> -perfect başarısız
    res = clean_file(src)
    assert res.reencoded and res.cleaned_path.exists()


def test_jpeg_orientation_1_is_lossless_path(tmp_path):
    src = tmp_path / "one.jpg"
    make_jpeg(src, orientation=1)
    res = clean_file(src)
    assert not res.reencoded and not res.notes
    assert pixels(res.cleaned_path) == pixels(src)


# ─── Gömülü kapak görselleri (M4A / FLAC / OGG) ───────────────────────────────
def cover_png() -> bytes:
    import io
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Secret", "GIZLI_KAPAK")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def ffmpeg_audio(path: Path, *extra: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.3", *extra, str(path)],
                   check=True)


def flac_picture() -> Picture:
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/png", cover_png()
    pic.width = pic.height = 16
    pic.depth = 24
    return pic


@needs_ffmpeg
def test_m4a_cover_art_removed(tmp_path):
    src = tmp_path / "c.m4a"
    ffmpeg_audio(src, "-c:a", "aac")
    mp4 = MP4(src)
    mp4["\xa9nam"] = ["Baslik"]
    mp4["covr"] = [MP4Cover(cover_png(), imageformat=MP4Cover.FORMAT_PNG)]
    mp4.save()
    assert b"GIZLI_KAPAK" in sha(src)
    res = clean_file(src)
    assert res.status == "success"
    assert b"GIZLI_KAPAK" not in sha(res.cleaned_path)
    tags = MP4(res.cleaned_path).tags
    assert not tags or "covr" not in tags
    assert "covr" in res.removed_fields
    assert MP4(res.cleaned_path).info.length > 0


@needs_ffmpeg
def test_flac_cover_art_removed(tmp_path):
    src = tmp_path / "c.flac"
    ffmpeg_audio(src, "-c:a", "flac")
    fl = FLAC(src)
    fl["title"] = "Baslik"
    fl.add_picture(flac_picture())
    fl.save()
    assert b"GIZLI_KAPAK" in sha(src)
    res = clean_file(src)
    assert res.status == "success"
    assert b"GIZLI_KAPAK" not in sha(res.cleaned_path)
    assert FLAC(res.cleaned_path).pictures == []
    assert res.removed_fields and "PICTURE" in " ".join(res.removed_fields).upper()
    assert FLAC(res.cleaned_path).info.length > 0


@needs_ffmpeg
def test_ogg_cover_art_removed(tmp_path):
    src = tmp_path / "c.ogg"
    ffmpeg_audio(src, "-c:a", "libvorbis")
    og = OggVorbis(src)
    og["title"] = ["Baslik"]
    og["metadata_block_picture"] = [base64.b64encode(flac_picture().write()).decode("ascii")]
    og.save()
    assert b"metadata_block_picture" in sha(src).lower()
    res = clean_file(src)
    assert res.status == "success"
    out = OggVorbis(res.cleaned_path)
    assert not out.tags or "metadata_block_picture" not in {k.lower() for k in out.tags.keys()}
    assert b"metadata_block_picture" not in sha(res.cleaned_path).lower()
    assert out.info.length > 0
