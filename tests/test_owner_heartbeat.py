"""Owner discovery remains healthy during silent workers, without real waits."""
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

import psutil

import owner_connection
import server


SESSION = 'fixture-heartbeat-local-session-token'


class OwnerHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.record = {'schema_version': 1, 'host': '127.0.0.1', 'port': 32123,
                       'pid': os.getpid(), 'process_started': psutil.Process().create_time(),
                       'instance_id': 'd' * 32, 'token': SESSION,
                       'heartbeat_at': time.time(), 'queue_owned': True}
        self.queue_thread = Mock()
        self.queue_thread.is_alive.return_value = True
        self.queue_file = Mock(closed=False)
        self.runtime = {'healthy': True, 'last_error': '', 'last_success': None, 'consecutive_errors': 0}
        self.owner_runtime = {'healthy': True}
        self.patches = [patch.object(server, 'owner_record', self.record),
                        patch.object(server, 'queue_thread', self.queue_thread),
                        patch.object(server, 'queue_file_lock', self.queue_file),
                        patch.object(server, 'queue_runtime', self.runtime),
                        patch.object(server, 'owner_runtime', self.owner_runtime),
                        patch.object(server, 'state_lock', threading.RLock())]
        for value in self.patches:
            value.start()
            self.addCleanup(value.stop)

    def test_dedicated_pulse_refreshes_during_long_silent_worker(self):
        self.record['heartbeat_at'] -= 120
        fresh = dict(self.record, heartbeat_at=time.time())
        stopped = Mock()
        stopped.wait.side_effect = [False, True]
        with patch.object(server, 'stop_event', stopped), patch('owner_connection.refresh', return_value=fresh) as refresh:
            server.owner_heartbeat_loop()
        self.assertEqual(self.record['heartbeat_at'], fresh['heartbeat_at'])
        self.assertEqual(stopped.wait.call_args_list[0].args, (10,))
        refresh.assert_called_once_with(server.DATA, self.record, queue_owned=True)
        with patch.object(server, 'get', return_value=None):
            payload = server.live_owner()
        owner_connection.validate_live_owner(self.record, payload)
        self.assertNotIn(SESSION, json.dumps(payload))
        self.assertNotIn('token', payload)

    def test_register_starts_separate_daemon_and_does_not_echo_session(self):
        with patch.object(server, 'owner_record', None), patch.object(server, 'owner_heartbeat_thread', None), \
                patch('owner_connection.publish', return_value=self.record) as publish, \
                patch.object(server.threading, 'Thread') as thread:
            actual = server.register_owner(32123)
            thread.assert_called_once_with(target=server.owner_heartbeat_loop, name='NPKOwnerHeartbeat', daemon=True)
            thread.return_value.start.assert_called_once_with()
            publish.assert_called_once_with(server.DATA, 32123, server.TOKEN, queue_owned=True)
            self.assertIs(actual, self.record)
            with patch.object(server, 'get', return_value=None):
                self.assertNotIn(SESSION, json.dumps(server.live_owner()))

    def test_refresh_failure_denies_live_handshake_without_exposing_exception(self):
        stopped = Mock()
        stopped.wait.side_effect = [False, True]
        with patch.object(server, 'stop_event', stopped), patch('owner_connection.refresh', side_effect=RuntimeError(SESSION)):
            server.owner_heartbeat_loop()
        self.assertFalse(self.owner_runtime['healthy'])
        # A normal idle queue pulse cannot overwrite a failed owner heartbeat.
        self.runtime.update(healthy=True, last_error='', consecutive_errors=0)
        with patch.object(server, 'get', return_value=None):
            payload = server.live_owner()
        self.assertFalse(payload['healthy'])
        self.assertNotIn(SESSION, json.dumps(self.runtime))
        self.assertNotIn(SESSION, json.dumps(self.owner_runtime))
        self.assertNotIn(SESSION, json.dumps(payload))
        with self.assertRaises(owner_connection.OwnerUnavailable):
            owner_connection.validate_live_owner(self.record, payload)

    def test_verified_heartbeat_recovers_owner_without_hiding_queue_failure(self):
        self.owner_runtime['healthy'] = False
        self.runtime['healthy'] = False
        stopped = Mock()
        stopped.wait.side_effect = [False, True]
        fresh = dict(self.record, heartbeat_at=time.time())
        with patch.object(server, 'stop_event', stopped), patch('owner_connection.refresh', return_value=fresh):
            server.owner_heartbeat_loop()
        self.assertTrue(self.owner_runtime['healthy'])
        with patch.object(server, 'get', return_value=None):
            self.assertFalse(server.live_owner()['healthy'])
            self.runtime['healthy'] = True
            self.assertTrue(server.live_owner()['healthy'])

    def test_shutdown_joins_publisher_before_deleting_own_discovery(self):
        stopped = Mock()
        publisher = Mock()
        order = []
        publisher.join.side_effect = lambda **_: order.append('heartbeat_join')
        self.queue_thread.is_alive.return_value = False
        with patch.object(server, 'stop_event', stopped), patch.object(server, 'owner_heartbeat_thread', publisher), \
                patch.object(server, 'kill_owned_worker'), patch('live_clean_preview.cancel_active'), \
                patch('owner_connection.remove', side_effect=lambda *_: order.append('remove')):
            server.shutdown()
        stopped.set.assert_called_once_with()
        self.assertLess(order.index('heartbeat_join'), order.index('remove'))
        self.queue_file.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
