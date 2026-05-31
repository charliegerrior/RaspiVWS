#!/usr/bin/env python3
"""
On-demand proxy for raspi-vws.

Listens on PUBLIC_PORT (8099). When the first client connects it starts
Raspi_VLC_Webcam_Stream.sh on INTERNAL_PORT (18099) and proxies all TCP
traffic transparently. When the last client disconnects and IDLE_TIMEOUT
seconds pass with no new connections, VLC is stopped. The Pi idles at
near-zero CPU between streams.
"""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

# ── tunables ──────────────────────────────────────────────────────────────────
PUBLIC_PORT      = 8099   # port clients connect to
INTERNAL_PORT    = 18099  # port VLC listens on internally
IDLE_TIMEOUT     = 30     # seconds after last client before stopping VLC
VLC_START_TIMEOUT = 20    # seconds to wait for VLC to bind INTERNAL_PORT
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'Raspi_VLC_Webcam_Stream.sh')

logging.basicConfig(
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_lock          = threading.Lock()
_vlc_proc      = None
_active_clients = 0
_idle_since    = None   # monotonic time of last client disconnect


# ── VLC lifecycle ─────────────────────────────────────────────────────────────

def _vlc_ready():
    try:
        s = socket.create_connection(('127.0.0.1', INTERNAL_PORT), timeout=0.3)
        s.close()
        return True
    except OSError:
        return False


def _start_vlc():
    """Start VLC if not already running. Returns True when VLC is ready."""
    global _vlc_proc, _idle_since
    if _vlc_proc is not None and _vlc_proc.poll() is None:
        return True

    log.info('Starting VLC on internal port %d', INTERNAL_PORT)
    _vlc_proc = subprocess.Popen(
        [SCRIPT, '0', str(INTERNAL_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _idle_since = None

    deadline = time.monotonic() + VLC_START_TIMEOUT
    while time.monotonic() < deadline:
        if _vlc_proc.poll() is not None:
            log.error('VLC process exited during startup (exit code %d)',
                      _vlc_proc.returncode)
            return False
        if _vlc_ready():
            log.info('VLC ready')
            return True
        time.sleep(0.25)

    log.error('Timed out waiting for VLC to start')
    _vlc_proc.terminate()
    return False


def _stop_vlc():
    global _vlc_proc, _idle_since
    if _vlc_proc is not None and _vlc_proc.poll() is None:
        log.info('Stopping VLC (idle for %ds)', IDLE_TIMEOUT)
        _vlc_proc.terminate()
        try:
            _vlc_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning('VLC did not exit cleanly; killing')
            _vlc_proc.kill()
    _vlc_proc = None
    _idle_since = None


# ── proxy ─────────────────────────────────────────────────────────────────────

def _forward(src: socket.socket, dst: socket.socket):
    """Pump bytes from src to dst until either side closes."""
    try:
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


def _handle_client(client_sock: socket.socket, addr):
    global _active_clients, _idle_since

    log.info('Client connected: %s:%d', addr[0], addr[1])

    with _lock:
        _active_clients += 1
        ok = _start_vlc()

    if not ok:
        try:
            client_sock.sendall(
                b'HTTP/1.0 503 Service Unavailable\r\n'
                b'Content-Type: text/plain\r\n\r\n'
                b'Stream unavailable - VLC failed to start.\n'
            )
        except OSError:
            pass
        client_sock.close()
        with _lock:
            _active_clients -= 1
            if _active_clients == 0:
                _idle_since = time.monotonic()
        return

    try:
        vlc_sock = socket.create_connection(('127.0.0.1', INTERNAL_PORT))
    except OSError as exc:
        log.error('Could not connect to VLC: %s', exc)
        client_sock.close()
        with _lock:
            _active_clients -= 1
            if _active_clients == 0:
                _idle_since = time.monotonic()
        return

    # Bidirectional proxy — each direction in its own thread.
    t1 = threading.Thread(target=_forward, args=(client_sock, vlc_sock), daemon=True)
    t2 = threading.Thread(target=_forward, args=(vlc_sock, client_sock), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    log.info('Client disconnected: %s:%d', addr[0], addr[1])
    with _lock:
        _active_clients -= 1
        if _active_clients == 0:
            _idle_since = time.monotonic()


# ── idle monitor ──────────────────────────────────────────────────────────────

def _idle_monitor():
    while True:
        time.sleep(5)
        with _lock:
            if (_active_clients == 0
                    and _idle_since is not None
                    and time.monotonic() - _idle_since >= IDLE_TIMEOUT):
                _stop_vlc()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(('', PUBLIC_PORT))
    except OSError as exc:
        log.error('Cannot bind to port %d: %s', PUBLIC_PORT, exc)
        sys.exit(1)
    server.listen(16)

    def _shutdown(sig, _frame):
        log.info('Shutting down (signal %d)', sig)
        with _lock:
            _stop_vlc()
        server.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(target=_idle_monitor, daemon=True).start()

    log.info('On-demand proxy listening on port %d (VLC on %d when active)',
             PUBLIC_PORT, INTERNAL_PORT)

    while True:
        try:
            client_sock, addr = server.accept()
        except OSError:
            break
        threading.Thread(
            target=_handle_client,
            args=(client_sock, addr),
            daemon=True,
        ).start()


if __name__ == '__main__':
    main()
