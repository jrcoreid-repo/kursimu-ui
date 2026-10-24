# KursiMu — Desktop Relaxation Monitoring

Aplikasi desktop **DearPyGui** untuk prototipe KursiMu (Windows 11): membandingkan tekanan darah **sebelum** dan **sesudah** relaksasi musik sambil mencatat sensor pijakan Arduino.

## V2.1 — UI refinement
- Tema putih–hijau, **fullscreen dan always-on-top**, tombol tutup di header.
- Toolbar lebar penuh: **Mulai** hijau, **Pause/Lanjutkan** amber, **Selesai & Simpan** merah, **Pilih Musik** biru.
- Panel **Pijakan Sensor**: pembacaan level, grafik tren real-time, rerata dan jumlah sampel.
- Panel **BP KursiMu AWAL**: SYS/DIA/Pulse, PP, estimasi MAP dan bar indikator.
- Panel **BP KursiMu AKHIR**: parameter yang sama dan **delta (AKHIR − AWAL)**.
- BLE ESP32-C3 V13.2: UUID tetap, CRC16, auto-reconnect, ACK_RESULT + penyimpanan hasil sebelum ACK. Tidak otomatis mengirim START saat reconnect.
- Sesi: sebelum relaksasi ukur AWAL; **Mulai** menyalakan satu lagu relaksasi dan mencatat CSV pijakan; Pause/Lanjutkan menghentikan sementara; ukur AKHIR; Selesai menyimpan ringkasan CSV.
- Area logo horizontal bawah di `logos/` (gambar opsional; dapat ditambahkan secara lokal).

## Instalasi Windows 11
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python kursimu_gui_v2.py --demo
```
Setelah demo, jalankan perangkat asli dengan `python kursimu_gui_v2.py`. Jangan menjalankan BLE CLI dan GUI bersamaan.

**Musik:** tempatkan satu berkas `relaksasi.mp3` pada folder `music/` atau pilih melalui tombol **Pilih Musik**. Instal `pygame-ce` (bukan `pygame`) untuk audio.

**Sensor pijakan:** Arduino Serial 115200 baud; kirim integer `0..100` atau `LEVEL:65`; pilih port COM pada GUI.

**BP:** XIAO ESP32-C3 dengan firmware BLE KursiMu V13.2. Tidak ada perubahan di firmware.

## Data & keamanan
Hasil CSV disimpan **secara lokal** dalam `kursimu_data/sessions/YYYY-MM-DD/`, bukan ke GitHub. Jangan commit CSV, data pasien, musik berlisensi, atau konfigurasi komputer.

PP = SYS − DIA (mmHg). MAP ≈ DIA + PP / 3 (estimasi, bukan hasil pengukuran langsung). Delta = AKHIR − AWAL; ini bukan penilaian klinis otomatis.

Alat adalah **prototipe penelitian**, bukan pengganti alat medis tervalidasi. Pastikan mekanisme toggle dan penghentian pompa aman sebelum digunakan pada manusia.
