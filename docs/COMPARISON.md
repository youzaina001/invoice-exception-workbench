# Saved extraction and confirmed workflow

[Documentation](README.md) · [Project README](../README.md)

Invoice Triage compares raw fields separately from what actually entered its
confirmed workflow. Synthetic development examples are **authored stubs**. Their
8/13 and 11/13 raw counts are scoring examples, not observed model performance.
The ten-document independently curated layout, exposure receipt, eligible-account
billing proof and actual live comparison remain incomplete. No real invoice data,
provider request or model credential is needed for offline replay.

Run the `invoice_triage.workflow_replay` command in
[Experiments](experiments.md#finite-model-experiment-offline-first) after
[local setup](local-development.md). It verifies the complete artifact
inventory, canonical PDF text, frozen scorer and workflow code before starting an
isolated PostgreSQL cluster. Every arm/document gets a fresh database. It never
connects to or resets the application's database. JSON and Markdown contain the
raw primary counts, paired results, field/state/failure breakdowns, actual workflow
outcomes, expected dispositions, wrong clearances, final all-five agreement and
timings. Equal raw counts are a tie; neither count establishes general superiority.
S10 is field fidelity, not a safety certification.

The declared policy confirms unchanged saved fields when the common gate allows
it. Missing/ambiguous required identities refuse intake. Missing/ambiguous amounts
can enter; actual core checks create their cases. A case can receive only the
predeclared correction PDF/output, followed by fresh explicit confirmation,
checks and resolution. Rejected inputs never receive manufactured cases. Labels
are parsed for scoring after the scripted actions finish. Expected-open and
expected-resolved groups remain separate; empty ratios are N/A. A wrongly guessed
amount can actually pass completeness; the report calls that an incorrect
clearance rather than treating confirmation as proof of invoice truth or payment
approval. Correctly processed submissions and correctly cleared submissions are
different counts.

The command measures scripted journey latency, not human review. Extraction
latency is available only when retained by the runner. No Operator observations
are included in the checked-in example: review time and time saved are unavailable.
Optional positive-duration observations identify document, arm and measurement
method; the method must define active time excluding unattended and supplier
correction wait. Test observations and fault injection are not actual observations.
Actual billed cost remains unavailable, including failed/uncertain sends; a reserved
maximum is not an invoice. Zero-success ratios are N/A, never zero cost per success.

## Import one saved extraction

The local PDF form accepts an optional saved JSON file beside the original PDF:

```json
{
  "version": "1",
  "kind": "offline-stub",
  "arm": "authored-example",
  "run_id": "my-frozen-analysis",
  "source_sha256": "<exact lowercase SHA-256 of the PDF bytes>",
  "result": {"version": "1", "status": "failure", "stage": "extract", "code": "saved_refusal"}
}
```

For successful output, `result` follows the existing five-field result schema.
`kind` is `offline-stub`, `controlled-transport`, or `saved-provider-output`.
Those metadata claims do not establish provider provenance. The upload checks the
exact source hash, rejects extra/duplicate keys and invalid evidence spans, and
caps saved JSON at 1 MiB. It cannot override PDF/document refusal. The original
PDF stays shared and immutable; each revision retains its distinct extraction
and binds it into confirmation. Edits keep that identity and require new
confirmation. Replacing the source returns to deterministic extraction and new
confirmation. Hosted PDF routes remain disabled. CSV parsing and its non-retention
of original upload bytes remain unchanged.

## Freeze a future first-output analysis

The original development PDFs, gold, prompt and first authored output bytes remain
unchanged. Extending the scorer creates a new analysis/scorer identity. Never
change a live allowance to fit changed code: its seal fails closed. No actual
allowance has been prepared by this work.

A future bundle's `manifest.json` declares `kind: first-outputs`, `scope: reserved`
(exactly ten evaluation slots) or `development` (the three preselected scored
development slots). It names the same schema, prompt, all source PDFs, two saved
arm mappings and their hashes. `run` names `state`, `seal`, all consumed slots'
`raw_files`, `model_arm`, `baseline_arm`, `deterministic_sha256` and one `layout`.
Copy the runner's state, adjacent seal and first raw responses into that bundle
without altering them. Archive every attempted role, including preflight/tuning,
so failed/uncertain spend provenance is retained. Missing and interrupted results
remain `not_sent` and `uncertain_send` failures, never retries. Shared adapter
refusals use empty pages and the canonical failure JSON SHA as `text_sha256`;
both arms retain that failure without a provider send.

Replay verifies slot/document/source/request identities, raw response hashes and
strict parsed results, returned version/usage, eligibility dates at dispatch,
maximum charges and the repository allowance seal. Deterministic results must
match the frozen extractor. It never constructs a provider client. Controlled
transport receipts remain labeled controlled; they do not become live evidence.
Hashes prove consistency, not independent curation, exposure or account truth.
The curator/exposure and pre-run freeze declarations remain human evidence and
must be retained before the reserved run.

`workflow.json` binds the manifest SHA, workflow code SHA and pinned core revision;
it names the fixed policy, expected disposition per document and any predeclared
correction PDF, both saved outputs, final gold and exact extra-file hash inventory.
`evidence_class` distinguishes `authored-example`, `fault-injection` and
`first-outputs`. The checked-in plan is a complete development example of this
format. Do not choose corrections from gold during replay or replace failures
after seeing scores.

The installed core is `ba683bb8343803771599f4d4287205a9b31fff0a`, called in-process
by the existing check path. Selected Airflow integration proof is separate from
full Airflow release readiness (#12). Local software verification, independent
review, publication, hosted acceptance and the presenter's own understanding are
separate states. The optional €0 hosting boundary is unchanged by the $5 eligible
extraction allowance. Nothing here establishes hosted PDF support or learner
mastery.
