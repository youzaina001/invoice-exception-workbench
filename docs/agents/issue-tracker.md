# Issue tracker: GitHub

Accepted issues and specifications live in `youzaina001/invoice-exception-workbench`.
Use `gh` to inspect them from a checkout; infer the repository from its remote.

- Read a ticket: `gh issue view <number> --comments`.
- List open work: `gh issue list --state open --json number,title,body,labels`.
- Read a pull request: `gh pr view <number> --comments`; `gh pr diff <number>`.

GitHub shares one number space across issues and pull requests. Resolve an
ambiguous number with `gh pr view <number>` and fall back to `gh issue view`.
Pull requests are not the feature-request triage surface for this project.

Remote writes (creating, commenting, editing, closing, claiming issues or pull
requests) remain user-terminal actions under the personal Guarded YOLO rules.
A skill's publishing instruction does not override those restrictions.
