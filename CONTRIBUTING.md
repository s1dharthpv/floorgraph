# Contributing to Floorgraph

Thanks for helping. Bug reports, questions and pull requests are all welcome.

## Before you start

- Read the [README](README.md) for what Floorgraph does and why, especially the governed action layer.
  Every write to the shop floor must go through `floorgraph.actions.engine`, never around it.
- For a bug or a request, open an issue first so we can agree on the approach.
- To report a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Set up

```bash
bench get-app https://github.com/s1dharthpv/floorgraph --branch develop
bench --site <your-site> install-app floorgraph
cd apps/floorgraph
pre-commit install
```

Floorgraph needs ERPNext (Work Orders, Job Cards, BOMs). CI tests against Frappe and ERPNext v15 and v16.

## Make a change

1. Branch from `develop`. `main` is the stable branch that installs target.
2. Keep the change small and focused, and update the README if behaviour changes.
3. Add or update tests, then run the suite:
   ```bash
   bench --site <your-site> set-config allow_tests true
   bench --site <your-site> run-tests --app floorgraph
   ```
4. Run `pre-commit run --all-files` (ruff, eslint, prettier, pyupgrade). CI runs the same linters on every pull request.
5. Open a pull request into `develop`.

## Pull request checklist

- Tests pass on the Frappe versions you can test, and the linters are clean.
- New writes go through the action layer, and agent-sourced requests still require human approval.
- No real customer or production data in tests, screenshots or issue text.

By contributing you agree that your work is released under the [MIT license](LICENSE).
