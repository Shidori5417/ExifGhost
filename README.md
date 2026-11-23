# 👻 ExifGhost

[![CI](https://github.com/Shidori5417/ExifGhost/actions/workflows/ci.yml/badge.svg)](https://github.com/Shidori5417/ExifGhost/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Fotoğraf, ses ve video dosyalarındaki metadata'yı tek tıkla temizleyen masaüstü uygulaması. İşlem tamamen yereldir, hiçbir veri dışarı çıkmaz.

| Dosyalar eklendi | Temizleme bitti |
|---|---|
| ![Dosya listesi](docs/screenshots/files.png) | ![Temizleme tamamlandı](docs/screenshots/cleaned.png) |

## Özellikler

- Sürükle-bırak veya dosya seçici ile çoklu dosya ekleme
- Temiz kopya orijinalin yanına `_cleaned` son ekiyle kaydedilir, orijinal dosya değiştirilmez. Aynı adlı bir `_cleaned` dosyası zaten varsa üzerine yazılmaz; `_cleaned (2)`, `_cleaned (3)` gibi numaralandırılır
- Dosya başına durum ve genel ilerleme çubuğu (arka plan iş parçacığında çalışır); hata ve atlanan dosyaların nedeni bitiş özetinde gösterilir
- Açık / koyu tema

## Neyi siler, neyi silmez?

| Tür | Formatlar | Yöntem | Silinenler | Korunanlar |
|---|---|---|---|---|
| Görsel | JPG, JPEG | **Kayıpsız** marker temizliği: JPEG segment akışı yeniden yazılır, piksel (entropi) verisi birebir kopyalanır; yön != 1 ise `jpegtran` ile kayıpsız döndürme, olmazsa yeniden kodlama (aşağıya bakın) | EXIF (GPS dahil), XMP, IPTC/Photoshop (APP13), COM yorum, MPF ve diğer APPn segmentleri, JFIF içi küçük resim, EOI sonrası ek veri | Tarama/kuantalama/Huffman verisi (aşamalı JPEG dahil), JFIF (APP0), ICC renk profili (APP2), Adobe APP14 (renk çözümü için); EXIF yönü piksellere uygulanır, görüntü yan dönmez |
| Görsel | PNG (APNG dahil) | Pillow ile yeniden kayıt | tEXt/zTXt/iTXt (XMP dahil), eXIf, dpi vb. | ICC renk profili, saydamlık; animasyonlu PNG'de tüm kareler, süreler ve döngü sayısı |
| Ses | MP3, FLAC, OGG, M4A, AAC | mutagen ile etiketlerin silinmesi | ID3/Vorbis/MP4 etiketleri; gömülü kapak görselleri (MP4 `covr`, FLAC PICTURE blokları, OGG `METADATA_BLOCK_PICTURE`) | Ses verisi (yeniden kodlanmaz) |
| Ses | WAV | RIFF dosyası yeniden yazılır | `LIST/INFO`, `id3 `, `bext`, `iXML`, XMP vb. tüm ek chunk'lar | Yalnızca `fmt `, `fact`, `data`; ses verisi birebir |
| Video | MP4, MOV, M4V | **ffmpeg** ile akış kopyalama (`-c copy`) | Konteyner ve akış etiketleri (GPS `location` dahil), bölümler, kapak görseli, zamanlı veri akışları | Video/ses/altyazı akışları (yeniden kodlanmaz, kalite kaybı olmaz) |

Önemli notlar:

- **JPEG kayıpsız temizlenir:** EXIF Orientation yoksa ya da 1 ise dosya yeniden sıkıştırılmaz; yalnızca marker segmentleri atılır ve görüntü verisi baytı baytına korunur (çözülmüş pikseller özdeştir). Orientation 1'den farklıysa `jpegtran` (libjpeg-turbo-progs) PATH'te ise `-perfect` ile kayıpsız döndürülür. `jpegtran` yoksa ya da görüntü boyutları MCU'ya tam oturmuyorsa (kayıpsız döndürme mümkün değilse) pikseller döndürülüp kaynağın kalite tablolarıyla (`quality="keep"`) **yeniden kodlanır**; bu durum sonuçta ve bitiş özetinde "yeniden kodlandı (kayıplı)" olarak bildirilir. PNG zaten kayıpsızdır.
- **Kapak görselleri:** M4A (`covr`), FLAC (PICTURE) ve OGG (`METADATA_BLOCK_PICTURE`) içindeki gömülü resimler testlerle doğrulanarak silinir. FLAC PICTURE blokları mutagen'in `delete()` çağrısıyla gitmediği için ayrıca temizlenir.
- **ICC renk profili kasıtlı olarak korunur;** silinirse renkler değişir. Profil kişisel bilgi taşımaz, ancak çok nadir de olsa cihaz/yazılım adı içerebilir.
- **Video ve ffmpeg:** MP4/MOV/M4V temizliği için `ffmpeg` (ve raporlama için `ffprobe`) PATH'te kurulu olmalıdır. Kurulu değilse bu dosyalar "hata" olarak işaretlenir ve çıktı üretilmez; başarı bildirilmez. Görsel, MP3/FLAC/OGG/M4A/AAC ve WAV için ffmpeg gerekmez.
- Çıktı yeni bir dosya olarak yazılır: dosya sistemi xattr'ları ve eski tarihler taşınmaz. **Dosya adının kendisi** kişisel bilgi içeriyorsa onu elle değiştirmelisiniz.
- Görüntü pikselleri, ses/video içeriği ve gömülü filigranlar (steganografi, görünür damga vb.) bu araçla temizlenmez.

## Kurulum

Python 3.10+ ve Tk gerekir (bazı Linux dağıtımlarında ayrıca `tk` paketi).

```bash
git clone https://github.com/Shidori5417/ExifGhost.git
cd ExifGhost
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Video temizliği için ffmpeg (isteğe bağlı bağımlılık): `sudo apt install ffmpeg`, `brew install ffmpeg` veya Windows'ta `winget install ffmpeg`.

## Test

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

Mantık testleri ekran gerektirmez. ffmpeg kurulu değilse video, `jpegtran` kurulu değilse kayıpsız döndürme testleri atlanır. GUI duman testi (`tests/test_gui_smoke.py`) bir ekran ister; yerelde ekran yoksa atlanır, CI'da `xvfb-run -a pytest` ile çalışır.

## Teknoloji ve yapı

`customtkinter` (arayüz), `tkinterdnd2` (sürükle-bırak), `Pillow` + `piexif` (görsel), `mutagen` (ses), `ffmpeg` (video, isteğe bağlı).

```
main.py    # giriş noktası
gui.py     # CustomTkinter arayüzü
logic.py   # format bazlı temizleyiciler (clean_file)
tests/     # pytest testleri
```

## Lisans

[MIT](LICENSE)
