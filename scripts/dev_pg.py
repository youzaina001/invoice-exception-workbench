"""Foreground embedded Postgres for the local M1 demo. Writes runtime DSNs to .env."""

from __future__ import annotations

import argparse
import signal
from pathlib import Path

from invoice_triage.db import setup_database
from invoice_triage.governance import provision_governance_schema
from invoice_triage.local_pg import LocalPostgres

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgdata", default=str(ROOT / ".pgdata"))
    parser.add_argument("--port", type=int, default=5433)
    args = parser.parse_args()

    pg = LocalPostgres(pgdata=Path(args.pgdata), port=args.port)
    pg.start()
    try:
        pg.ensure_database("warehouse")
        admin_wh = pg.dsn(db="warehouse")
        creds = provision_governance_schema(admin_wh)
        intake = setup_database(admin_wh)
        (ROOT / ".env").write_text(
            f"INTAKE_DSN={intake.intake_dsn}\n"
            f"READ_DSN={creds.read_dsn}\n"
            f"AUDIT_DSN={creds.audit_dsn}\n"
            "LLM_MODE=stub\nAPPLY_MODE=off\nTRACE_POSTGRES=true\nTRACES_DIR=traces\n",
            encoding="utf-8",
        )
        (ROOT / ".env.setup").write_text(
            f"# Setup-only admin DSN. Never used at runtime.\nADMIN_DSN={admin_wh}\n",
            encoding="utf-8",
        )
        print(
            f"Invoice Triage Postgres on 127.0.0.1:{args.port} (pgdata {args.pgdata})"
        )
        print("Runtime DSNs written to .env; admin DSN to .env.setup (setup-only).")
        print("Press Ctrl-C to stop.")
        signal.pause()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        pg.stop()


if __name__ == "__main__":
    main()
