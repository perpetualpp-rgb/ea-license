"""Throwaway PostgreSQL cluster for tests (initdb + pg_ctl in a temp dir, free TCP port).

PostgreSQL refuses to run as root; when the tests run as root the server processes are
started as the ``postgres`` OS user (or $FLOWLAB_PG_OS_USER) instead.
"""
from __future__ import annotations

import os
import pwd
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

CANDIDATE_BIN_DIRS = ["/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/17/bin", "/usr/local/pgsql/bin",
                      "/opt/homebrew/opt/postgresql@16/bin", "/usr/local/opt/postgresql@16/bin"]


class ClusterUnavailable(RuntimeError):
    """Raised with a human-readable reason when no cluster can be started."""


def find_bin_dir() -> Optional[Path]:
    env = os.environ.get("FLOWLAB_PG_BIN")
    candidates = [env] if env else []
    candidates += CANDIDATE_BIN_DIRS
    which = shutil.which("initdb")
    if which:
        candidates.append(str(Path(which).parent))
    for c in candidates:
        if c and (Path(c) / "initdb").exists() and (Path(c) / "pg_ctl").exists():
            return Path(c)
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PgCluster:
    def __init__(self, superuser: str = "flowlab_admin"):
        self.bin = find_bin_dir()
        if self.bin is None:
            raise ClusterUnavailable("PostgreSQL binaries (initdb, pg_ctl) not found; set FLOWLAB_PG_BIN")
        self.superuser = superuser
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix="flowlab-pg-"))
        self.data = self.dir / "data"
        self._demote: Optional[Callable[[], None]] = None
        if os.geteuid() == 0:
            name = os.environ.get("FLOWLAB_PG_OS_USER", "postgres")
            try:
                pw = pwd.getpwnam(name)
            except KeyError as e:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise ClusterUnavailable(f"running as root and OS user {name!r} does not exist "
                                         "(PostgreSQL will not run as root)") from e
            os.chown(self.dir, pw.pw_uid, pw.pw_gid)

            def demote(uid=pw.pw_uid, gid=pw.pw_gid):
                os.setgid(gid)
                os.setuid(uid)
            self._demote = demote

    def _run(self, *args: str) -> None:
        r = subprocess.run([str(a) for a in args], cwd=self.dir, preexec_fn=self._demote,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise ClusterUnavailable(f"{Path(args[0]).name} failed: {r.stderr.strip() or r.stdout.strip()}")

    def start(self) -> "PgCluster":
        self._run(self.bin / "initdb", "-D", self.data, "-U", self.superuser, "--auth=trust",
                  "-E", "UTF8", "--locale=C.UTF-8", "--no-sync")
        opts = (f"-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1 -c fsync=off "
                "-c synchronous_commit=off -c full_page_writes=off -c timezone=UTC")
        self._run(self.bin / "pg_ctl", "-D", self.data, "-o", opts, "-l", self.dir / "server.log", "-w", "-t", "60", "start")
        return self

    def dsn(self, user: Optional[str] = None, dbname: str = "postgres") -> str:
        return f"host=127.0.0.1 port={self.port} user={user or self.superuser} dbname={dbname}"

    def stop(self) -> None:
        try:
            if (self.data / "postmaster.pid").exists():
                self._run(self.bin / "pg_ctl", "-D", self.data, "-m", "fast", "-w", "stop")
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)
