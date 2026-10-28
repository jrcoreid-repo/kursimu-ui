# Korsimu v1.0.0 Final — EXE dan installer

## Unduh hasil build dari GitHub
1. Buka **GitHub → Actions → Build KursiMu Windows installer**.
2. Pilih run terbaru berstatus **Success** di branch `main`.
3. Pada bagian **Artifacts**, unduh:
   - **Korsimu-Windows-Installer**: `Korsimu_Setup_1.0.0_Final.exe`, untuk instalasi Windows per pengguna (tanpa instal Python).
   - **Korsimu-Windows-Portable**: folder aplikasi `KursiMu`, ekstrak **seluruh** isi ZIP terlebih dahulu, lalu jalankan `KursiMu.exe`. Jangan mengambil EXE-nya saja: mode onedir membutuhkan `_internal`.
4. Saat pertama dibuka, izinkan Bluetooth Windows dan hubungkan perangkat BP sesuai alur GUI. Perangkat USB serial memakai driver COM Windows yang sesuai hardware.

## Build sendiri pada Windows
Dengan Python 3.12 terpasang, buka PowerShell dari root repo:
```powershell
git pull origin main
.\build_windows.ps1
```
Hasil: `dist\Korsimu\Korsimu.exe`. Installer memerlukan Inno Setup 6:
```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "installer\KursiMu.iss"
```
Hasil: `dist\installer\Korsimu_Setup_1.0.0_Final.exe`.

## Lokasi file dan aset
- Aplikasi installer ada di `%LOCALAPPDATA%\Programs\Korsimu`.
- Data pengukuran, CSV dan konfigurasi: `%LOCALAPPDATA%\KursiMu\kursimu_data`.
- Musik: `%LOCALAPPDATA%\KursiMu\music`.
- Logo PNG/JPG: `%LOCALAPPDATA%\KursiMu\logos`.
- Ikon Windows: `assets\kursimu.ico`, sudah dibundel.
- Aset **logo dan MP3 yang benar-benar di-commit ke repo sebelum build** akan ikut masuk paket dan disalin ke folder user pada peluncuran pertama (tidak menimpa file yang ada). Tiga logo PNG sudah dilacak di folder `logos/` dan ikut dibundel. Lagu MP3 pribadi belum dilacak di `music/`, sehingga belum otomatis ikut dalam installer. Tambahkan file berlisensi jelas sebelum membuat release jika memang ingin ikut dibundel.
- Aset musik/logo tambahan juga bisa ditaruh manual di folder user tanpa rebuild.

## Kompatibilitas
Build menggunakan runner Windows x64 dan Python 3.12. Target: Windows 10/11 **64-bit**, dengan BLE dan driver serial yang kompatibel. Windows versi lain/arsitektur lain belum divalidasi. Installer dan build **belum ditandatangani kode** sehingga SmartScreen mungkin menampilkan peringatan. Jangan menonaktifkan antivirus demi menjalankan aplikasi.

**Keamanan:** data pengguna/pasien tidak dimasukkan dalam installer, tidak dihapus saat uninstall, dan tidak otomatis diunggah ke GitHub. Uji komunikasi BP dan alur emergency exit pada PC lain sebelum penggunaan operasional.

**Kompatibilitas:** nama BLE `KURSIMU-BP-C3` serta lokasi data lama `%LOCALAPPDATA%\\KursiMu` sengaja dipertahankan. Pengguna yang sebelumnya memasang `KursiMu` dapat menghapus shortcut/program lama sesudah memastikan `Korsimu` berjalan, tanpa menghapus data sesi.
