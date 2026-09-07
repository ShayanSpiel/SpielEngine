"""Repository-local lifecycle for the durable company runner process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


class RunnerService:
    def __init__(self, project_root: Path, db_path: Path):
        self.project_root = project_root
        self.db_path = Path(db_path)
        # The stop switch, pid file, and log all live beside the DATABASE,
        # matching how the runner derives its state directory
        # (automation_enabled(store.path.parent)). Deriving from the db path
        # keeps `company runner stop` effective for a custom --db too: the
        # flag is written and read at the same location, so for the default
        # db (.spielos/state/company.sqlite) the control files live in the
        # private state tree.
        self.state_dir = self.db_path.parent
        self.pid_path = self.state_dir / "runner.pid"
        self.log_path = self.state_dir / "runner.log"
        self.control_path = self.state_dir / "automation.json"

    def start(self, interval: float = 2.0) -> dict:
        self.enable()
        current = self.status()
        if current["running"]:
            return current
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.pid_path.exists():
            self.pid_path.unlink()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(self.project_root / ".agents")
        command = [sys.executable, "-B", "-m", "company", "--db", str(self.db_path),
                   "runner", "watch", "--interval", str(interval)]
        with self.log_path.open("a") as log:
            process = subprocess.Popen(command, cwd=self.project_root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.pid_path.write_text(json.dumps({"pid": process.pid, "command": command,
                                             "db_path": str(self.db_path)}) + "\n")
        return self.status()

    def stop(self) -> dict:
        current = self.status()
        if current["running"]:
            os.kill(current["pid"], signal.SIGTERM)
        if self.pid_path.exists():
            self.pid_path.unlink()
        self._set_enabled(False)
        return self.status()

    def enable(self) -> dict:
        self._set_enabled(True)
        return self.status()

    def status(self) -> dict:
        metadata = self._metadata()
        pid = metadata.get("pid")
        running = bool(pid and _alive(pid))
        started_at = None
        if running:
            # The pid file is rewritten on every runner start, so its mtime is
            # the launch time of the currently-alive local background process.
            # No new state file is introduced.
            try:
                started_at = datetime.fromtimestamp(self.pid_path.stat().st_mtime,
                                                    tz=timezone.utc).isoformat()
            except OSError:
                started_at = None
        return {"enabled": automation_enabled(self.state_dir),
                "running": running, "pid": pid if running else None,
                "started_at": started_at,
                "pid_path": str(self.pid_path), "log_path": str(self.log_path),
                "db_path": metadata.get("db_path", str(self.db_path))}

    def _set_enabled(self, enabled: bool) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.control_path.write_text(json.dumps({"enabled": enabled}) + "\n")

    def _metadata(self) -> dict:
        if not self.pid_path.exists():
            return {}
        try:
            value = json.loads(self.pid_path.read_text())
            value["pid"] = int(value["pid"])
            return value
        except (KeyError, ValueError, json.JSONDecodeError):
            return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def automation_enabled(state_dir: Path) -> bool:
    path = state_dir / "automation.json"
    if not path.exists():
        return True
    try:
        return bool(json.loads(path.read_text()).get("enabled", True))
    except (OSError, json.JSONDecodeError):
        return False
