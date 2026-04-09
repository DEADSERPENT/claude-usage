"""
daemon.py - Background daemon service for claude-usage.

Runs the scanner as a persistent background process that automatically
monitors JSONL files and updates the database. Can run alongside the
dashboard or independently.

Supports:
- Start/stop/status lifecycle
- PID file management for single-instance enforcement
- Configurable scan interval
- Log file for diagnostics
- Automatic anomaly detection after each scan cycle
"""

import os
import sys
import time
import signal
import threading
from datetime import datetime
from pathlib import Path

from config import SCAN_INTERVAL_SECS, DAEMON_PID_FILE, DAEMON_LOG_FILE


class DaemonLogger:
    """Simple file logger for the daemon process."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._ensure_dir()

    def _ensure_dir(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def info(self, msg):  self.log("INFO", msg)
    def warn(self, msg):  self.log("WARN", msg)
    def error(self, msg): self.log("ERROR", msg)


def _read_pid() -> int | None:
    """Read PID from the PID file, return None if not found or stale."""
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        pid = int(DAEMON_PID_FILE.read_text(encoding="utf-8").strip())
        # Check if process actually exists (cross-platform)
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return pid
            return None
        else:
            os.kill(pid, 0)
            return pid
    except (ValueError, OSError, PermissionError):
        return None


def _write_pid():
    DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid():
    try:
        DAEMON_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def is_running() -> dict:
    """Check if the daemon is currently running."""
    pid = _read_pid()
    if pid is not None:
        return {"running": True, "pid": pid}
    return {"running": False, "pid": None}


def start(foreground: bool = False, interval: int = None):
    """
    Start the daemon process.

    If foreground=True, runs in the current process (blocking).
    If foreground=False, spawns a background subprocess.
    """
    if interval is None:
        interval = SCAN_INTERVAL_SECS

    status = is_running()
    if status["running"]:
        print(f"Daemon already running (PID {status['pid']})")
        return status

    if foreground:
        return _run_foreground(interval)
    else:
        return _run_background(interval)


def _run_background(interval: int) -> dict:
    """Spawn a detached background process."""
    import subprocess

    # Start a new Python process running this module in foreground mode
    cmd = [sys.executable, __file__, "--foreground", "--interval", str(interval)]

    if sys.platform == "win32":
        # Windows: use CREATE_NO_WINDOW flag
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Give it a moment to write PID file
    time.sleep(1)
    return {"running": True, "pid": proc.pid}


def _run_foreground(interval: int) -> dict:
    """Run the daemon in the current process (blocking)."""
    logger = DaemonLogger(DAEMON_LOG_FILE)
    _write_pid()
    pid = os.getpid()

    logger.info(f"Daemon started (PID {pid}, interval {interval}s)")
    print(f"Daemon started (PID {pid})")
    print(f"Scanning every {interval}s. Press Ctrl+C to stop.")

    _shutdown = threading.Event()

    def _signal_handler(signum, frame):
        logger.info("Received shutdown signal")
        _shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    scan_count = 0

    try:
        while not _shutdown.is_set():
            try:
                from scanner import scan
                result = scan(verbose=False)
                scan_count += 1

                turns = result.get("turns", 0)
                new = result.get("new", 0)
                updated = result.get("updated", 0)

                if turns > 0 or new > 0 or updated > 0:
                    logger.info(f"Scan #{scan_count}: {turns} turns, "
                                f"{new} new files, {updated} updated")
            except Exception as e:
                logger.error(f"Scan error: {e}")

            _shutdown.wait(timeout=interval)
    except KeyboardInterrupt:
        pass
    finally:
        _remove_pid()
        logger.info(f"Daemon stopped after {scan_count} scans")
        print(f"\nDaemon stopped (ran {scan_count} scan cycles)")

    return {"running": False, "pid": pid, "scans": scan_count}


def _terminate_process_windows(pid: int) -> bool:
    """Terminate a process by PID on Windows without shelling out."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        PROCESS_SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False
            kernel32.WaitForSingleObject(handle, 5000)
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def stop() -> dict:
    """Stop the running daemon."""
    status = is_running()
    if not status["running"]:
        _remove_pid()
        return {"stopped": False, "message": "Daemon is not running"}

    pid = status["pid"]
    try:
        if sys.platform == "win32":
            if not _terminate_process_windows(pid):
                # Fallback for environments where Windows API calls are restricted.
                import subprocess
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, check=True)
        else:
            os.kill(pid, signal.SIGTERM)
            # Wait for graceful shutdown
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break

        _remove_pid()
        return {"stopped": True, "pid": pid}
    except Exception as e:
        return {"stopped": False, "message": str(e)}


def get_log(lines: int = 50) -> list[str]:
    """Read the last N lines from the daemon log."""
    if not DAEMON_LOG_FILE.exists():
        return []
    try:
        all_lines = DAEMON_LOG_FILE.read_text(encoding="utf-8").splitlines()
        return all_lines[-lines:]
    except Exception:
        return []


# Allow running as a standalone script for background spawning
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--interval", type=int, default=SCAN_INTERVAL_SECS)
    args = parser.parse_args()

    if args.foreground:
        _run_foreground(args.interval)
    else:
        result = start(foreground=False, interval=args.interval)
        if result.get("running"):
            print(f"Daemon started in background (PID {result['pid']})")
