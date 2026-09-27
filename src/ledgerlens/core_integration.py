"""Workbench registration against the installed governance core."""

from __future__ import annotations

from threading import Lock

from airflow_dq_agent.contracts.models import Dimension
from airflow_dq_agent.contracts.tables import (
    TABLE_CONTRACTS,
    ColumnContract,
    TableContract,
    register_contract,
)
from airflow_dq_agent.quality.registry import (
    CHECK_SPECS,
    CheckPolicy,
    CheckSpec,
    register_check,
)

STAGING_TABLE = "staging_invoice"
STAGING_SCHEMA = "wb"
AMOUNT_CHECK_ID = "staging_invoice.amount_minor.completeness"
_registration_lock = Lock()


def ensure_registered() -> None:
    """Register the staging contract and amount check once per process."""
    with _registration_lock:
        if STAGING_TABLE not in TABLE_CONTRACTS:
            register_contract(
                TableContract(
                    table=STAGING_TABLE,
                    schema_name=STAGING_SCHEMA,
                    grain="single row: the current submission",
                    description="Workbench-owned staging projection.",
                    primary_key=["invoice_ref"],
                    columns=[
                        ColumnContract(name="invoice_ref", dtype="utf8", unique=True),
                        ColumnContract(name="invoice_id", dtype="utf8"),
                        ColumnContract(name="supplier_id", dtype="utf8"),
                        ColumnContract(name="invoice_date", dtype="date"),
                        ColumnContract(name="currency", dtype="utf8"),
                        ColumnContract(
                            name="amount_minor", dtype="int64", nullable=True
                        ),
                    ],
                )
            )
        if AMOUNT_CHECK_ID not in CHECK_SPECS:
            register_check(
                CheckSpec(
                    check_id=AMOUNT_CHECK_ID,
                    table=STAGING_TABLE,
                    column="amount_minor",
                    dimension=Dimension.COMPLETENESS,
                    description="amount must be present",
                    policies=[CheckPolicy(action_id="quarantine_nulls")],
                )
            )
