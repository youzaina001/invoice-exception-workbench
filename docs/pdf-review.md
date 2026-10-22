# Local PDF review and correction

[Documentation](README.md) · [Project README](../README.md)

Use [local setup](local-development.md#setup-and-demo) with the supported core
revision. Restart the local `make db` process to apply the additive PDF tables,
then restart `make run`. Do not reuse
an application process with old generated role settings.

1. Open `http://127.0.0.1:8101` and choose **PDF file** under **Review supplier PDF**.
   Use [the synthetic valid PDF](../fixtures/pdf_intake/valid.pdf) or
   [the zero-amount PDF](../fixtures/pdf_intake/zero.pdf).
2. Select **Review PDF**. Inspect the original page text, download the original
   file if needed, and compare each extracted field with its original page evidence.
3. Edit any reviewed fields and select **Save reviewed fields**. The original
   source and raw extraction stay unchanged. Amounts use decimal units, such as
   `12.34`; money is stored and checked exactly as integer cents.
4. Select **Confirm reviewed PDF**, then **Submit confirmed PDF**. Editing fields
   or replacing the source invalidates the earlier confirmation. Intake rechecks
   the source, revision, complete fields and local ownership in its transaction.
   Unsaved changes are refused by Confirm and Submit, including with JavaScript
   disabled. Select **Save reviewed fields**, review, and confirm again.
5. Select **Run checks on current submission**. Both supplied PDFs pass the
   installed core's completeness check without opening a case. A real zero stays
   zero. Confirmation and a passing check do not approve payment.

PDFs, original extraction and reviewed revisions are retained in local `wb.pdf_*`
tables. Submissions and staging contain normalized invoice fields; PDF bytes and
page text do not enter core Audit Lineage, XCom or private normalized case samples.
Repeated source/value submissions reuse existing duplicate/restage behavior.
A different replacement PDF retains its own source and submission history.

Invoice ID, supplier ID, date and currency must be definite before confirmation.
Missing or ambiguous amounts can be confirmed and submitted as SQL NULL; their
distinct states remain in the immutable reviewed revision and case history.
An absent state carrying a number is refused. A real zero stays zero.
Corrupt, encrypted, oversized, scanned and unsupported multi-invoice documents
are refused, with bounded source/error evidence retained where input limits allow.

Extraction uses anchored invoice/supplier ID, ISO calendar date, currency and
final-total labels. Subtotals are not invoice totals. Distinct final totals stay
ambiguous, as do identifiers with explicit `OR` alternatives. Money supports
unsigned decimal-dot notation with at most two fractional
digits, without rounding or grouping separators. EUR/USD and the euro symbol are
supported; a bare dollar symbol remains ambiguous. No model or OCR is called.
Unfamiliar layouts can produce missing fields; arbitrary-layout detection is
not established. All PDF routes refuse hosted requests before workspace writes;
this feature does not enable hosted PDF retention.

## PDF correction and resolution

1. Upload [the missing-amount PDF](../fixtures/pdf_intake/missing_amount.pdf) or
   [the ambiguous-amount PDF](../fixtures/pdf_intake/ambiguous_amount.pdf). Inspect
   the source and amount state, then confirm and submit without guessing a value.
   Confirmation and submission alone do not open a case.
2. Run checks on the current submission. The installed core reports the missing
   amount and opens a case. Resolve stays unavailable while checks fail.
3. Return to submissions and upload [the corrected PDF](../fixtures/pdf_intake/valid.pdf),
   or use **Replace PDF source** on the original draft. All three fixtures share
   invoice reference `PDF-VALID-001`; the corrected source supports `12.34 EUR`.
   Review and explicitly confirm the replacement before submitting it.
4. The replacement becomes submission version 2. Original PDF bytes, extraction,
   reviewed revisions and submission history remain stored. Earlier confirmation
   cannot authorize the replacement, and the previous checks cannot resolve it.
   Replaying the older PDF does not displace the corrected submission.
5. Run fresh checks on the corrected submission. The case remains open after a
   pass; select **Resolve case** to close it. Missing, stale, errored or incomplete
   evidence, including unavailable core Audit Lineage, still refuses resolution.
   Resolution does not approve payment or establish invoice truth.

The verified development-only contract is the local intake prerequisite. Full
ticket #2, the independent reserved corpus and held-out accuracy remain incomplete.
