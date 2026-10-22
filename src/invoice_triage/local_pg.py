"""Bundled PostgreSQL bound to 127.0.0.1 for local development and tests."""

from __future__ import annotations

import secrets
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pgserver
from airflow_dq_agent.warehouse.db import make_engine
from sqlalchemy import text


def postgres_binary(name: str) -> str:
    exe = Path(pgserver.__file__).parent / "pginstall" / "bin" / name
    if not exe.is_file():
        raise RuntimeError(f"bundled postgres binary missing: {exe}")
    return str(exe)


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@dataclass
class LocalPostgres:
    pgdata: Path
    port: int

    def _stored_password(self) -> Path:
        return self.pgdata / "superuser_password"

    def _superuser_password(self) -> str:
        """Read the one password initdb wrote for this cluster.

        The value is persisted inside the data directory so a reused cluster
        keeps working; a cluster without it predates password auth.
        """
        stored = self._stored_password()
        if not stored.is_file():
            raise RuntimeError(
                f"no superuser password stored for existing cluster {self.pgdata};"
                " remove the data directory to reinitialize it"
            )
        return stored.read_text(encoding="utf-8").strip()

    def dsn(self, *, user: str = "postgres", db: str = "postgres") -> str:
        auth = f"{user}:{self._superuser_password()}"
        return f"postgresql+psycopg://{auth}@127.0.0.1:{self.port}/{db}"

    def start(self) -> None:
        fresh_cluster = not (self.pgdata / "PG_VERSION").exists()
        self.pgdata.mkdir(parents=True, exist_ok=True)
        if fresh_cluster:
            # initdb refuses a non-empty data directory, so the password file
            # it reads lives outside pgdata; the durable copy is written after.
            password = secrets.token_hex(16)
            pwfile = self.pgdata.with_name(f"{self.pgdata.name}.initdb-password")
            pwfile.write_text(password, encoding="utf-8")
            pwfile.chmod(0o600)
            try:
                subprocess.run(
                    [
                        postgres_binary("initdb"),
                        "--auth=scram-sha-256",
                        "--encoding=utf8",
                        "--pwfile",
                        str(pwfile),
                        "-U",
                        "postgres",
                        "-D",
                        str(self.pgdata),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            finally:
                pwfile.unlink(missing_ok=True)
            stored = self._stored_password()
            stored.write_text(password, encoding="utf-8")
            stored.chmod(0o600)
        subprocess.run(
            [
                postgres_binary("pg_ctl"),
                "-D",
                str(self.pgdata),
                "-l",
                str(self.pgdata / "log"),
                "-o",
                "-h 127.0.0.1",
                "-o",
                f"-p {self.port}",
                "-o",
                "-k ''",
                "-w",
                "-t",
                "60",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def stop(self) -> None:
        subprocess.run(
            [
                postgres_binary("pg_ctl"),
                "-D",
                str(self.pgdata),
                "-w",
                "-t",
                "60",
                "stop",
            ],
            capture_output=True,
            text=True,
        )

    def ensure_database(self, name: str) -> None:
        engine = make_engine(self.dsn(db="postgres"))
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if exists:
                return
        with engine.connect() as conn:
            autocommit = conn.execution_options(isolation_level="AUTOCOMMIT")
            quoted = str(
                autocommit.execute(
                    text("SELECT quote_ident(:name)"), {"name": name}
                ).scalar_one()
            )
            autocommit.execute(text(f"CREATE DATABASE {quoted}"))
