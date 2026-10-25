# KursiMu — Desktop Relaxation Monitoring

Aplikasi desktop **DearPyGui** untuk prototipe KursiMu (Windows 11): membandingkan tekanan darah **sebelum** dan **sesudah** relaksasi musik sambil mencatat sensor pijakan Arduino.

## GUI KursiMu — Tampilan dan musik
- Tema putih–hijau, **fullscreen dan always-on-top**, tombol tutup di header.
- Toolbar lebar penuh: **Mulai** hijau, **Pause/Lanjutkan** amber, **Selesai & Simpan** merah, **Pilih Musik** biru.
- Panel **Pijakan Sensor**: pembacaan level, grafik tren real-time, rerata dan jumlah sampel.
- Panel **BP KursiMu AWAL**: kartu warna untuk SYS, DIA, HR, PP, dan estimasi MAP.
- Panel **BP KursiMu AKHIR**: lima parameter yang sama. Delta (AKHIR − AWAL) berada pada panel terpisah sebelum logo.
- BLE ESP32-C3 V13.2: UUID tetap, CRC16, auto-reconnect, ACK_RESULT + penyimpanan hasil sebelum ACK. Tidak otomatis mengirim START saat reconnect.
- Sesi: sebelum relaksasi ukur AWAL; **Mulai** menyalakan satu lagu relaksasi dan mencatat CSV pijakan; Pause/Lanjutkan menghentikan sementara; ukur AKHIR; Selesai menyimpan ringkasan CSV.
- Area logo horizontal bawah di `logos/` (gambar opsional; dapat ditambahkan secara lokal).

## V2.8 — Alur sesi ketat (safety rule)

Semua tombol dan callback mematuhi urutan yang sama:

1. **IDLE:** hubungkan BP KursiMu (BLE) **dan** Pijakan Sensor (Arduino serial). Keduanya wajib terkoneksi.
2. Isi **Nama** dan **ID Pengguna**. Selama kosong, tombol UKUR AWAL tidak aktif.
3. **UKUR AWAL:** baru bisa dijalankan dengan identitas lengkap dan kedua perangkat terkoneksi. Nama/ID kemudian terkunci agar tidak tertukar.
4. **MULAI:** baru tersedia setelah BP AWAL tersimpan dan musik tersedia. Musik diputar serta CSV pijakan dimulai.
5. **PAUSE / LANJUTKAN:** menghentikan sementara/melanjutkan musik, timer, dan pencatatan. Jika BP atau Pijakan terputus saat sesi berjalan, aplikasi **pause otomatis**; LANJUTKAN memerlukan koneksi kembali. Jika port COM Arduino berubah setelah reconnect, port dapat dipilih ulang saat **PAUSED**.
6. **UKUR AKHIR:** baru tersedia saat relaksasi RUNNING/PAUSED, koneksi BP/Pijakan siap, dan AWAL sudah tercatat.
7. **SELESAI:** baru tersedia setelah BP AKHIR diterima; CSV pijakan ditutup dan CSV ringkasan lengkap ditulis. Jika simpan ringkasan gagal, tombol ini bisa dicoba kembali.
8. **SESI BARU:** baru tersedia setelah penyimpanan sukses, lalu identitas pengguna dan panel BP dibersihkan.

**PILIH MUSIK** hanya aktif pada kondisi IDLE, tidak selama sesi atau saat perintah pengukuran BP sedang berlangsung.

**Warna status:** merah = belum siap/terputus/gagal simpan; kuning = menunggu tahap berikutnya/PAUSE/BP sedang mengukur; hijau = siap relaksasi, sesi aktif, siap disimpan, atau selesai.

**Keluar darurat:** karena aplikasi fullscreen, saat sesi aktif ada pilihan eksplisit **KELUAR DARURAT**. Ini mempertahankan data CSV pijakan yang sudah direkam dan menulis penanda `*_BELUM_SELESAI.json`, tetapi **tidak** membuat ringkasan penelitian seolah-olah BP AWAL/AKHIR lengkap.

