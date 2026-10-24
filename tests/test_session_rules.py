"""State-machine safety tests for KursiMu relaxation GUI."""
import unittest

from session_rules import evaluate_session


def decide(state="IDLE", bp=True, foot=True, ready=True,
           name=True, awal=False, akhir=False, measuring=False,
           busy=False, music=True):
    return evaluate_session(
        state=state, bp_connected=bp, foot_connected=foot,
        bp_ready=ready, identity_ready=name, initial_ready=awal,
        final_ready=akhir, measuring=measuring, bp_busy=busy,
        music_ready=music,
    )


class SessionRuleTests(unittest.TestCase):
    def test_no_hardware_no_bp_start(self):
        r=decide(bp=False, foot=False)
        self.assertFalse(r.can_measure_awal)
        self.assertFalse(r.can_start)
        self.assertEqual(r.color, "red")

    def test_requires_both_connections(self):
        self.assertFalse(decide(bp=False).can_measure_awal)
        self.assertFalse(decide(foot=False).can_measure_awal)
        self.assertFalse(decide(ready=False).can_measure_awal)

    def test_requires_identity_before_first_measurement(self):
        self.assertFalse(decide(name=False).can_measure_awal)
        self.assertTrue(decide(name=True).can_measure_awal)
        self.assertFalse(decide(name=False, awal=True).can_start)

    def test_start_only_after_awal_and_music(self):
        self.assertFalse(decide().can_start)
        self.assertFalse(decide(awal=True, music=False).can_start)
        r=decide(awal=True, music=True)
        self.assertTrue(r.can_start)
        self.assertFalse(r.can_measure_awal)
        self.assertFalse(r.can_edit_identity)
        self.assertEqual(r.color,"green")

    def test_both_measurements_block_when_busy(self):
        self.assertFalse(decide(measuring=True).can_measure_awal)
        self.assertFalse(decide(busy=True).can_measure_awal)
        self.assertFalse(decide(state="RUNNING",awal=True,measuring=True).can_measure_akhir)

    def test_pause_resume_connection_gate(self):
        r=decide(state="RUNNING",awal=True)
        self.assertTrue(r.can_pause)
        self.assertFalse(r.can_finish)
        r=decide(state="PAUSED",awal=True,foot=False)
        self.assertFalse(r.can_resume)
        self.assertFalse(r.can_measure_akhir)
        self.assertEqual(r.color,"red")
        r=decide(state="PAUSED",awal=True)
        self.assertTrue(r.can_resume)
        self.assertEqual(r.color,"yellow")

    def test_akhir_only_during_relaxation(self):
        self.assertFalse(decide(state="IDLE",awal=True).can_measure_akhir)
        self.assertTrue(decide(state="RUNNING",awal=True).can_measure_akhir)
        self.assertTrue(decide(state="PAUSED",awal=True).can_measure_akhir)
        self.assertFalse(decide(state="RUNNING",awal=True,akhir=True).can_measure_akhir)

    def test_finish_requires_akhir(self):
        self.assertFalse(decide(state="RUNNING",awal=True).can_finish)
        self.assertFalse(decide(state="PAUSED",awal=True).can_finish)
        self.assertTrue(decide(state="PAUSED",awal=True,akhir=True).can_finish)
        self.assertTrue(decide(state="RUNNING",awal=True,akhir=True,foot=False).can_finish)

    def test_new_session_only_after_save(self):
        for state in ("IDLE","RUNNING","PAUSED","SAVE_FAILED"):
            self.assertFalse(decide(state=state,awal=True,akhir=True).can_new)
        self.assertTrue(decide(state="FINISHED",awal=True,akhir=True).can_new)

    def test_music_only_idle(self):
        for state in ("RUNNING","PAUSED","FINISHED","SAVE_FAILED"):
            self.assertFalse(decide(state=state).can_change_music)
        self.assertFalse(decide(measuring=True).can_change_music)
        self.assertTrue(decide().can_change_music)

    def test_retry_failed_summary(self):
        r=decide(state="SAVE_FAILED",awal=True,akhir=True)
        self.assertTrue(r.can_finish)
        self.assertFalse(r.can_new)
        self.assertEqual(r.color,"red")


if __name__ == "__main__":
    unittest.main()
