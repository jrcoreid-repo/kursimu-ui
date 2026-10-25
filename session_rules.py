"""Pure KursiMu session rules (no GUI or hardware dependencies).

The GUI must enforce these rules both in button states and action callbacks.
Colors indicate workflow readiness, never BP clinical risk.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionDecision:
    can_measure_awal: bool
    can_start: bool
    can_pause: bool
    can_resume: bool
    can_measure_akhir: bool
    can_finish: bool
    can_new: bool
    can_change_music: bool
    can_edit_identity: bool
    phase: str
    guidance: str
    color: str


def requires_exit_confirmation(state: str, pending_measurement: bool = False) -> bool:
    """Ask before leaving an active/incomplete session or in-flight BP reading.

    After SESI BARU resets the phase to IDLE, quitting is immediate.
    """
    return pending_measurement or state in ("RUNNING", "PAUSED", "SAVE_FAILED")


def evaluate_session(
    *,
    state: str,
    bp_connected: bool,
    foot_connected: bool,
    bp_ready: bool,
    identity_ready: bool,
    initial_ready: bool,
    final_ready: bool,
    measuring: bool,
    bp_busy: bool = False,
    music_ready: bool = False,
) -> SessionDecision:
    """Return UI permissions AND the reason for the current phase.

    The initial reading precedes music, and the final reading precedes save.
    Connection is not required to save already acquired final measurements.
    """
    devices = bp_connected and bp_ready  # Foot COM is optional; BP is mandatory.
    free = not measuring and not bp_busy
    idle = state == "IDLE"
    active = state in ("RUNNING", "PAUSED")
    can_awal = idle and devices and identity_ready and not initial_ready and free
    can_start = idle and devices and identity_ready and initial_ready and free and music_ready
    can_pause = state == "RUNNING"
    can_resume = state == "PAUSED" and devices and free
    can_akhir = active and devices and initial_ready and not final_ready and free
    can_finish = (active or state == "SAVE_FAILED") and initial_ready and final_ready and free
    can_new = state == "FINISHED" and free
    can_music = idle and not measuring and not bp_busy
    can_edit = idle and not initial_ready and not measuring

    if state == "FINISHED":
        phase, guidance, color = "SELESAI", "Data tersimpan. Tekan SESI BARU untuk pengguna berikutnya.", "green"
    elif state == "SAVE_FAILED":
        phase, guidance, color = "GAGAL SIMPAN", "Ringkasan belum tersimpan. Ulangi SELESAI.", "red"
    elif state == "RUNNING" and not devices:
        phase, guidance, color = "KONEKSI TERPUTUS", "Koneksi BP terputus; sesi akan dijeda.", "red"
    elif state == "PAUSED" and not devices:
        phase, guidance, color = "MENUNGGU KONEKSI", "Sesi dijeda. Hubungkan BP KursiMu untuk melanjutkan.", "red"
    elif state == "PAUSED" and final_ready:
        phase, guidance, color = "SIAP SELESAI", "Hasil AKHIR lengkap. Tekan SELESAI & SIMPAN.", "green"
    elif state == "RUNNING" and final_ready:
        phase, guidance, color = "SIAP SELESAI", "Hasil AKHIR lengkap. Tekan SELESAI & SIMPAN.", "green"
    elif state == "PAUSED":
        phase, guidance, color = "RELAKSASI DIJEDA", "Lanjutkan sesi atau lakukan pengukuran AKHIR.", "yellow"
    elif state == "RUNNING":
        phase, guidance, color = "RELAKSASI AKTIF", "Musik dan logging aktif. Lakukan pengukuran AKHIR sebelum selesai.", "green"
    elif measuring or bp_busy:
        phase, guidance, color = "BP MENGUKUR", "Tunggu hasil pengukuran tekanan darah.", "yellow"
    elif not devices:
        phase, guidance, color = "BELUM TERHUBUNG", "Hubungkan BP KursiMu terlebih dahulu.", "red"
    elif not identity_ready:
        phase, guidance, color = "ISI DATA PENGGUNA", "Isi Nama dan ID Pengguna sebelum UKUR AWAL.", "red"
    elif not initial_ready:
        phase, guidance, color = "SIAP UKUR AWAL", "Data pengguna lengkap. Tekan UKUR AWAL.", "yellow"
    elif not music_ready:
        phase, guidance, color = "PILIH MUSIK", "BP AWAL tersimpan. Pilih musik untuk memulai relaksasi.", "yellow"
    else:
        phase, guidance, color = "SIAP RELAKSASI", "BP AWAL tersimpan. Tekan MULAI untuk relaksasi.", "green"

    return SessionDecision(
        can_measure_awal=can_awal,
        can_start=can_start,
        can_pause=can_pause,
        can_resume=can_resume,
        can_measure_akhir=can_akhir,
        can_finish=can_finish,
        can_new=can_new,
        can_change_music=can_music,
        can_edit_identity=can_edit,
        phase=phase,
        guidance=guidance,
        color=color,
    )
