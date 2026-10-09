import sys
import os
import time
import logging
import subprocess
from logging.handlers import RotatingFileHandler
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

# How many seconds the filesystem must remain unchanged before we do a restart
STABILITY_THRESHOLD = 1  

# Logs: supervisor events go to logs/supervisor.log. The child's stdout/stderr are
# left on the terminal on purpose: Textual draws the UI on *stderr*, so redirecting
# it would send the whole display into a file. main.py logs its own crashes to
# logs/monitor.log.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
SUPERVISOR_LOG = os.path.join(LOG_DIR, "supervisor.log")

log = logging.getLogger("supervisor")
_h = RotatingFileHandler(SUPERVISOR_LOG, maxBytes=500_000, backupCount=2)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
log.addHandler(_h)
log.setLevel(logging.INFO)


# Directory names whose contents must never trigger a restart (pip installs touch
# thousands of .py files under .venv). Matched by path segment, because watchdog's
# ignore_patterns use PurePath.match, which only matches the trailing components,
# so a pattern like "*/.venv/*" silently misses anything nested deeper.
IGNORED_DIRS = {".venv", "venv", "__pycache__", ".git",
                "build", "dist", ".build", ".sparkle-tools"}  # packaging output


class PythonFileChangeHandler(PatternMatchingEventHandler):
    """
    A watchdog event handler that sets a 'change detected' flag anytime
    any .py file is created, modified, moved, or deleted.
    """
    def __init__(self, on_change_callback):
        super().__init__(
            patterns=["*.py"],       # match *.py files
            ignore_directories=False,
            case_sensitive=True
        )
        self.on_change_callback = on_change_callback

    def dispatch(self, event):
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if not raw:
                continue
            parts = os.fsdecode(raw).split(os.sep)
            if any(seg in IGNORED_DIRS for seg in parts):
                return
        super().dispatch(event)

    def on_any_event(self, event):
        # Trigger callback on any file system event involving *.py
        self.on_change_callback(event)

def main():
    # Decide which Python script to run as the child process
    # If user passes arguments, the first should be the script we spawn.
    # Otherwise, default to 'main.py'.
    if len(sys.argv) > 1:
        child_script = sys.argv[1]
        child_args = sys.argv[2:]
    else:
        child_script = "main.py"
        child_args = []

    # Warn if the child script doesn't actually exist
    if not os.path.isfile(child_script):
        print(f"Warning: {child_script} does not exist; attempting to run anyway.")

    child_process = None  # Store our running child process

    last_change_time = 0
    pending_restart = False

    def start_child():
        """Start the child process. It inherits the terminal for stdin/stdout/stderr."""
        print(f"\n[Supervisor] Starting child process: {child_script} {child_args}")
        proc = subprocess.Popen([sys.executable, child_script] + child_args)
        log.info("started %s %s pid=%s", child_script, child_args, proc.pid)
        return proc

    def stop_child(proc):
        """Stop the child process if it’s still running."""
        if proc and proc.poll() is None:  # None means it's still running
            print(f"\n[Supervisor] Stopping child process PID={proc.pid}")
            log.info("stopping pid=%s (SIGTERM)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("pid=%s ignored SIGTERM for 5s, killing", proc.pid)
                proc.kill()  # Force kill if it won't stop
        else:
            print("[Supervisor] No child process running or already exited.")

    def on_change(event):
        """
        We record that a change happened. We'll do the restart 
        once the file system remains stable for STABILITY_THRESHOLD seconds.
        """
        nonlocal last_change_time, pending_restart
        last_change_time = time.time()
        pending_restart = True
        print(f"[Supervisor] Detected change in {event.src_path}")
        log.info("change detected: %s %s", event.event_type, event.src_path)

    # ---------------------
    #  Set up file watcher
    # ---------------------
    event_handler = PythonFileChangeHandler(on_change)
    observer = Observer()
    # Monitor current directory (".") recursively
    observer.schedule(event_handler, path=".", recursive=True)
    observer.start()
    print(f"[Supervisor] Watching .py files under {os.path.abspath('.')}")
    log.info("supervisor started, watching %s, logs in %s", os.path.abspath('.'), LOG_DIR)

    # ---------------------
    #  Main supervision loop
    # ---------------------
    try:
        child_process = start_child()
        while True:
            time.sleep(0.5)

            # 1) Check if the child process has exited on its own
            if child_process.poll() is not None:
                rc = child_process.returncode
                print(f"[Supervisor] Child process exited with code {rc}")
                if rc == 0:
                    log.info("child exited cleanly (rc=0)")
                else:
                    log.error("child exited rc=%s (its traceback is in logs/monitor.log)", rc)
                print("[Supervisor] Exiting supervisor because the child process ended.")
                break

            # 2) Check if we need to restart due to file changes
            if pending_restart:
                elapsed = time.time() - last_change_time
                if elapsed >= STABILITY_THRESHOLD:
                    # Clear the flag first: a save that lands while we restart must
                    # schedule another restart rather than be lost.
                    pending_restart = False
                    stop_child(child_process)
                    child_process = start_child()

    except KeyboardInterrupt:
        print("\n[Supervisor] Caught keyboard interrupt, shutting down.")
        log.info("keyboard interrupt")

    finally:
        # Clean up watchdog observer and the child process before exiting
        observer.stop()
        observer.join()
        stop_child(child_process)
        print("[Supervisor] Supervisor shutting down.")
        log.info("supervisor shutting down")

if __name__ == "__main__":
    main()
