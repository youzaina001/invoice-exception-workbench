# Invoice Exception Review

LedgerLens lets an operator investigate incomplete supplier invoices and resolve exception cases using current check evidence. The default runtime is the localhost workflow in `README.md`. Hosted mode gives an invited Reviewer synthetic examples inside a private Demo Workspace. No hosted deployment is claimed. See `docs/adr/0001-select-hosted-services.md`.

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
One execution of the invoice walkthrough within a Demo Workspace, with its own submissions, cases, check results, and audit evidence. It starts with no invoices. Starting fresh begins a new attempt and leaves the previous attempt as read-only history. A Demo Workspace keeps at most five Demo Attempts; the oldest is removed when a sixth begins.
_Avoid_: Login session, cleared history
