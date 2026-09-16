import unittest,time,threading,ctypes,queue
from netlab.engine import Engine,validate_filter,_WinDivertTransport
from netlab.config import DEFAULT_CONFIG, PRESETS, validate_config
class T:
 def __init__(self): self.q=[]; self.sent=[]; self.closed=False; self.shutdown_calls=[]; self.opens=0; self.block_open=False; self.open_enter=threading.Event(); self.open_release=threading.Event()
 def open(self,e):
  self.e=e; self.opens+=1; self.open_enter.set()
  if self.block_open: self.open_release.wait(1)
  return self.opens
 def recv(self,h):
  if self.q:return self.q.pop(0)
  time.sleep(.005); return None
 def send(self,h,p,a): self.sent.append(p); return len(p)
 def close(self,h): self.closed=True
 def shutdown(self,h): self.shutdown_calls.append(h)

def wait_for(pred, timeout=1):
 end=time.time()+timeout
 while time.time()<end:
  if pred(): return True
  time.sleep(.005)
 return pred()
class Test(unittest.TestCase):
 def test_fake_compile_filter_abi(self):
  class Fn:
   def __init__(self): self.argtypes=None; self.restype=None; self.args=None
   def __call__(self,*args):
    self.args=args
    err=ctypes.cast(args[4],ctypes.POINTER(ctypes.c_char_p)); pos=ctypes.cast(args[5],ctypes.POINTER(ctypes.c_uint))
    err[0]=b'bad syntax'; pos[0]=3
    return 0
  f=Fn(); obj=object.__new__(_WinDivertTransport); obj.compile_fn=f
  with self.assertRaisesRegex(ValueError,'position 3'):
   obj.validate_filter('abc')
  self.assertEqual(len(f.args),6)

 def test_loss_dup_ipv6(self):
  t=T(); e=Engine('.',transport=t); e.start({'filter':'ipv6','loss_pct':0,'duplicate_pct':100}); t.q.append((b'x',bytes(80))); time.sleep(.04); e.stop(); self.assertEqual(len(t.sent),2)
 def test_filter(self):
  self.assertRaises(ValueError,validate_filter,'(')
 def test_stop(self):
  t=T();e=Engine('.',transport=t);e.start({'filter':'outbound','duration_s':1}); e.stop(); self.assertFalse(e.snapshot()['running'])
 def test_stop_shuts_down_before_close(self):
  calls=[]
  class Transport(T):
   def shutdown(self, h): calls.append(('shutdown', h)); super().shutdown(h)
   def close(self, h): calls.append(('close', h)); super().close(h)
  t=Transport(); e=Engine('.',transport=t); e.start({'filter':'outbound'}); e.stop()
  self.assertEqual(calls, [('shutdown', 1), ('close', 1)])
 def test_receive_failure_shuts_down_handle(self):
  class Broken(T):
   def recv(self, h): raise OSError('recv failed')
  t=Broken(); e=Engine('.',transport=t); e.start({'filter':'outbound'})
  self.assertTrue(wait_for(lambda:not e.snapshot()['running'], .5))
  self.assertEqual(t.shutdown_calls, [1]); self.assertTrue(t.closed)

 def test_waiting_defers_open_then_active_duration(self):
  t=T(); e=Engine('.',transport=t)
  e.start({'filter':'udp','blackout':True,'start_delay_s':1,'blackout_duration_s':1})
  self.assertEqual(t.opens,0); self.assertEqual(e.snapshot()['phase'],'waiting')
  self.assertTrue(wait_for(lambda:t.opens==1, 1.5)); self.assertEqual(e.snapshot()['phase'],'active')
  self.assertTrue(wait_for(lambda:not e.snapshot()['running'], 1.5)); self.assertTrue(t.closed)

 def test_wait_cancel_never_opens(self):
  t=T(); e=Engine('.',transport=t); e.start({'filter':'x','blackout':True,'start_delay_s':1,'blackout_duration_s':1}); e.stop(); time.sleep(1.2)
  self.assertEqual(t.opens,0); self.assertFalse(e.snapshot()['running'])

 def test_cancel_restart_no_ghost_open(self):
  t=T(); e=Engine('.',transport=t); e.start({'filter':'x','blackout':True,'start_delay_s':1,'blackout_duration_s':1}); e.stop(); e.start({'filter':'x','blackout':True,'start_delay_s':0,'blackout_duration_s':1}); self.assertEqual(t.opens,1); e.stop(); self.assertEqual(t.opens,1)

 def test_open_stop_race_closes_handle(self):
  t=T(); t.block_open=True; e=Engine('.',transport=t)
  th=threading.Thread(target=lambda:e.start({'filter':'x'})); th.start(); self.assertTrue(t.open_enter.wait(.2)); e.stop(); t.open_release.set(); th.join(1); self.assertTrue(t.closed); self.assertFalse(e.snapshot()['running'])

 def test_validation_blackout_duration(self):
  t=T(); e=Engine('.',transport=t)
  with self.assertRaises(ValueError): e.start({'filter':'x','blackout':True,'blackout_duration_s':1.5})
  with self.assertRaises(ValueError): e.start({'filter':'x','blackout':True,'start_delay_s':1,'blackout_duration_s':0})

 def test_validated_default_and_presets_start(self):
  scenarios = [('default', DEFAULT_CONFIG)] + [(p['name'], p['values']) for p in PRESETS]
  for name, values in scenarios:
   with self.subTest(name=name):
    cfg=validate_config(values); t=T(); e=Engine('.',transport=t)
    try:
     e.start(cfg)
     self.assertTrue(e.snapshot()['running'])
     self.assertEqual(e.snapshot()['phase'], 'waiting' if cfg['blackout'] and cfg['start_delay_s'] else 'active')
    finally:
     e.stop()

 def test_unchecked_blackout_retains_values_but_starts_normally(self):
  cfg=validate_config({'blackout':False,'start_delay_s':10,'blackout_duration_s':5,'duration_s':60})
  t=T(); e=Engine('.',transport=t)
  try:
   e.start(cfg)
   self.assertEqual(t.opens,1)
   self.assertEqual(e.snapshot()['phase'],'active')
   self.assertEqual(e._config['duration_s'],60)
   self.assertEqual(cfg['start_delay_s'],10)
   self.assertEqual(cfg['blackout_duration_s'],5)
  finally:
   e.stop()

 def test_legacy_blackout_duration_is_preserved(self):
  t=T(); e=Engine('.',transport=t)
  try:
   e.start({'filter':'udp','blackout':True,'duration_s':3})
   self.assertEqual(e._config['duration_s'],3)
  finally:
   e.stop()

 def test_elapsed_freezes_after_stop(self):
  t=T(); e=Engine('.',transport=t); e.start({'filter':'x','blackout':True,'start_delay_s':1,'blackout_duration_s':1}); self.assertTrue(wait_for(lambda:e.snapshot()['phase']=='active', 1.5)); e.stop(); s=e.snapshot(); time.sleep(.03); self.assertEqual(s['elapsed'],e.snapshot()['elapsed'])
