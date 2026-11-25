import unittest
from netlab.engine import Engine
from netlab.feedback import traffic_feedback
from tests.test_engine import T, wait_for


class LossEvidenceTests(unittest.TestCase):
    def test_zero_and_full_loss_are_measured_and_recover_on_restart(self):
        transport = T()
        engine = Engine('.', transport=transport)
        self.addCleanup(engine.stop)
        for loss in (0, 100, 0):
            transport.sent.clear()
            engine.start({'filter': 'udp', 'loss_pct': loss})
            transport.q.extend((b'packet', bytes(80)) for _ in range(20))
            self.assertTrue(wait_for(lambda: engine.snapshot()['received'] == 20))
            if loss == 0:
                self.assertTrue(wait_for(lambda: len(transport.sent) == 20))
            engine.stop()
            stats = engine.snapshot()
            self.assertEqual(stats['loss_dropped'], 20 if loss else 0)
            self.assertEqual(len(transport.sent), 0 if loss else 20)
            self.assertEqual(stats['blackout_dropped'], 0)

    def test_blackout_is_not_random_loss(self):
        transport = T()
        engine = Engine('.', transport=transport)
        self.addCleanup(engine.stop)
        engine.start({'filter': 'udp', 'blackout': True, 'loss_pct': 100})
        transport.q.append((b'packet', bytes(80)))
        self.assertTrue(wait_for(lambda: engine.snapshot()['blackout_dropped'] == 1))
        self.assertEqual(engine.snapshot()['loss_dropped'], 0)

    def test_overflow_is_not_random_loss(self):
        transport = T()
        engine = Engine('.', transport=transport, max_queue=1)
        self.addCleanup(engine.stop)
        engine.start({'filter': 'udp', 'loss_pct': 0, 'delay_ms': 10000})
        transport.q.extend((b'packet', bytes(80)) for _ in range(3))
        self.assertTrue(wait_for(lambda: engine.snapshot()['overflow_dropped'] == 2))
        self.assertEqual(engine.snapshot()['loss_dropped'], 0)

    def test_no_hits_and_no_loss_do_not_claim_effective_packet_loss(self):
        self.assertIn('尚未捕获', traffic_feedback({'loss_pct': 20}, {}))
        self.assertIn('暂未发生随机丢包', traffic_feedback({'loss_pct': 20}, {'received': 2}))
        self.assertIn('随机丢包未开启', traffic_feedback({'loss_pct': 0}, {'received': 2}))
