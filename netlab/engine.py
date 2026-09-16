"""WinDivert based weak-network packet engine.

The packet transport is deliberately injectable so scheduling and policy can be
verified without a driver or administrator privileges.
"""
from __future__ import annotations

import ctypes
import heapq
import math
import os
import random
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

_ADDR_SIZE = 80
_OUTBOUND_WORD_OFFSET = 8
_OUTBOUND_BIT = 17
_IMPOSTOR_BIT = 19


def _address_flag(address: bytes | bytearray | memoryview, bit: int) -> bool:
    if len(address) < 12:
        return False
    word = struct.unpack_from("<I", address, _OUTBOUND_WORD_OFFSET)[0]
    return bool(word & (1 << bit))


def validate_filter(expression: str) -> str:
    """Validate a WinDivert filter before opening a handle."""
    if not isinstance(expression, str):
        raise ValueError("filter must be a string")
    expression = expression.strip()
    if not expression:
        raise ValueError("filter must not be empty")
    if len(expression) > 4096:
        raise ValueError("filter is too long")
    if "\\x00" in expression or any(ord(c) < 32 and c not in "\t\r\n" for c in expression):
        raise ValueError("filter contains control characters")
    depth = 0
    quote = None
    for c in expression:
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
    if quote or depth:
        raise ValueError("unbalanced filter expression")
    return expression


class _WinDivertTransport:
    """Small ctypes ABI adapter for WinDivert 2.x."""
    def __init__(self, dll_dir: Path):
        if os.name != "nt":
            raise OSError("WinDivert is only available on Windows")
        dll_dir = Path(dll_dir)
        names = ("WinDivert.dll", "windivert.dll")
        dll = next((dll_dir / n for n in names if (dll_dir / n).exists()), None)
        if dll is None:
            raise FileNotFoundError(f"WinDivert.dll not found in {dll_dir}")
        self.dll = ctypes.WinDLL(str(dll), use_last_error=True)
        self.open_fn = self.dll.WinDivertOpen
        self.open_fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_short, ctypes.c_uint64]
        self.open_fn.restype = ctypes.c_void_p
        self.recv_fn = self.dll.WinDivertRecv
        self.recv_fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.recv_fn.restype = ctypes.c_int
        self.send_fn = self.dll.WinDivertSend
        self.send_fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.send_fn.restype = ctypes.c_int
        self.close_fn = self.dll.WinDivertClose
        self.close_fn.argtypes = [ctypes.c_void_p]
        self.close_fn.restype = ctypes.c_int
        self.shutdown_fn = getattr(self.dll, "WinDivertShutdown", None)
        if self.shutdown_fn:
            self.shutdown_fn.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            self.shutdown_fn.restype = ctypes.c_int
        self.compile_fn = getattr(self.dll, "WinDivertHelperCompileFilter", None)
        if self.compile_fn:
            # BOOL WinDivertHelperCompileFilter(
            #   LPCSTR filter, WINDIVERT_LAYER layer, PVOID object,
            #   UINT objLen, const char **errorStr, UINT *errorPos);
            self.compile_fn.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                        ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.POINTER(ctypes.c_char_p),
                                        ctypes.POINTER(ctypes.c_uint)]
            self.compile_fn.restype = ctypes.c_int
        self.checksum_fn = getattr(self.dll, "WinDivertHelperCalcChecksums", None)
        if self.checksum_fn:
            self.checksum_fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint64]
            self.checksum_fn.restype = ctypes.c_int

    def open(self, expression: str):
        # NETWORK layer is 0, priority 0, no flags.
        h = self.open_fn(expression.encode("ascii"), 0, 0, 0)
        if not h or h == ctypes.c_void_p(-1).value:
            raise OSError(f"WinDivertOpen failed ({ctypes.get_last_error()})")
        return h

    def recv(self, handle):
        packet = ctypes.create_string_buffer(40 + 65535)
        length = ctypes.c_uint(0)
        address = ctypes.create_string_buffer(_ADDR_SIZE)
        if not self.recv_fn(handle, packet, len(packet), ctypes.byref(length), address):
            raise OSError(f"WinDivertRecv failed ({ctypes.get_last_error()})")
        return bytes(packet.raw[: length.value]), bytes(address.raw)

    def send(self, handle, packet: bytes, address: bytes):
        p = ctypes.create_string_buffer(packet)
        a = ctypes.create_string_buffer(address, _ADDR_SIZE)
        sent = ctypes.c_uint(0)
        if self.checksum_fn:
            if not self.checksum_fn(p, len(packet), a, 0):
                raise OSError(f"WinDivertHelperCalcChecksums failed ({ctypes.get_last_error()})")
        if not self.send_fn(handle, p, len(packet), ctypes.byref(sent), a):
            raise OSError(f"WinDivertSend failed ({ctypes.get_last_error()})")
        return sent.value

    def close(self, handle):
        if handle:
            self.close_fn(handle)

    def shutdown(self, handle):
        if handle and self.shutdown_fn:
            # Unblock both WinDivertRecv and any pending send operation before
            # closing the handle.  Closing only the receive side can leave a
            # worker blocked briefly, which in turn keeps the WinDivert driver
            # service (and WinDivert64.sys) loaded during an upgrade.
            self.shutdown_fn(handle, 3)  # WINDIVERT_SHUTDOWN_RECV | SEND

    def validate_filter(self, expression: str):
        if not self.compile_fn:
            return
        obj = ctypes.create_string_buffer(4096)
        error_str = ctypes.c_char_p()
        pos = ctypes.c_uint(0)
        if not self.compile_fn(expression.encode("ascii"), 0, obj, len(obj),
                               ctypes.byref(error_str), ctypes.byref(pos)):
            detail = error_str.value.decode("utf-8", "replace") if error_str.value else "invalid filter"
            raise ValueError(f"{detail} at position {pos.value}")


