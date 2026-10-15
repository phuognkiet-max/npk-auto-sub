import copy
import unittest

from speech_delivery import delivery_window, schedule_speech, signature_payload


class SpeechAutoScheduleTests(unittest.TestCase):
    def plans(self, rows, duration=12):
        return [delivery_window(r, {'tts_timing_mode': 'natural'}, i+1, duration)
                for i, r in enumerate(rows)]

    def test_gap_borrowing_preserves_full_duration_and_input(self):
        plans = self.plans([{'start': 0, 'end': .5, 'text': 'A'},
                            {'start': 1, 'end': 1.5, 'text': 'B'},
                            {'start': 8, 'end': 9, 'text': 'C'}])
        before = copy.deepcopy(plans)
        placed = schedule_speech(plans, [2, 2, 1], 12)
        self.assertEqual(plans, before)
        self.assertEqual(placed[0]['start'], 0)
        self.assertEqual(placed[2]['start'], 8)
        self.assertTrue(all(a['end'] <= b['start'] for a,b in zip(placed, placed[1:])))
        self.assertTrue(all(p['available'] >= d / p['scheduled_tempo'] for p,d in zip(placed, [2,2,1])))

    def test_tail_borrows_earlier_space_within_video(self):
        placed = schedule_speech(self.plans([{'start': 11, 'end': 12, 'text': 'A'}]), [3], 12)
        self.assertLess(placed[0]['start'], 11)
        self.assertAlmostEqual(placed[0]['end'], 12)

    def test_tempo_stays_bounded_and_pause_is_preserved(self):
        plans = self.plans([{'start': 0, 'end': 3, 'text': 'A', 'pause_before_ms': 200,
                            'pause_after_ms': 300}, {'start': 3, 'end': 5, 'text': 'B'}], 5)
        placed = schedule_speech(plans, [2.5, 2], 5)
        self.assertGreater(placed[0]['scheduled_tempo'], 1)
        self.assertLessEqual(placed[0]['scheduled_tempo'], 1.15)
        self.assertAlmostEqual(placed[0]['start'], .2)
        self.assertGreaterEqual(placed[1]['start'] - placed[0]['end'], .3-1e-8)
        self.assertLessEqual(placed[-1]['end'], 5+1e-8)

    def test_unknown_and_over_capacity_are_rejected(self):
        plans = self.plans([{'start': 0, 'end': 1, 'text': 'A'}])
        for duration in (None, 99):
            with self.assertRaises(ValueError):
                schedule_speech(plans, [duration], 12)

    def test_explicit_auto_policy_can_fit_above_strict_natural_limit(self):
        from speech_delivery import fit_rate
        plans = self.plans([{'start': 0, 'end': 1, 'text': 'A'}], 1)
        with self.assertRaises(ValueError):
            fit_rate(1.2, 1, 'natural', 1)
        placed = schedule_speech(plans, [1.17], 1)
        self.assertGreater(placed[0]['scheduled_tempo'], 1.15)
        self.assertLessEqual(placed[0]['scheduled_tempo'], 1.25)
        self.assertLessEqual(fit_rate(1.17, placed[0]['available'], 'natural', 1,
                                     tempo_limit=placed[0]['tempo_limit']), 1.25)

    def test_policy_changes_artifact_signature_without_changing_captions(self):
        project = {'media': {'duration': 12}, 'segments': [{'start': 0, 'end': 1, 'translated': 'A'}],
                   'params': {'tts_timing_mode': 'natural', 'tts_text_source': 'translated'}}
        old = signature_payload(project)
        project['params']['tts_auto_schedule'] = True
        self.assertNotEqual(old, signature_payload(project))