Pemeriksaan otomatis: `python -m unittest discover -s tests -p "test_session_rules.py" -v` (tanpa hardware) dan CI Python syntax pada setiap push.

## Instalasi Windows 11
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python kursimu_gui_v2.py --demo
```
Setelah demo, jalankan perangkat asli dengan `python kursimu_gui_v2.py`. Jangan menjalankan BLE CLI dan GUI bersamaan.

**Musik:** letakkan satu berkas MP3/WAV/OGG bernama apa saja dalam folder `music/` (contoh: `meditation-relaxing-music-293922.mp3`). Saat startup aplikasi akan menemukannya secara otomatis. Kalau ada lebih dari satu lagu, `relaksasi.mp3` diprioritaskan, lalu MP3 lain. Tombol **Pilih Musik** membuka folder `music/` secara default dan bisa memilih lokasi lain. Pilihan manual disimpan lokal di `kursimu_data/ui_settings.json`, bukan di GitHub. Instal `pygame-ce` (bukan `pygame`) untuk audio.

Jika MP3 belum terbaca, jalankan diagnostik tanpa GUI:

```powershell
python kursimu_gui_v2.py --test-music
```

Atau cek file tertentu:

```powershell
python kursimu_gui_v2.py --test-music --music-file "C:\\Users\\Public\\Music\\relaksasi.mp3"
```

Jika ditemukan tetapi gagal dimuat, terminal menampilkan error decoder/audio. Pastikan unduhan benar-benar file audio, bukan halaman HTML hasil unduh yang diberi ekstensi MP3.

### Memilih MP3 di KursiMu (V2.7)

1. Buka tombol **PILIH MUSIK**. Dialognya sekarang putih–hijau, bukan penjelajah gelap DearPyGui.
2. Jika ada MP3 dalam `kursimu-ui/music/` (termasuk subfolder), pilih dari daftar lalu tekan **GUNAKAN MUSIK**.
3. Jika file masih ada di **Downloads**, **Desktop**, atau lokasi lain, tekan **PILIH DARI WINDOWS** dan buka file melalui dialog Windows standar. Bisa juga tempel path absolut file audio di kolom lokasi.
4. Aplikasi akan mengingat path yang dipilih di `kursimu_data/ui_settings.json`. Saat **MULAI**, lagu tersebut diputar.

**Catatan penting:** Repository hanya menyimpan `music/README.md`, bukan MP3, karena file musik dikecualikan oleh `.gitignore`. `git pull` tidak mengunduh lagu. Pastikan filenya benar-benar ada di **folder `music` pada clone yang sedang dijalankan**, atau pilih dari komputer dengan tombol Windows.

Diagnostik:

```powershell
python kursimu_gui_v2.py --test-music
python kursimu_gui_v2.py --test-music --music-file "C:\\Users\\LENOVO\\Downloads\\contoh-musik.mp3"
```

**Sensor pijakan:** Arduino Serial 115200 baud; kirim integer `0..100` atau `LEVEL:65`; pilih port COM pada GUI.

**BP:** XIAO ESP32-C3 dengan firmware BLE KursiMu V13.2. Tidak ada perubahan di firmware.

## Data & keamanan
Hasil CSV disimpan **secara lokal** dalam `kursimu_data/sessions/YYYY-MM-DD/`, bukan ke GitHub. Jangan commit CSV, data pasien, musik berlisensi, atau konfigurasi komputer.

PP = SYS − DIA (mmHg). MAP ≈ DIA + PP / 3 (estimasi, bukan hasil pengukuran langsung). Delta = AKHIR − AWAL; ini bukan penilaian klinis otomatis.

Alat adalah **prototipe penelitian**, bukan pengganti alat medis tervalidasi. Pastikan mekanisme toggle dan penghentian pompa aman sebelum digunakan pada manusia.
