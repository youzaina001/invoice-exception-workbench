# Invoice Exception Review

Invoice Triage lets an operator investigate incomplete supplier invoices and resolve exception cases using current check evidence. The default runtime is the localhost workflow in `README.md`. Hosted mode gives an invited Reviewer synthetic examples inside a private Demo Workspace. No hosted deployment is claimed. See `docs/adr/0001-select-hosted-services.md`.

## Language

**Operator**:
The person who investigates an incomplete supplier invoice and resolves its exception case inside the workbench.
_Avoid_: Reviewer, admin, user

**Reviewer**:
An invited person who enters the hosted demonstration and acts as the Operator inside their own Demo Workspace.
_Avoid_: Customer, visitor, maintainer

**Maintainer**:
The person who owns the hosted demonstration and may inspect every Demo Workspace.
_Avoid_: Host, admin, reviewer

**Demo Workspace**:
One Reviewer's private collection of synthetic invoice submissions, cases, check results, and audit evidence. It begins when that Reviewer signs in for the first time. Other Reviewers cannot read or change its contents.
_Avoid_: Shared demo, customer account

**Demo Attempt**:
One execution of the invoice walkthrough within a Demo Workspace, with its own submissions, cases, check results, and audit evidence. It starts with no invoices. Starting fresh begins a new attempt and leaves the previous attempt as read-only history. A Demo Workspace keeps at most five Demo Attempts; the oldest is removed when a sixth begins. Only the current Demo Attempt accepts new submissions, check runs, or case resolutions.
_Avoid_: Login session, cleared history

**Submission**:
One immutable version of a supplier invoice entered into the workbench for a given invoice reference, either from a single-invoice CSV upload or from an explicitly confirmed local PDF review.
_Avoid_: Raw upload, draft, mutable record

**Current Submission**:
The single active submission held in staging for quality checks at a given moment within local mode or a Demo Attempt. Running checks evaluates only the Current Submission, and resolving an Exception Case requires passing evidence bound to it.
_Avoid_: Latest upload, staging table

**Exception Case**:
An open or resolved tracking record created when a required quality check does not pass for an invoice reference. An Exception Case remains open until an Operator explicitly resolves it after fresh passing checks and complete audit evidence on the Current Submission.
_Avoid_: Ticket, quarantine, auto-closed alert
