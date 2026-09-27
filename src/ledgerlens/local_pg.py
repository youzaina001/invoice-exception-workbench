"""Bundled PostgreSQL bound to 127.0.0.1 for local development and tests."""

from __future__ import annotations

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

    def dsn(self, *, user: str = "postgres", db: str = "postgres") -> str:
        return f"postgresql+psycopg://{user}@127.0.0.1:{self.port}/{db}"

    def start(self) -> None:
        self.pgdata.mkdir(parents=True, exist_ok=True)
        if not (self.pgdata / "PG_VERSION").exists():
            subprocess.run(
                [
                    postgres_binary("initdb"),
                    "--auth=trust",
                    "--encoding=utf8",
                    "-U",
                    "postgres",
                    "-D",
                    str(self.pgdata),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
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
            autocommit.execute(text(f'CREATE DATABASE "{name}"'))
