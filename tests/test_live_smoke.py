import unittest
from unittest.mock import patch
from tools.live_smoke import run_case


class LiveSmokeVerdictTests(unittest.TestCase):
    def test_failed_start_cannot_count_as_successful_packet_loss(self):
        with patch('netlab.engine.Engine.start', side_effect=OSError('permission denied')):
            row = run_case('loss', {'loss_pct': 100, 'expect': 0}, '.')
        self.assertFalse(row['ok'])
        self.assertEqual(row['received'], 0)
        self.assertIn('permission denied', row['error'])
