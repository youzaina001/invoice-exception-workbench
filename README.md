# Invoice Triage

An Operator workbench for investigating incomplete supplier invoices with the
[Airflow governance core](https://github.com/youzaina001/airflow-dq-agent).
Review a PDF or upload a CSV, run checks against PostgreSQL, inspect the exception
case, submit a correction, and explicitly resolve it using fresh check evidence.

[Static walkthrough](site/index.html) · [Development report](site/report.html) ·
[Documentation](docs/README.md)

The static pages include synthetic PDF/text/CSV downloads and need no account
or backend. Their authored extraction scores are examples, not model accuracy.

## Quick start

Use Python 3.12+, `uv`, Make, and a checkout of the
[supported core revision](docs/local-development.md#tested-core-revision).
Run these commands from the repository root:

```bash
make setup CORE=/path/to/airflow-dq-agent
make db                                 # terminal 1: local PostgreSQL
```

In a second terminal, from the same directory:

```bash
make run                                # http://127.0.0.1:8101
```

Open the application and follow the
[PDF correction journey](docs/pdf-review.md#pdf-correction-and-resolution), or
upload `fixtures/missing_amount.csv`, run checks, upload
`fixtures/corrected_amount.csv`, run fresh checks, and select **Resolve**.

For upgrades, role settings and troubleshooting, see [local development](docs/local-development.md).

## Development checks

```bash
make browser-install
make check CORE=/path/to/airflow-dq-agent
```

The suite uses real PostgreSQL, Chromium and the installed core. Setup and checks
must select the same core checkout. See [verification](docs/agents/verification.md).

## Boundaries

- The default application is a localhost synthetic demo without sign-in.
- PDF review retains original files and extraction history in the local database;
  hosted PDF intake is disabled. CSV uploads retain normalized fields.
- Resolution requires passing checks for the current submission; it is not payment approval.
- The ordinary application and offline replay make no live extraction-model calls.
- Scanned PDFs are refused; OCR is unsupported. Held-out model accuracy and
  hosted application acceptance remain unverified.

Distribution: `invoice-triage`; import package: `invoice_triage`.
The repository name remains `invoice-exception-workbench`.

## Guides

| Guide | Details |
| --- | --- |
| [Documentation index](docs/README.md) | Navigation, domain terms and historical records |
| [Local development](docs/local-development.md) | Setup, upgrades, supported core and environment settings |
| [PDF review](docs/pdf-review.md) | Confirmation, correction, resolution and extraction limits |
| [Architecture](docs/architecture.md) | Data flow, restricted roles and evidence binding |
| [Experiments](docs/experiments.md) | Offline commands and finite model experiments |
| [Comparison](docs/COMPARISON.md) | Saved outputs, report meaning and provenance |
| [Hosted demo](docs/hosted-demo.md) | Invitation-only deployment preparation |