class Engine:
    def __init__(self, dll_dir: Path, *, transport: Any = None,
                 clock: Callable[[], float] | None = None,
                 rng: random.Random | Any | None = None,
                 max_queue: int = 4096, max_queue_bytes: int = 64 * 1024 * 1024):
        self.dll_dir = Path(dll_dir)
        self._transport = transport
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self.max_queue = max(1, int(max_queue))
        self.max_queue_bytes = max(1, int(max_queue_bytes))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._heap: list[tuple[float, int, bytes, bytes, bool]] = []
        self._queued_bytes = 0
        self._seq = 0
        self._handle = None
        self._receiver = None
        self._sender = None
        self._session = None
        self._active_started_at = 0.0
        self._wait_started_at = 0.0
        self._round_stop = threading.Event()
        self._stop_event = threading.Event()
        self._closed = False
        self._config: dict[str, Any] = {}
        self._started_at = 0.0
        self._stats = self._new_stats()
        self._rate_next = {False: 0.0, True: 0.0}

    def _close_transport_handle(self, handle):
        """Stop I/O and close a WinDivert handle, tolerating teardown races.

        All shutdown paths go through this helper.  In particular, a stop
        racing with ``WinDivertOpen`` and a receive-loop failure must perform
        the same shutdown call as the normal stop path so the kernel driver is
        released before the process exits or an installer replaces files.
        """
        if handle is None:
            return True
        transport = self._transport
        try:
            shutdown = getattr(transport, "shutdown", None)
            if shutdown:
                shutdown(handle)
        except Exception:
            # Teardown is best effort; still call close below.
            pass
        try:
            close = getattr(transport, "close", None)
            if close:
                close(handle)
        except Exception as exc:
            self._record_error(exc)
            return False
        return True

    @staticmethod
    def _new_stats() -> dict[str, Any]:
        return {"running": False, "phase": "idle", "cycle": 0, "wait_remaining": 0.0,
                "active_elapsed": 0.0, "active_remaining": None,
                "received": 0, "sent": 0, "dropped": 0,
                "duplicated": 0, "reordered": 0, "queued": 0,
                "bytes_received": 0, "bytes_sent": 0, "errors": 0,
                "error": None, "elapsed": 0.0}

    def validate_filter(self, expression: str) -> str:
        value = validate_filter(expression)
        transport = self._transport
        if transport is None and os.name == "nt":
            transport = _WinDivertTransport(self.dll_dir)
            self._transport = transport
        checker = getattr(transport, "validate_filter", None)
        if checker:
            checker(value)
        return value

    def start(self, config: dict):
        if not isinstance(config, dict):
            raise ValueError("config must be a dict")
        cfg = dict(config)
        # Keep track of whether the new blackout fields were supplied.  Older
        # callers only provided ``blackout`` and ``duration_s``; in that form
        # the regular duration remains the active blackout duration.
        has_blackout_duration = "blackout_duration_s" in cfg
        self.validate_filter(cfg.get("filter", ""))
        defaults = {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0,
                    "bandwidth_kbps": 0.0, "duplicate_pct": 0.0,
                    "reorder_pct": 0.0, "reorder_ms": 0.0,
                    "duration_s": 0, "blackout": False, "blackout_duration_s": 0,
                    "start_delay_s": 0, "blackout_loop": False}
        for k, v in defaults.items(): cfg.setdefault(k, v)
        if not isinstance(cfg["blackout_loop"], bool):
            raise ValueError("blackout_loop must be a bool")
        if cfg["blackout_loop"] and cfg.get("blackout") is not True:
            raise ValueError("blackout_loop requires blackout")
        for k in ("delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps", "duplicate_pct", "reorder_pct", "reorder_ms", "duration_s", "start_delay_s", "blackout_duration_s"):
            if isinstance(cfg[k], bool): raise ValueError(f"invalid {k}")
            try:
                if k in ("duration_s", "start_delay_s", "blackout_duration_s"):
                    if isinstance(cfg[k], float) and not cfg[k].is_integer(): raise ValueError
                    cfg[k] = int(cfg[k])
                else: cfg[k] = float(cfg[k])
            except (TypeError, ValueError, OverflowError): raise ValueError(f"invalid {k}")
            if not math.isfinite(cfg[k]): raise ValueError(f"invalid {k}")
            if cfg[k] < 0: raise ValueError(f"{k} must be non-negative")
        # The UI keeps the last blackout values while the checkbox is off.
        # Ignore them in that state without changing the user's saved form.
        if not cfg.get("blackout"):
            cfg["start_delay_s"] = 0
            cfg["blackout_duration_s"] = 0
        if cfg["start_delay_s"] > 86400: raise ValueError("start_delay_s must be <= 86400")
        if cfg.get("blackout") and has_blackout_duration:
            cfg["duration_s"] = cfg["blackout_duration_s"]
        if cfg["blackout_loop"] and (cfg["start_delay_s"] < 1 or cfg["duration_s"] < 1):
            raise ValueError("blackout_loop requires start_delay_s and blackout duration >= 1")
        if cfg["start_delay_s"] and cfg["duration_s"] < 1:
            raise ValueError("duration_s must be >= 1 with start_delay_s")
        for k in ("loss_pct", "duplicate_pct", "reorder_pct"):
            if cfg[k] > 100: raise ValueError(f"{k} must be <= 100")
        with self._condition:
            if self._stats["running"] or any(t and t.is_alive() for t in (self._receiver, self._sender, self._session)):
                raise RuntimeError("engine already running")
            self._config = cfg
            self._stats = self._new_stats()
            self._heap.clear(); self._queued_bytes = 0; self._seq = 0
            self._rate_next = {False: 0.0, True: 0.0}
            self._stop_event.clear(); self._closed = False
            self._started_at = self._clock()
            self._wait_started_at = self._started_at
            self._active_started_at = 0.0
            self._round_stop = threading.Event()
            self._stats["running"] = True
            self._stats["cycle"] = 1
            self._stats["phase"] = "waiting" if cfg["start_delay_s"] else "active"
            if self._transport is None:
                self._transport = _WinDivertTransport(self.dll_dir)
            if cfg["start_delay_s"]:
                target = self._blackout_loop_session if cfg["blackout_loop"] else self._delayed_activate
                self._session = threading.Thread(target=target, name="netlab-session", daemon=True)
                self._session.start()
                return
            self._activate_now(raise_on_error=True)

    def _activate_now(self, raise_on_error=False):
        try:
            expr = f"({self._config['filter']}) and !impostor"
            handle = self._transport.open(expr)
            with self._condition:
                if self._stop_event.is_set():
                    self._close_transport_handle(handle)
                    return False
                self._handle = handle
                self._closed = False
                self._round_stop = threading.Event()
                self._active_started_at = self._clock()
                self._stats["phase"] = "active"
                self._stats["wait_remaining"] = 0.0
                self._receiver = threading.Thread(target=self._receive_loop,
                                                  args=(handle, self._round_stop),
                                                  name="netlab-recv", daemon=True)
                # A looping blackout only drops packets.  Its session thread
                # owns the deadline so a normal sender timeout cannot stop the
                # complete session after the first outage.
                self._sender = None if self._config["blackout_loop"] else threading.Thread(
                    target=self._send_loop, name="netlab-send", daemon=True)
                self._receiver.start()
                if self._sender:
                    self._sender.start()
            return True
        except Exception as exc:
            if not self._stop_event.is_set():
                self._request_failure(exc)
            if raise_on_error: raise
            return False

    def _wait_until(self, deadline):
        with self._condition:
            while not self._stop_event.is_set():
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=min(remaining, 0.25))
            return False

    def _blackout_loop_session(self):
        """Repeat connected wait / outage, releasing each handle in between."""
        try:
            while not self._stop_event.is_set():
                if not self._wait_until(self._wait_started_at + self._config["start_delay_s"]):
                    return
                if not self._activate_now():
                    return
                if not self._wait_until(self._active_started_at + self._config["duration_s"]):
                    return
                with self._condition:
                    self._round_stop.set()
                    handle, receiver = self._handle, self._receiver
                    if handle is not None and not self._closed:
                        self._closed = True
                        if not self._close_transport_handle(handle):
                            raise RuntimeError("无法关闭断网句柄，已停止循环")
                    self._handle = None
                # Do not let a previous receive call touch a later handle or
                # report its expected shutdown exception against a new round.
                if receiver:
                    receiver.join(timeout=2.0)
                    if receiver.is_alive():
                        raise RuntimeError("断网接收线程未退出，已停止循环")
                with self._condition:
                    if self._stop_event.is_set():
                        return
                    self._clear_queue_locked()
                    self._wait_started_at = self._clock()
                    self._active_started_at = 0.0
                    self._stats["cycle"] += 1
                    self._stats["phase"] = "waiting"
                    self._stats["active_elapsed"] = 0.0
                    self._stats["active_remaining"] = None
                    self._condition.notify_all()
        except Exception as exc:
            if not self._stop_event.is_set():
                self._request_failure(exc)

    def _delayed_activate(self):
        deadline = self._started_at + self._config.get("start_delay_s", 0)
        with self._condition:
            while not self._stop_event.is_set():
                rem = deadline - self._clock()
                if rem <= 0: break
                self._stats["wait_remaining"] = rem
                self._condition.wait(timeout=min(rem, 0.25))
            if self._stop_event.is_set(): return
            self._stats["wait_remaining"] = 0.0
        self._activate_now(False)

    def stop(self):
        with self._condition:
            workers = (self._receiver, self._sender, self._session)
            if not self._stats["running"] and self._handle is None and not any(t and t.is_alive() for t in workers):
                self._clear_queue_locked(); return
            self._stop_event.set(); self._round_stop.set(); self._condition.notify_all()
            handle = self._handle
            if handle is not None and not self._closed:
                self._closed = True
                self._close_transport_handle(handle)
        current = threading.current_thread()
        for t in (self._receiver, self._sender, self._session):
            if t and t is not current: t.join(timeout=2.0)
        with self._condition:
            self._handle = None
            self._clear_queue_locked()
            self._stats["running"] = False
            if self._stats.get("phase") not in ("error",):
                self._stats["phase"] = "stopped"
            self._stats["wait_remaining"] = 0.0
            self._stats["active_elapsed"] = max(0.0, self._clock() - self._active_started_at) if self._active_started_at else 0.0
            self._stats["active_remaining"] = None
            self._stats["elapsed"] = max(0.0, self._clock() - self._started_at) if self._started_at else 0.0
            self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._condition:
            snap = dict(self._stats)
            snap["queued"] = len(self._heap)
            if self._stats["running"]:
                snap["elapsed"] = max(0.0, self._clock() - self._started_at) if self._started_at else 0.0
                if snap.get("phase") == "waiting":
                    snap["wait_remaining"] = max(0.0, self._wait_started_at + self._config.get("start_delay_s", 0) - self._clock())
                elif snap.get("phase") == "active":
                    snap["active_elapsed"] = max(0.0, self._clock() - self._active_started_at)
                    d = self._config.get("duration_s", 0)
                    snap["active_remaining"] = max(0.0, self._active_started_at + d - self._clock()) if d else None
            return snap

    def _record_error(self, exc: BaseException):
        with self._condition:
            self._stats["errors"] += 1
            self._stats["error"] = str(exc)

    def _clear_queue_locked(self):
        self._heap.clear(); self._queued_bytes = 0

    def _request_failure(self, exc: BaseException):
        self._record_error(exc)
        self._stop_event.set()
        self._round_stop.set()
        with self._condition:
            self._condition.notify_all()
        with self._condition:
            handle = self._handle
            should_close = handle is not None and not self._closed
            if should_close:
                self._closed = True
        if should_close:
            self._close_transport_handle(handle)
        with self._condition:
            self._handle = None
            self._clear_queue_locked()
            self._stats["running"] = False
            self._stats["phase"] = "error"
            self._stats["wait_remaining"] = 0.0
            self._stats["active_remaining"] = None
            self._stats["elapsed"] = max(0.0, self._clock() - self._started_at) if self._started_at else 0.0
            self._condition.notify_all()

    def _receive_loop(self, handle, round_stop):
        while not self._stop_event.is_set() and not round_stop.is_set():
            try:
                item = self._transport.recv(handle)
                if self._stop_event.is_set() or round_stop.is_set():
                    break
                if item is None: continue
                packet, address = self._normalise_recv(item)
                if _address_flag(address, _IMPOSTOR_BIT):
                    continue
                self._stats_add_received(len(packet))
                self._process_packet(bytes(packet), bytes(address))
            except Exception as exc:
                with self._condition:
                    if not self._stop_event.is_set() and not round_stop.is_set():
                        self._request_failure(exc)
                break

    @staticmethod
    def _normalise_recv(item):
        if isinstance(item, dict): return item["packet"], item["address"]
        if isinstance(item, tuple) and len(item) == 2: return item
        if isinstance(item, tuple) and len(item) == 3: return item[1], item[2]
        raise ValueError("transport.recv must return (packet, address)")

    def _stats_add_received(self, size):
        with self._condition:
            self._stats["received"] += 1; self._stats["bytes_received"] += size

    def _chance(self, pct):
        return pct > 0 and self._rng.random() * 100.0 < pct

    def _process_packet(self, packet: bytes, address: bytes):
        cfg = self._config
        if cfg["blackout"] or self._chance(cfg["loss_pct"]):
            with self._condition: self._stats["dropped"] += 1
            return
        outbound = _address_flag(address, _OUTBOUND_BIT)
        base = self._clock() + max(0.0, cfg["delay_ms"] + self._rng.uniform(-cfg["jitter_ms"], cfg["jitter_ms"])) / 1000.0
        if self._chance(cfg["reorder_pct"]):
            base += cfg["reorder_ms"] / 1000.0
            with self._condition: self._stats["reordered"] += 1
        self._enqueue(packet, address, base, outbound)
        if self._chance(cfg["duplicate_pct"]):
            with self._condition: self._stats["duplicated"] += 1
            self._enqueue(packet, address, base, outbound)

    def _enqueue(self, packet, address, ready, outbound):
        with self._condition:
            if self._stop_event.is_set() or not self._stats["running"]:
                self._stats["dropped"] += 1; return
            if len(self._heap) >= self.max_queue or self._queued_bytes + len(packet) > self.max_queue_bytes:
                self._stats["dropped"] += 1; return
            rate = self._config.get("bandwidth_kbps", 0.0)
            if rate > 0:
                ready = max(ready, self._rate_next[outbound])
                self._rate_next[outbound] = ready + len(packet) / (rate * 1024.0)
            self._seq += 1
            heapq.heappush(self._heap, (ready, self._seq, packet, address, outbound))
            self._queued_bytes += len(packet); self._condition.notify_all()

    def _send_loop(self):
        while not self._stop_event.is_set():
            timed_out = False
            with self._condition:
                while not self._heap and not self._stop_event.is_set():
                    remaining = self._remaining_duration()
                    if self._duration_expired():
                        self._stop_event.set()
                        self._condition.notify_all()
                        break
                    self._condition.wait(timeout=remaining)
                if self._duration_expired():
                    self._stop_event.set()
                    self._condition.notify_all()
                    timed_out = True
                if timed_out:
                    pass
                elif self._stop_event.is_set(): break
                else:
                    ready, _, packet, address, _ = self._heap[0]
                    wait = ready - self._clock()
                    if wait > 0:
                        self._condition.wait(timeout=min(wait, self._remaining_duration())); continue
                    heapq.heappop(self._heap); self._queued_bytes -= len(packet)
            if timed_out:
                self.stop()
                break
            try:
                sent = self._transport.send(self._handle, packet, address)
                with self._condition:
                    self._stats["sent"] += 1; self._stats["bytes_sent"] += int(sent if sent is not None else len(packet))
            except Exception as exc:
                if not self._stop_event.is_set(): self._request_failure(exc)
                break
            if self._duration_expired():
                # Complete the normal shutdown path so the handle is closed and
                # snapshots report running=False after an automatic timeout.
                self.stop()
                break

    def _remaining_duration(self):
        d = self._config.get("duration_s", 0)
        if not d: return 0.25
        if not self._active_started_at: return 0.25
        return max(0.0, self._active_started_at + d - self._clock())

    def _duration_expired(self):
        d = self._config.get("duration_s", 0)
        return bool(d and self._active_started_at and self._clock() >= self._active_started_at + d)
