"""Final batch telemetry must survive progress throttling."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import worker


class UsageMilestoneEvents(unittest.TestCase):
    def test_last_one_cue_batch_is_not_dropped_after_response_usage(self):
        out = io.StringIO()
        with patch.object(worker, '_disk_guard', None), patch.object(worker, '_pipeline_phase', (2, 6, 'translate')), \
                patch.object(worker, '_event_stage', '3/6:translate'), patch.object(worker, '_event_at', 100.), \
                patch('worker.time.monotonic', return_value=100.1), redirect_stdout(out):
            worker.emit(progress=1., api_usage={'completed_cues':381, 'remaining_cues':0})
        event = json.loads(out.getvalue())
        self.assertEqual(event['api_usage']['completed_cues'], 381)
        self.assertEqual(event['api_usage']['remaining_cues'], 0)

    def test_frame_only_progress_remains_throttled(self):
        out = io.StringIO()
        with patch.object(worker, '_disk_guard', None), patch.object(worker, '_pipeline_phase', None), \
                patch.object(worker, '_event_stage', 'ocr'), patch.object(worker, '_event_at', 100.), \
                patch('worker.time.monotonic', return_value=100.1), redirect_stdout(out):
            worker.emit(stage='ocr', progress=.3)
        self.assertEqual(out.getvalue(), '')
