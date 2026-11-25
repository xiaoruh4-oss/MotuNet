"""Administrator-only web direction checks using owned localhost TCP/UDP ports.

No adapter, external host or game service is touched.  Each direction owns fresh
target and control sockets, establishes a receive-based baseline, checks the
selected direction while the opposite direction and control remain usable, and
checks recovery after stopping WinDivert.  TCP sendall returning is never treated
as proof of delivery; actual recv evidence is saved for both endpoints.

Run explicitly from an elevated terminal:
  python -B tools/verify_web_direction.py --output artifacts/web-direction.json
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlab import __version__
from netlab.config import validate_config
from netlab.engine import Engine


LOCALHOSTS = ("127.0.0.1", "::1")
BLOCK_OBSERVATION_S = 0.65
BLACKOUT_DURATION_S = 3


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def make_web_config(port, protocol, direction, host):
    # A literal loopback IP avoids DNS and makes accidental external access
    # impossible.  Explicit zero impairments isolate 100% blackout behavior.
    return validate_config(dict(
        name=f"本机方向验证 {protocol} {direction}", scope="web", host=host,
        ports=str(port), protocol=protocol, direction=direction,
        blackout=True, blackout_mode="packet", blackout_loop=False,
        start_delay_s=0, blackout_duration_s=BLACKOUT_DURATION_S,
        duration_s=BLACKOUT_DURATION_S, delay_ms=0, jitter_ms=0, loss_pct=0,
        bandwidth_kbps=0, duplicate_pct=0, reorder_pct=0,
    ))


def message(label):
    return f"motu-{label}-{uuid.uuid4().hex}\n".encode("ascii")


class LocalPair:
    """An owned client and server; all I/O happens on the calling thread."""

    def __init__(self, protocol, host):
        self.protocol = protocol
        require(host in LOCALHOSTS, "Only literal IPv4/IPv6 localhost is allowed")
        self.host = host
        self.family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self.sockets = []
        self.client = None
        self.server = None
        self.server_port = None
        self.client_port = None
        try:
            if protocol == "tcp":
                listener = self._socket(socket.SOCK_STREAM)
                listener.bind((self.host, 0))
                listener.listen(1)
                self.server_port = listener.getsockname()[1]
                self.client = self._socket(socket.SOCK_STREAM)
                self.client.bind((self.host, 0))
                self.client.connect((self.host, self.server_port))
                self.server, peer = listener.accept()
                self.sockets.append(self.server)
                require(peer[0] == self.host, "TCP peer is not localhost")
                self.server.settimeout(1)
                self.client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                listener.close()
            elif protocol == "udp":
                self.server = self._socket(socket.SOCK_DGRAM)
                self.server.bind((self.host, 0))
                self.server_port = self.server.getsockname()[1]
                self.client = self._socket(socket.SOCK_DGRAM)
                self.client.bind((self.host, 0))
            else:
                raise ValueError("Unsupported protocol")
            self.client_port = self.client.getsockname()[1]
        except Exception:
            self.close()
            raise

    def _socket(self, kind):
        value = socket.socket(self.family, kind)
        self.sockets.append(value)
        value.settimeout(1)
        return value

    def send(self, direction, payload):
        source = self.client if direction == "outbound" else self.server
        destination_port = self.server_port if direction == "outbound" else self.client_port
        if self.protocol == "tcp":
            source.sendall(payload)
        else:
            sent = source.sendto(payload, (self.host, destination_port))
            require(sent == len(payload), "UDP send was incomplete")
        return dict(payload_id=payload.decode("ascii").strip(),
                    send_returned=True, delivery_proven=False)

    def receive(self, direction, expected, *, timeout=1, full_window=False):
        destination = self.server if direction == "outbound" else self.client
        expected_source_port = self.client_port if direction == "outbound" else self.server_port
        started = time.monotonic()
        deadline = started + timeout
        chunks = []
        datagrams = []
        data = b""
        result = dict(endpoint="server" if direction == "outbound" else "client",
                      expected_payload_id=expected.decode("ascii").strip(),
                      matched=False, timed_out=False, eof=False)
        previous_timeout = destination.gettimeout()
        try:
            while time.monotonic() < deadline:
                destination.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    if self.protocol == "tcp":
                        chunk = destination.recv(65535)
                        if not chunk:
                            result["eof"] = True
                            break
                    else:
                        chunk, peer = destination.recvfrom(65535)
                        require(peer[:2] == (self.host, expected_source_port),
                                "Received a datagram from an unexpected endpoint")
                        datagrams.append(chunk.decode("ascii", "backslashreplace"))
                    data += chunk
                    chunks.append(dict(after_s=round(time.monotonic() - started, 4),
                                       data=chunk.decode("ascii", "backslashreplace")))
                    result["matched"] = (expected in data if self.protocol == "tcp"
                                         else expected.decode("ascii") in datagrams)
                    if result["matched"] and not full_window:
                        break
                except socket.timeout:
                    result["timed_out"] = True
                    break
        finally:
            destination.settimeout(previous_timeout)
        result.update(bytes_received=len(data), chunks=chunks,
                      elapsed_s=round(time.monotonic() - started, 4))
        return result

    def exchange(self, label, direction, *, timeout=1):
        payload = message(label)
        sent = self.send(direction, payload)
        received = self.receive(direction, payload, timeout=timeout)
        return dict(direction=direction, send=sent, receive=received,
                    ok=received["matched"] and not received["eof"])

    def bidirectional(self, label, *, timeout=1):
        results = [self.exchange(label + "-upload", "outbound", timeout=timeout),
                   self.exchange(label + "-download", "inbound", timeout=timeout)]
        return dict(exchanges=results, ok=all(item["ok"] for item in results))

    def close(self):
        for item in reversed(self.sockets):
            item.close()
        return all(item.fileno() == -1 for item in self.sockets)


def active_snapshot(engine):
    snapshot = engine.snapshot()
    require(snapshot["running"] and snapshot["phase"] == "active",
            "Blackout expired or stopped before evidence was collected")
    require(snapshot["errors"] == 0, "Engine reported a packet error")
    return snapshot


def direction_case(protocol, direction, host, report, output):
    case = dict(name=f"{host} {protocol.upper()} {direction}: target and independent control",
                protocol=protocol, direction=direction, ok=False, error=None,
                host=host, ip_version=6 if host == "::1" else 4, skipped=False,
                localhost_only=True, control_bidirectional_ok=False,
                restored_bidirectional_ok=False)
    report["cases"].append(case)
    engine = Engine(ROOT / "vendor" / "windivert")
    pairs = []
    try:
        target = LocalPair(protocol, host)
        pairs.append(target)
        control = LocalPair(protocol, host)
        pairs.append(control)
        case.update(target_port=target.server_port, control_port=control.server_port,
                    target_client_port=target.client_port,
                    control_client_port=control.client_port)
        require(target.server_port != control.server_port, "Control port overlaps target")
        require(target.server_port != control.client_port, "Control client uses target port")
        case["baseline"] = dict(target=target.bidirectional("baseline-target"),
                                control=control.bidirectional("baseline-control"))
        require(all(value["ok"] for value in case["baseline"].values()),
                "Baseline failed; blocked delivery cannot be inferred")

        case["config"] = make_web_config(target.server_port, protocol, direction, host)
        save_report(output, report)
        engine.start(case["config"])
        case["stats_at_start"] = active_snapshot(engine)
        case["resolved_target"] = case["stats_at_start"].get("web_target")

        # A tiny reverse-direction payload fits the already established TCP
        # window, so a blocked ACK does not obscure whether data can arrive.
        reverse = "inbound" if direction == "outbound" else "outbound"
        case["opposite_direction"] = target.exchange("allowed-reverse", reverse)
        require(case["opposite_direction"]["ok"], "Opposite target direction was blocked")

        blocked_payload = message("blocked-unique")
        case["blocked_send"] = target.send(direction, blocked_payload)
        case["blocked_sends"] = [case["blocked_send"]]
        if protocol == "udp":
            for sequence in (2, 3):
                case["blocked_sends"].append(target.send(direction, message(f"blocked-sequence-{sequence}")))
        case["blocked_receive"] = target.receive(
            direction, blocked_payload, timeout=BLOCK_OBSERVATION_S, full_window=True)
        case["stats_after_blocked_receive"] = active_snapshot(engine)
        case["control_during_blackout"] = control.bidirectional("control-active")
        case["control_bidirectional_ok"] = case["control_during_blackout"]["ok"]
        case["stats"] = active_snapshot(engine)
        case["server_received_target"] = (
            case["blocked_receive"]["matched"] if direction == "outbound"
            else case["opposite_direction"]["receive"]["matched"])
        case["client_received_target"] = (
            case["blocked_receive"]["matched"] if direction == "inbound"
            else case["opposite_direction"]["receive"]["matched"])
        save_report(output, report)
        require(case["blocked_receive"]["bytes_received"] == 0
                and case["blocked_receive"]["timed_out"],
                "Selected direction received data during its blackout")
        require(case["control_bidirectional_ok"], "Independent control port was affected")
        require(case["stats"]["received"] > 0 and case["stats"]["blackout_dropped"] > 0,
                "Engine has no captured and blackout-dropped packet evidence")

        engine.stop()
        require(not engine.snapshot()["running"], "Engine is still running after stop")
        # Windows TCP may retransmit an application message queued during the
        # blackout.  Observe the *original* ID before sending new recovery data;
        # post-stop delivery is distinct from delivery while filtering is active.
        if protocol == "tcp":
            retransmit = target.receive(direction, blocked_payload, timeout=3)
            case["post_stop_original_payload"] = retransmit
            case["blocked_payload_delivered_after_stop"] = retransmit["matched"]
        else:
            case["blocked_payload_delivered_after_stop"] = None
        case["recovery"] = dict(target=target.bidirectional("recovery-target", timeout=5),
                                control=control.bidirectional("recovery-control"))
        case["restored_bidirectional_ok"] = all(value["ok"] for value in case["recovery"].values())
        require(case["restored_bidirectional_ok"], "Bidirectional traffic failed after stop")
        # A TCP retry may arrive together with the new recovery marker if its
        # retransmission timer was longer than the dedicated observation window.
        if protocol == "tcp" and not case["blocked_payload_delivered_after_stop"]:
            late = next(item for item in case["recovery"]["target"]["exchanges"]
                        if item["direction"] == direction)
            received_text = "".join(item["data"] for item in late["receive"]["chunks"])
            case["blocked_payload_delivered_after_stop"] = blocked_payload.decode("ascii") in received_text
        case["ok"] = True
    except Exception as exc:
        case.update(error=str(exc), traceback=traceback.format_exc())
        if (host == "::1" and not pairs and isinstance(exc, OSError)
                and (exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, 10047, 10049)
                     or getattr(exc, "winerror", None) in (10047, 10049))):
            case.update(skipped=True, skip_reason="IPv6 loopback is unavailable on this Windows host")
    finally:
        # Restore interception before closing sockets, including on assertions,
        # configuration rejection, socket timeout and interruption.
        try:
            engine.stop()
            case["stopped"] = not engine.snapshot()["running"]
            case["final_stats"] = engine.snapshot()
            case["engine_threads_stopped"] = not any(
                thread and thread.is_alive()
                for thread in (engine._receiver, engine._sender, engine._session))
        except Exception as exc:
            case.update(ok=False, cleanup_error=str(exc), stopped=False)
        closed = []
        for pair in reversed(pairs):
            try:
                closed.append(pair.close())
            except Exception as exc:
                closed.append(False)
                case.setdefault("socket_cleanup_errors", []).append(str(exc))
        case["sockets_closed"] = all(closed)
        if not (case.get("stopped") and case.get("engine_threads_stopped") and case["sockets_closed"]):
            case["ok"] = False
        save_report(output, report)
    return case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    report = dict(version=__version__, timestamp=datetime.now().astimezone().isoformat(),
                  localhost_only=True, network_adapter_changes=False,
                  cases=[], complete=False, passed=False)
    save_report(output, report)
    try:
        require(os.name == "nt", "This verification requires Windows")
        require(bool(ctypes.windll.shell32.IsUserAnAdmin()),
                "Windows administrator permission required; no network test was run")
        for host in LOCALHOSTS:
            for protocol in ("tcp", "udp"):
                for direction in ("outbound", "inbound"):
                    case = direction_case(protocol, direction, host, report, output)
                    if not case.get("stopped") or not case.get("engine_threads_stopped"):
                        raise RuntimeError("Engine cleanup failed; subsequent cases were not started")
        report["skipped_cases"] = sum(case["skipped"] for case in report["cases"])
        report["passed"] = len(report["cases"]) == 8 and all(case["ok"] for case in report["cases"])
    except KeyboardInterrupt:
        report["error"] = "Interrupted; current case cleanup was attempted"
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        report.update(complete=True, finished_at=datetime.now().astimezone().isoformat())
        save_report(output, report)
    print(json.dumps(dict(passed=report["passed"], output=str(output))))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
