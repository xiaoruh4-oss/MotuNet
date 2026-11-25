"""Administrator-only local-port network checks; always stop every handle."""
from __future__ import annotations
import argparse
import ctypes
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
import threading
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlab import __version__
from netlab.engine import Engine


def wait_for(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate(): return True
        time.sleep(.01)
    return bool(predicate())


def udp_case(name, settings, n=200, gap=.003):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1', 0))
    receiver.settimeout(.05)
    port = receiver.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seen = []; done = threading.Event()
    def receive():
        while not done.is_set():
            try:
                data, _ = receiver.recvfrom(4096)
                seen.append((data, time.monotonic()))
            except socket.timeout: pass
            except OSError: break
    reader = threading.Thread(target=receive, daemon=True); reader.start()
    engine = Engine(ROOT/'vendor/windivert')
    config = dict(filter=f'outbound and loopback and udp and udp.DstPort == {port}', duration_s=8, **settings)
    error = None; stats = {}; recovered = False; after = {}; latency = 0
    try:
        engine.start(config)
        started = time.monotonic()
        for i in range(n):
            tx.sendto(i.to_bytes(4, 'big') + bytes(1020), ('127.0.0.1', port))
            if gap: time.sleep(gap)
        if not wait_for(lambda: engine.snapshot()['received'] >= n):
            raise AssertionError('No complete packet capture')
        if not wait_for(lambda: engine.snapshot()['sent'] + engine.snapshot()['dropped'] >= n * (2 if settings.get('duplicate_pct') == 100 else 1), 5):
            raise AssertionError('Packets did not finish processing')
        time.sleep(.1)
        stats = engine.snapshot()
        received = len([data for data, _ in seen if len(data) == 1024])
        latency = max((when-started for data,when in seen if len(data)==1024), default=0)
        expected = n - stats['dropped'] + stats['duplicated']
        ok = received == expected and stats['received'] == n and stats['errors'] == 0
        loss = settings.get('loss_pct', 0)
        if loss == 20: ok = ok and .08 <= stats['loss_dropped']/n <= .32
        if loss == 100: ok = ok and stats['loss_dropped'] == n and received == 0
        if loss == 0 and not settings.get('blackout'): ok = ok and stats['loss_dropped'] == 0
        if settings.get('delay_ms'): ok = ok and latency >= settings['delay_ms']/1000*.8
        if settings.get('bandwidth_kbps'): ok = ok and latency >= (n-1)/settings['bandwidth_kbps']*.8
        if settings.get('reorder_pct'): ok = ok and stats['reordered'] == n
    except Exception as exc:
        error = str(exc); ok = False; received = len(seen); stats = engine.snapshot()
    finally:
        engine.stop(); after = engine.snapshot()
        tx.sendto(b'restored', ('127.0.0.1', port))
        recovered = wait_for(lambda: any(data == b'restored' for data,_ in seen), 2)
        done.set(); reader.join(1); receiver.close(); tx.close()
    return dict(name=name, ok=ok and recovered and not after['running'] and not error,
                config=config, sent=n, delivered=received, observed_span_s=latency,
                stats=stats, stopped=not after['running'], restored=recovered, error=error)


def tcp_case():
    server = socket.socket(); server.bind(('127.0.0.1',0)); server.listen(1)
    port = server.getsockname()[1]; errors=[]
    def echo():
        try:
            conn,_=server.accept()
            with conn:
                while True:
                    data=conn.recv(1024)
                    if not data: break
                    conn.sendall(data)
        except OSError as error: errors.append(str(error))
    worker=threading.Thread(target=echo,daemon=True);worker.start()
    client=socket.create_connection(('127.0.0.1',port),timeout=3)
    engine=Engine(ROOT/'vendor/windivert'); error=None; recovered=False; blocked=False; stats={}
    try:
        client.sendall(b'normal'); assert client.recv(1024)==b'normal'
        engine.start(dict(filter=f'outbound and loopback and tcp and (tcp.SrcPort == {port} or tcp.DstPort == {port})',loss_pct=100,duration_s=8))
        client.settimeout(1);client.sendall(b'blocked')
        try: client.recv(1024)
        except socket.timeout: blocked=True
        stats=engine.snapshot()
    except Exception as exc: error=str(exc)
    finally:
        engine.stop()
        try:
            client.settimeout(5)
            pending=client.recv(1024) if blocked else b''
            client.sendall(b'restored'); restored=client.recv(1024)
            recovered=blocked and pending==b'blocked' and restored==b'restored'
        except Exception as exc: error=error or str(exc)
        client.close();server.close();worker.join(1)
    return dict(name='TCP 100% packet loss and retransmission after stop',
                ok=not error and blocked and recovered and stats.get('loss_dropped',0)>0 and not engine.snapshot()['running'],
                stats=stats, blocked=blocked, restored=recovered, error=error)


def timed_blackout():
    rx=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);rx.bind(('127.0.0.1',0));rx.settimeout(.25)
    tx=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);port=rx.getsockname()[1]
    engine=Engine(ROOT/'vendor/windivert');error=None;phases={};stats={}
    def probe(payload):
        tx.sendto(payload,('127.0.0.1',port))
        try: return rx.recvfrom(1024)[0]==payload
        except socket.timeout: return False
    try:
        engine.start(dict(filter=f'outbound and loopback and udp and udp.DstPort == {port}',blackout=True,start_delay_s=1,blackout_duration_s=1))
        phases['waiting_passes']=probe(b'waiting')
        assert wait_for(lambda:engine.snapshot()['phase']=='active',2)
        phases['outage_blocks']=not probe(b'blocked')
        assert wait_for(lambda:not engine.snapshot()['running'],3)
        stats=engine.snapshot();phases['automatic_restore']=probe(b'restored')
    except Exception as exc:error=str(exc)
    finally:engine.stop();rx.close();tx.close()
    return dict(name='Wait 1s, blackout 1s, automatic recovery',ok=not error and len(phases)==3 and all(phases.values()) and stats.get('blackout_dropped',0)>0,phases=phases,stats=stats,error=error)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    report=dict(version=__version__,timestamp=datetime.now().astimezone().isoformat(),localhost_only=True,cases=[])
    if not ctypes.windll.shell32.IsUserAnAdmin():
        report['error']='Administrator permission required';report['passed']=False
    else:
        for name,settings,n,gap in [
            ('UDP 0%',{'loss_pct':0},200,.003),
            ('UDP 20%',{'loss_pct':20},400,.003),
            ('UDP 100%',{'loss_pct':100},200,.003),
            ('Delay 150ms',{'delay_ms':150},10,.003),
            ('Jitter 30ms',{'delay_ms':100,'jitter_ms':30},30,.003),
            ('Bandwidth 32 KiB/s',{'bandwidth_kbps':32},24,0),
            ('Duplicate 100%',{'duplicate_pct':100},20,.003),
            ('Reorder 100%, 120ms hold',{'reorder_pct':100,'reorder_ms':120},20,.003),
        ]:
            report['cases'].append(udp_case(name,settings,n,gap))
        report['cases'].append(tcp_case())
        report['cases'].append(timed_blackout())
        report['passed']=all(case['ok'] for case in report['cases'])
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
