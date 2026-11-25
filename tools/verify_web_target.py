"""Opt-in administrator checks for web-target packet loss; never disable adapters.

The local case owns two temporary HTTP servers and impairs only one of their
ports.  Optional remote checks perform TCP handshakes only, with no login or
application messages.  Every interception handle is stopped in finally blocks.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlab import __version__
from netlab.config import resolve_web_config, validate_config
from netlab.engine import Engine


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(21):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(.025)


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):
        self.do_HEAD()
        try:
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionError):
            pass

    def log_message(self, *args):
        pass


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *args):
        # A probe deliberately abandons its socket after its short timeout.
        pass


def open_server():
    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    return server, thread


def http_probe(port, *, timeout=.45, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    started = time.monotonic()
    result = dict(port=port, method=method, connected=False, timed_out=False)
    try:
        connection.request(method, "/")
        response = connection.getresponse()
        body = response.read()
        result.update(connected=response.status == 200, status=response.status,
                      body_ok=(body == b"ok" if method == "GET" else body == b""))
    except TimeoutError:
        result["timed_out"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        connection.close()
        result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


def tcp_probe(address, port, *, timeout=3):
    family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
    result = dict(address=address, port=port, connected=False, timed_out=False)
    started = time.monotonic()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect((address, port))
            result["connected"] = True
            # No TLS, HTTP, WebSocket, credentials or game payload is sent.
    except TimeoutError:
        result["timed_out"] = True
    except OSError as exc:
        result["error"] = str(exc)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


def wait_phase(engine, phase, cycle, *, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = engine.snapshot()
        if snapshot.get("error") or not snapshot["running"]:
            raise RuntimeError("Engine stopped while waiting: " + str(snapshot.get("error")))
        if snapshot["phase"] == phase and snapshot["cycle"] == cycle:
            remaining = snapshot.get("active_remaining") if phase == "active" else snapshot.get("wait_remaining")
            if remaining is None or remaining >= .7:
                return snapshot
        if snapshot.get("cycle", 0) > cycle:
            raise AssertionError("Missed required cycle; no timing evidence may be inferred")
        time.sleep(.01)
    raise TimeoutError(f"Did not observe {phase}, cycle {cycle}")


def local_cycle_case(report, output):
    case = dict(name="Local web target: two blackout cycles with an unaffected HTTP control",
                passed=False, probes=[])
    report["cases"].append(case)
    servers = []
    engine = Engine(ROOT / "vendor" / "windivert")
    try:
        target, target_thread = open_server()
        servers.append((target, target_thread))
        control, control_thread = open_server()
        servers.append((control, control_thread))
        target_port, control_port = target.server_port, control.server_port
        case.update(target_port=target_port, control_port=control_port)

        def probe(label, blocked=False, method="GET"):
            observed = dict(label=label, target=http_probe(target_port, method=method),
                            control=http_probe(control_port), snapshot=engine.snapshot())
            case["probes"].append(observed)
            save_report(output, report)
            require(observed["control"]["connected"] and observed["control"]["body_ok"],
                    "Independent HTTP control was affected")
            if blocked:
                require(observed["target"]["timed_out"], "Target did not time out during its outage")
            else:
                require(observed["target"]["connected"] and observed["target"]["body_ok"],
                        "Target was not usable outside its outage")

        probe("baseline")
        config = validate_config(dict(scope="web", host=f"http://localhost:{target_port}/",
                                      blackout=True, blackout_mode="packet", blackout_loop=True,
                                      start_delay_s=1, blackout_duration_s=2, duration_s=2,
                                      delay_ms=0, jitter_ms=0, loss_pct=0))
        engine.start(config)
        case["filter"] = engine._config["filter"]
        case["resolved_ips"] = engine._config["web_ips"]
        wait_phase(engine, "waiting", 1)
        probe("initial_wait")
        wait_phase(engine, "active", 1)
        probe("outage_cycle_1", blocked=True, method="HEAD")
        wait_phase(engine, "waiting", 2)
        probe("recovery_between_cycles")
        wait_phase(engine, "active", 2)
        probe("outage_cycle_2", blocked=True)
        case["stats_before_stop"] = engine.snapshot()
        require(case["stats_before_stop"]["received"] > 0 and case["stats_before_stop"]["dropped"] > 0,
                "No real captured/dropped packet evidence")
        require(case["stats_before_stop"]["errors"] == 0, "Packet engine reported an error")
        engine.stop()
        probe("after_stop")
        require(not engine.snapshot()["running"], "Interception still running after stop")
        case["passed"] = True
    except Exception as exc:
        case.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        try:
            engine.stop()
            case["stopped"] = not engine.snapshot()["running"]
        except Exception as exc:
            case.update(passed=False, cleanup_error=str(exc))
        for server, thread in reversed(servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        case["servers_closed"] = all(not thread.is_alive() for _, thread in servers)
        if not case["servers_closed"]:
            case["passed"] = False
        save_report(output, report)


def remote_case(report, output, host, port, control_host):
    case = dict(name="Remote game endpoint TCP handshake only", host=host, port=port,
                control_host=control_host, control_port=443, passed=False, probes=[])
    report["cases"].append(case)
    engine = Engine(ROOT / "vendor" / "windivert")
    control, thread = open_server()
    config = dict(scope="web", host=host, ports=str(port), protocol="tcp", blackout=True,
                  blackout_mode="packet", blackout_loop=False, start_delay_s=0,
                  blackout_duration_s=20, duration_s=20, delay_ms=0, jitter_ms=0, loss_pct=0)
    try:
        resolved = resolve_web_config(config)
        control_config = resolve_web_config(dict(scope="web", host=control_host,
                                                  ports="443", protocol="tcp"))
        control_candidates = [address for address in control_config["web_ips"]
                              if address not in resolved["web_ips"]]
        require(control_candidates, "Public control shares all target IPs; choose an unrelated --control-host")
        selected_control = None
        for address in sorted(control_candidates, key=lambda value: ipaddress.ip_address(value).version):
            baseline_control = tcp_probe(address, 443)
            case["probes"].append(dict(label="public_control_baseline", control=baseline_control))
            if baseline_control["connected"]:
                selected_control = address
                break
        require(selected_control is not None, "Unrelated public TCP control could not connect before capture")
        case["control_resolved_ips"] = control_config["web_ips"]
        case["control_selected_ip"] = selected_control
        selected = None
        for address in sorted(resolved["web_ips"], key=lambda value: ipaddress.ip_address(value).version):
            baseline = tcp_probe(address, port)
            case["probes"].append(dict(label="baseline", target=baseline))
            if baseline["connected"]:
                selected = address
                break
        require(selected is not None, "Remote baseline could not connect; cannot verify loss")
        engine.start(config)
        case.update(filter=engine._config["filter"], resolved_ips=engine._config["web_ips"])
        require(selected in case["resolved_ips"], "DNS changed before capture; baseline target is not covered")
        require(selected_control not in case["resolved_ips"], "Public control overlaps freshly resolved target IPs")
        blocked = tcp_probe(selected, port, timeout=1)
        control_probe = http_probe(control.server_port)
        public_control = tcp_probe(selected_control, 443)
        case["probes"].append(dict(label="blocked", target=blocked, control=control_probe,
                                   public_control=public_control))
        case["stats_before_stop"] = engine.snapshot()
        save_report(output, report)
        require(blocked["timed_out"], "Remote connection was not blocked")
        require(control_probe["connected"], "Local control was affected")
        require(public_control["connected"], "Unrelated public TCP control could not connect during target loss")
        require(case["stats_before_stop"]["received"] > 0 and case["stats_before_stop"]["dropped"] > 0,
                "Remote test captured no packets")
        require(case["stats_before_stop"]["errors"] == 0, "Remote engine reported an error")
        engine.stop()
        restored = tcp_probe(selected, port, timeout=8)
        public_restored = tcp_probe(selected_control, 443)
        case["probes"].append(dict(label="after_stop", target=restored,
                                   control=http_probe(control.server_port),
                                   public_control=public_restored))
        require(restored["connected"], "Remote TCP target did not recover after stop")
        require(public_restored["connected"], "Unrelated public TCP control did not remain available after stop")
        case["passed"] = True
    except Exception as exc:
        case.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        try:
            engine.stop()
            case["stopped"] = not engine.snapshot()["running"]
        except Exception as exc:
            case.update(passed=False, cleanup_error=str(exc))
        control.shutdown()
        control.server_close()
        thread.join(timeout=2)
        case["server_closed"] = not thread.is_alive()
        if not case["server_closed"]:
            case["passed"] = False
        save_report(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--remote-host")
    parser.add_argument("--remote-port", type=int, default=443)
    parser.add_argument("--control-host", default="www.baidu.com",
                        help="Unrelated public TCP control hostname (port 443)")
    args = parser.parse_args()
    report = dict(version=__version__, started_at=datetime.now().astimezone().isoformat(),
                  network_adapter_changes=False, complete=False, passed=False, cases=[])
    output = args.output.resolve()
    save_report(output, report)
    try:
        require(os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin(), "Windows administrator permission required")
        require(1 <= args.remote_port <= 65535, "Remote port is invalid")
        local_cycle_case(report, output)
        if args.remote_host and report["cases"][-1]["passed"]:
            remote_case(report, output, args.remote_host, args.remote_port, args.control_host)
        report["passed"] = bool(report["cases"] and all(case["passed"] for case in report["cases"]))
        if args.remote_host and len(report["cases"]) != 2:
            report["passed"] = False
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        report.update(complete=True, finished_at=datetime.now().astimezone().isoformat())
        save_report(output, report)
    print(json.dumps(dict(passed=report["passed"], output=str(output))))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
