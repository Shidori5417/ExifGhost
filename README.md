# 👻 ExifGhost

**Fotoğraf, ses ve video dosyalarındaki tüm metadata'yı tek tıkla temizleyen, modern masaüstü uygulaması.**

> İşlem tamamen yereldir — hiçbir veri dışarı çıkmaz.

---

## Özellikler

| Kategori | Desteklenen Formatlar |
|---|---|
| 🖼 Görsel | JPG, JPEG, PNG |
| 🎵 Ses | MP3, FLAC, OGG, M4A, WAV |
| 🎬 Video | MP4, MOV, M4V |

- **Sürükle-bırak** veya dosya seçici ile dosya yükleme
- Temizlenen dosyalar orijinalin yanına `_cleaned` son ekiyle kaydedilir
- Orijinal dosyalar **asla değiştirilmez**
- Gerçek zamanlı ilerleme takibi
- Açık/Koyu tema desteği

---

## Kurulum

```bash
# 1. Repo klonla
git clone https://github.com/kullanicin/ExifGhost.git
cd ExifGhost

# 2. Sanal ortam oluştur
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS

# 3. Bağımlılıkları yükle
pip install -r requirements.txt

# 4. Çalıştır
python main.py
```

---

## Bağımlılıklar

```
customtkinter>=5.2.2   # Modern GUI
Pillow>=10.0.0         # Görsel işleme
piexif>=1.1.3          # EXIF manipülasyonu
mutagen>=1.47.0        # Ses/Video metadata
tkinterdnd2>=0.3.0     # Sürükle-bırak desteği
```

---

## Proje Yapısı

```
ExifGhost/
├── main.py          # Giriş noktası
├── gui.py           # CustomTkinter arayüzü
├── logic.py         # Metadata temizleme motoru
├── requirements.txt
├── README.md
└── AGENTS.md
```

---

## Lisans

MIT — Özgürce kullan, değiştir, dağıt.
