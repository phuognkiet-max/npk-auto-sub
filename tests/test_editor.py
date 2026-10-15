import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core import DATA, get, create_project
from fastapi import HTTPException
from job_progress import accept_event, verify_outputs
from server import update_project


class EditorTests(unittest.TestCase):
    def test_database_read_closes_connection(self):
        from core import get
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with patch('core.connect', return_value=db):
            self.assertIsNone(get('project', 'missing'))
        db.close.assert_called_once()

    def test_database_write_commits_and_closes(self):
        from core import put
        db = MagicMock()
        with patch('core.connect', return_value=db):
            put('project', 'fixture', {'name': 'fixture'})
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_worker_cannot_publish_success(self):
        job = dict(state='running', stage='process', progress=.3, total=10, completed=3, fps=2)
        accept_event(job, dict(stage='done', progress=1, result={}, message='done'))
        self.assertEqual(job['state'], 'running')
        self.assertEqual(job['stage'], 'verify')
        self.assertEqual(job['progress'], .99)
        self.assertNotIn('total', job)

    def test_progress_finite_and_bounded(self):
        job = dict(state='running', progress=.3)
        for value in (float('nan'), float('inf'), None, 'bad'):
            accept_event(job, dict(progress=value))
            self.assertEqual(job['progress'], .3)
        accept_event(job, dict(progress=-3))
        self.assertEqual(job['progress'], 0)

    def test_cancelled_is_immutable(self):
        job = dict(state='cancelled', progress=.5)
        self.assertEqual(accept_event(job, dict(progress=1, result={})), dict(state='cancelled', progress=.5))

    def test_event_history_bounded(self):
        job = dict(state='running')
        for index in range(100):
            accept_event(job, dict(stage=str(index), message='event'))
        self.assertEqual(len(job['events']), 40)
        accept_event(job, dict(stage='99', message='latest'))
        self.assertEqual(len(job['events']), 40)
        self.assertEqual(job['events'][-1]['message'], 'latest')

    def test_output_contract(self):
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            project = dict(media=dict(duration=3, fps=25, audio=True))
            for files in ([], ['../outside.mp4'], ['clean.mp4']):
                with self.assertRaises(ValueError):
                    verify_outputs(dict(files=files), project, dict(kind='clean'), folder)
            Path(folder, 'clean.mp4').write_bytes(b'fixture')
            with patch('core.probe', return_value=dict(duration=1, audio=True)):
                with self.assertRaises(ValueError):
                    verify_outputs(dict(files=['clean.mp4']), project, dict(kind='clean'), folder)
                with self.assertRaises(ValueError):
                    verify_outputs(dict(files=['clean.mp4'], duration=1), project, dict(kind='clean'), folder)
            with patch('core.probe', return_value=dict(duration=3, audio=False)):
                with self.assertRaises(ValueError):
                    verify_outputs(dict(files=['clean.mp4']), project, dict(kind='clean'), folder)
            with patch('core.probe', return_value=dict(duration=3, audio=True)), patch('core.run') as decode:
                decode.return_value.stdout = b'frame=75\nprogress=end\n'
                result = verify_outputs(dict(files=['clean.mp4']), project, dict(kind='clean'), folder)
                self.assertEqual(result['files_checked'], 1)
                self.assertEqual(result['video_frames_decoded'], {'clean.mp4':75})
                decode.assert_called_once()

    def test_revision_conflict_preserves_previous(self):
        with patch('core.probe', return_value=dict(duration=3, fps=25, width=100, height=100)):
            project = create_project('fixture.mp4', 'Editor unit')
        updated = update_project(project['id'], dict(expected_revision=project['updated'], name='New name'))
        self.assertEqual(get('project_backup', project['id'])['name'], 'Editor unit')
        with self.assertRaises(HTTPException) as error:
            update_project(project['id'], dict(expected_revision=project['updated'], name='Stale name'))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(get('project', project['id'])['name'], 'New name')
