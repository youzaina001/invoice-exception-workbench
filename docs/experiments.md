# Offline replay and finite model experiments

[Documentation](README.md) · [Project README](../README.md)

Run commands from the repository root after [local setup](local-development.md).
Offline replay needs no model account; live dispatch has the separate eligibility
and allowance requirements below.

## Offline development PDF replay

After setup, run the saved synthetic extraction results without a database or
model account:

```bash
.venv/bin/python -m invoice_triage.evaluation replay fixtures/extraction/development/manifest.json
.venv/bin/python -m invoice_triage.evaluation text fixtures/extraction/development/pdfs/18e73a9c.pdf
```

The bundle contains 13 development PDFs with opaque IDs, gold field states,
saved stub outputs and manually calculated [scoring examples](../fixtures/extraction/SCORING-EXAMPLES.md).
Expected exact-record counts are 8/13 for `baseline-stub` and 11/13 for
`model-stub`; these are authored examples, not observed model performance.
The command checks frozen PDF/text/output/prompt/schema/scorer hashes, reports
paired results, definite-field accuracy, state confusion and failures, and
makes no network calls. Edited artifacts or scoring require a new analysis.

This is the development portion of ticket #2. The ten independently curated
reserved documents and exposure separation have not been established; no
held-out score is available. The [local deterministic extractor and review flow](pdf-review.md) are separate from the authored stub comparison; they establish no live-model
performance result. The finite runner below is implemented; no live comparison
has been run. The package integration
and Invoice-ID refusal correction create a new scorer identity while retaining
the original PDF/gold/first-output artifacts. PDF conversion uses the pinned bounded adapter and never
truncates. Multiple-invoice detection checks line-anchored invoice IDs throughout
the document and repeated invoice sections within a page. Repeated same-ID
headers across pages are treated as continuation; other document structures need
additional fixtures. Blank pages retain their position. Image pages with no text
or only a 1–4 digit page number are refused as scans.
Time and memory limits are configured; adversarial exhaustion remains unverified.

## Finite model experiment (offline-first)

For offline raw-versus-confirmed results and the saved-output format, read
[Comparison and workflow replay](COMPARISON.md). After `make setup CORE=...`,
one command replays the frozen development examples through isolated PostgreSQL
and the pinned core, producing JSON and Markdown without model credentials:

```bash
.venv/bin/python -m invoice_triage.workflow_replay \
  fixtures/extraction/development/manifest.json \
  --plan fixtures/extraction/development/workflow.json \
  --workdir /tmp/invoice-replay-new --output /tmp/invoice-report-new.json
```

Use new work/output paths for each replay; existing state is never reset. The
cluster stops when the command ends; its evidence directory remains. The report
contains authored development examples, not a completed held-out live comparison.

The finite runner is implemented and tested with controlled SDK transport;
no live comparison has been run. The ordinary application and offline replay
do not call it. `google-genai==2.32.0` is pinned in the optional `model` extra
and included in development installs:

```bash
uv pip install --python .venv/bin/python -e '.[model]'
.venv/bin/python -m invoice_triage.model_runner audit
```

`audit` refuses missing or damaged allowance state. It never creates a new
allowance. Every worktree of this repository uses the same experiment directory
under its shared Git metadata: `invoice-triage-pdf-experiment-v1`. Keep that
directory and its adjacent identity seal; they survive task-worktree replacement.
There is no reset or retry command. The file lock protects this experiment's
processes, not unrelated use of the cloud account or deliberate owner tampering.

Before preparation, an evaluator supplies a frozen `Plan` and sanitized
`Eligibility` receipt. Their exact JSON schemas are available without credentials:

```bash
.venv/bin/python -c 'import json; from invoice_triage.model_runner import Plan, Eligibility; print(json.dumps({"plan": Plan.model_json_schema(), "eligibility": Eligibility.model_json_schema()}, indent=2))'
```

The plan names ten evaluation slots, three preselected scored-development slots,
one tuning slot and one preflight slot. Scored documents must be distinct. Each
slot binds its original PDF hash and the complete logical request hash, plus a
verified input-token bound of at most 12,000 including the prompt and schema.
Use the shared PDF adapter's canonical tagged text and the runner's
`request_payload`, `canonical` and `digest` functions to calculate that request
identity offline. Slot/scenario names and gold labels are never sent to the model.
Normal developer commands and tests must not open reserved documents; the
independent curator supplies their freeze/counting evidence separately.

A live receipt must be `user-verified`, dated and unexpired, and name the exact
GCP project, global Vertex AI route, proposed `gemini-3.5-flash-lite` model,
service/SKU, eligible positive credits, credit expiry and current prices. It
requires explicit boolean attestations for no cash spending and a provider-enforced
output bound covering billed reasoning and client disconnects. Evidence records
name primary provider URLs, snapshot hashes and the verified assertions; each
slot also has counting evidence for its exact request. These are scoped user
attestations: JSON validation and hashes cannot establish account eligibility or
prove the provider's billing semantics. A response limit alone is insufficient.
No positive live feasibility receipt is included. A `controlled-transport`
receipt is exclusively for offline tests and cannot authorize the default sender.

```bash
.venv/bin/python -m invoice_triage.model_runner prepare --plan frozen-plan.json --receipt eligibility-receipt.json
.venv/bin/python -m invoice_triage.model_runner audit
# Only after actual eligibility/accounting verification, in the evaluator's environment:
.venv/bin/python -m invoice_triage.model_runner send --slot preflight-0 --pdf synthetic-preflight.pdf
.venv/bin/python -m invoice_triage.model_runner replay --slot preflight-0
```

Preparation freezes all fifteen slots and reserves their entire worst-case batch
cost plus 25%, strictly below $4 and inside both eligible credit and the $5 ceiling.
Infeasible preparation retains its refusal reason and authorizes zero sends.
Dispatch holds the exclusive lock and durably reserves the slot and worst-case
charge before constructing the SDK client. The SDK uses one total transport
attempt, one candidate, minimal thinking, a finite 60-second transport timeout,
structured output, and disabled function calling and automatic continuation.
There is no semantic retry, alternate model or paid fallback. A timeout, failed
response or process interruption consumes the slot and keeps its possible charge;
evaluation is never resent.

The first response bytes are saved before envelope/result validation. Saved
provenance includes source/text/request/prompt/schema/code identities, requested
and returned model where available, timestamps, usage and failure type. Unknown
actual cost stays unavailable; the reserved worst-case charge is not a bill.
`replay` verifies the sealed state and first-response hashes without constructing
a provider client. An interrupted unfinished send remains reserved and refuses
replay/resend pending evidence inspection. These local proofs establish neither
the required live comparison nor held-out performance or billing eligibility.

SDK references: [official documentation](https://googleapis.github.io/python-genai/)
and [pinned release](https://pypi.org/project/google-genai/2.32.0/).