class LoopTransport:
    """Each handle owns its receive queue, as a real driver handle does."""
    def __init__(self):
        self.opens = 0
        self.queues = {}
        self.closed = []
        self.shutdown_calls = []
        self.fail_open = None
        self.block_open = None
        self.open_entered = threading.Event()
        self.open_release = threading.Event()
        self.late_receive = None
        self.receive_closing = threading.Event()
        self.receive_release = threading.Event()

    def open(self, expression):
        self.opens += 1
        handle = self.opens
        if handle == self.block_open:
            self.open_entered.set()
            if not self.open_release.wait(5):
                raise RuntimeError("blocked open timed out")
        if handle == self.fail_open:
            raise OSError("reopen failed")
        self.queues[handle] = queue.Queue()
        return handle

    def recv(self, handle):
        try:
            value = self.queues[handle].get(timeout=.02)
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            if handle == self.late_receive:
                self.receive_closing.set()
                if not self.receive_release.wait(5):
                    raise RuntimeError("late receive timed out")
            raise value
        return value

    def shutdown(self, handle):
        self.shutdown_calls.append(handle)
        self.queues[handle].put(OSError("handle shut down"))

    def close(self, handle):
        self.closed.append(handle)


class BlackoutLoopTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.transport = LoopTransport()
        self.engine = Engine(".", transport=self.transport, clock=lambda: self.now)
        self.addCleanup(self.engine.stop)
        self.config = {"filter": "udp", "blackout": True, "blackout_loop": True,
                       "start_delay_s": 1, "blackout_duration_s": 1}

    def advance(self, seconds):
        with self.engine._condition:
            self.now += seconds
            self.engine._condition.notify_all()

    def active(self, cycle):
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["phase"] == "active"
                                and self.engine.snapshot()["cycle"] == cycle))

    def waiting(self, cycle):
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["phase"] == "waiting"
                                and self.engine.snapshot()["cycle"] == cycle))

    def reach_second_wait(self):
        self.engine.start(self.config)
        self.advance(1)
        self.active(1)
        self.advance(1)
        self.waiting(2)

    def test_repeated_wait_outage_recovery_and_aggregate_statistics(self):
        self.engine.start(self.config)
        self.waiting(1)
        self.assertEqual(self.transport.opens, 0)
        self.advance(1)
        self.active(1)
        self.transport.queues[1].put((b"one", bytes(80)))
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["dropped"] == 1))
        self.advance(1)
        self.waiting(2)
        self.assertEqual(self.transport.closed, [1])
        self.assertEqual(self.transport.shutdown_calls, [1])
        self.assertIsNone(self.engine._handle)
        self.assertAlmostEqual(self.engine.snapshot()["wait_remaining"], 1)
        self.advance(.4)
        self.assertAlmostEqual(self.engine.snapshot()["wait_remaining"], .6)
        self.advance(.6)
        self.active(2)
        self.transport.queues[2].put((b"two", bytes(80)))
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["dropped"] == 2))
        self.assertEqual(self.engine.snapshot()["elapsed"], 3)
        self.assertEqual(self.engine.snapshot()["active_remaining"], 1)
        self.advance(1)
        self.waiting(3)
        self.assertEqual(self.transport.closed, [1, 2])
        self.assertEqual(self.engine.snapshot()["errors"], 0)

    def test_stop_during_second_wait_never_reopens(self):
        self.reach_second_wait()
        self.engine.stop()
        frozen = self.engine.snapshot()["elapsed"]
        self.advance(10)
        self.assertFalse(self.engine.snapshot()["running"])
        self.assertEqual(self.engine.snapshot()["phase"], "stopped")
        self.assertEqual(self.engine.snapshot()["elapsed"], frozen)
        self.assertFalse(self.engine._session.is_alive())
        self.assertEqual(self.transport.opens, 1)

    def test_stop_during_second_outage_closes_once_and_allows_new_session(self):
        self.reach_second_wait()
        self.advance(1)
        self.active(2)
        self.engine.stop()
        self.assertEqual(self.transport.closed, [1, 2])
        self.assertEqual(self.transport.shutdown_calls, [1, 2])
        self.assertFalse(self.engine._receiver.is_alive())
        self.assertFalse(self.engine._session.is_alive())
        self.engine.start(self.config)
        self.waiting(1)
        self.advance(1)
        self.active(1)
        self.assertEqual(self.transport.opens, 3)

    def test_stop_during_second_open_closes_late_handle_without_receiver(self):
        self.reach_second_wait()
        self.transport.block_open = 2
        self.advance(1)
        self.assertTrue(self.transport.open_entered.wait(1))
        stopper = threading.Thread(target=self.engine.stop)
        stopper.start()
        self.assertTrue(self.engine._stop_event.wait(1))
        self.transport.open_release.set()
        stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(self.transport.closed, [1, 2])
        self.assertEqual(self.transport.shutdown_calls, [1, 2])
        self.assertFalse(self.engine.snapshot()["running"])
        self.assertFalse(self.engine._session.is_alive())

    def test_late_shutdown_receive_error_is_ignored_before_next_round(self):
        self.transport.late_receive = 1
        self.engine.start(self.config)
        self.advance(1)
        self.active(1)
        self.advance(1)
        self.assertTrue(self.transport.receive_closing.wait(1))
        self.assertEqual(self.transport.closed, [1])
        self.assertEqual(self.transport.opens, 1)
        self.transport.receive_release.set()
        self.waiting(2)
        self.advance(1)
        self.active(2)
        self.assertEqual(self.engine.snapshot()["errors"], 0)

    def test_reopen_failure_stops_loop(self):
        self.transport.fail_open = 2
        self.reach_second_wait()
        self.advance(1)
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["phase"] == "error"))
        self.assertFalse(self.engine.snapshot()["running"])
        self.assertEqual(self.engine.snapshot()["error"], "reopen failed")
        self.assertEqual(self.transport.closed, [1])
        self.engine.stop()
        self.advance(10)
        self.assertEqual(self.transport.opens, 2)

    def test_receive_failure_in_second_round_closes_and_stops(self):
        self.reach_second_wait()
        self.advance(1)
        self.active(2)
        self.transport.queues[2].put(OSError("connection failed"))
        self.assertTrue(wait_for(lambda: self.engine.snapshot()["phase"] == "error"))
        self.assertFalse(self.engine.snapshot()["running"])
        self.assertEqual(self.transport.closed, [1, 2])
        self.engine.stop()
        self.advance(10)
        self.assertEqual(self.transport.opens, 2)

    def test_loop_requires_valid_mode_and_nonzero_phases(self):
        for changes in ({"blackout_loop": "true"}, {"blackout_loop": 1},
                        {"blackout": False}, {"start_delay_s": 0},
                        {"blackout_duration_s": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.start(dict(self.config, **changes))
        self.assertEqual(self.transport.opens, 0)


if __name__=='__main__': unittest.main()
