"""NetLab localhost-only live smoke tests.
Requires an elevated Windows shell for WinDivert; never self-elevates.
"""
from __future__ import annotations
import argparse, ctypes, json, os, socket, statistics, threading, time, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def is_admin() -> bool:
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception: return False

def run_case(name, opts, dll_dir):
    from netlab.engine import Engine
    port_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); port_sock.bind(("127.0.0.1", 0)); port = port_sock.getsockname()[1]
    port_sock.settimeout(0.8); received=[]
    filt = f"outbound and loopback and udp and ip.DstAddr == 127.0.0.1 and udp.DstPort == {port} and not impostor"
    cfg = {"filter": filt, "delay_ms": opts.get("delay_ms",0), "jitter_ms":0, "loss_pct":opts.get("loss_pct",0), "duplicate_pct":opts.get("duplicate_pct",0), "reorder_pct":0, "reorder_ms":0, "bandwidth_kbps":0, "duration_s":opts.get("duration_s",4), "blackout":False}
    eng = Engine(dll_dir)
    started=time.perf_counter(); error=None
    try:
        eng.start(cfg)
        tx=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); tx.sendto(name.encode(),("127.0.0.1",port)); tx.close()
        deadline=time.perf_counter()+min(5, max(1,cfg["duration_s"]+1))
        while time.perf_counter()<deadline:
            try: data,addr=port_sock.recvfrom(2048); received.append(time.perf_counter()-started)
            except socket.timeout: break
            if opts.get("expect",1)==len(received): break
        stats=eng.snapshot()
    except Exception as exc:
        stats=eng.snapshot(); error=str(exc)
    finally:
        try: eng.stop()
        except Exception as exc: error = error or str(exc)
        port_sock.close()
    ok = (len(received)==opts.get("expect",1)) if opts.get("expect") is not None else bool(received)
    if opts.get("min_delay") is not None: ok = ok and bool(received) and received[0] >= opts["min_delay"]
    return {"name":name,"ok":ok,"port":port,"received":len(received),"latencies_s":received,"stats":stats,"error":error}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--output",default="live_smoke_report.json"); args=ap.parse_args()
    report={"timestamp":time.strftime("%Y-%m-%dT%H:%M:%S%z"),"admin":is_admin(),"localhost_only":True,"cases":[],"summary":{}}
    out=Path(args.output)
    if os.name!="nt" or not report["admin"]:
        report["summary"]={"status":"skipped","reason":"需要以管理员身份运行 Windows；脚本不会自动提权。","return_code":2}
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(report,ensure_ascii=False,indent=2)); return 2
    dll=ROOT/"vendor"/"windivert"
    cases=[("无损收发",{},1), ("延迟150ms",{"delay_ms":150,"min_delay":0.10},1), ("丢包100%",{"loss_pct":100,"expect":0},0), ("重复100%",{"duplicate_pct":100,"expect":2},2)]
    for name,opts,expect in cases:
        opts=dict(opts); opts.setdefault("expect",expect); report["cases"].append(run_case(name,opts,dll))
    report["summary"]={"status":"passed" if all(c["ok"] for c in report["cases"]) else "failed","passed":sum(c["ok"] for c in report["cases"]),"total":len(report["cases"]),"return_code":0}
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["summary"]["status"]=="passed" else 1
if __name__=="__main__": raise SystemExit(main())
