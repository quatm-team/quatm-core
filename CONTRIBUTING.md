# Contributing to Quatm Core

Thanks for improving Quatm Core. This repository contains the AGPL-3.0
runtime only; the commercial GUI, web console, licensing layer, analysis tools,
monitoring tools and vendor driver catalogue live in a separate repository.

## Before you start

- Search existing issues and pull requests before opening a new one.
- For a substantial change, open an issue first so the design and licence
  boundary can be agreed before implementation.
- Security reports must not be posted publicly. Follow [SECURITY.md](SECURITY.md).

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
python -m unittest discover -s tests -t . -q
```

The test suite uses the Python standard library `unittest`; no test framework
installation is required.

## Pull requests

- Keep each pull request focused. Separate refactors, behaviour changes and
  formatting-only edits.
- Add or update tests for behavioural changes.
- Update public documentation when an interface, default or workflow changes.
- Do not add dependencies without an issue or ADR explaining the need and the
  alternatives considered.
- Do not commit credentials, tokens, private laboratory data, customer
  configuration or vendor SDK binaries.
- Keep timing, units and experiment semantics explicit. A change to those
  semantics needs a short rationale in the pull request.
- New public documentation and user-facing messages should be written in
  English. Source comments may explain a non-obvious implementation detail,
  but should not contain internal task numbers or private repository paths.

Run at least the core tests before requesting review:

```bash
python -m unittest discover -s tests -t . -q
```

## Licensing of contributions

By submitting a contribution to this repository, you confirm that you have the
right to license it under **AGPL-3.0**. Contributions are accepted under
AGPL-3.0; you retain copyright in your contribution.

Do not contribute code that is copied from a project with an incompatible
licence, or code that you do not have permission to redistribute.

## Review

Maintainers review for correctness, test coverage, operational safety,
documentation quality and licence compatibility. A pull request may be declined
when it adds maintenance cost without a clear laboratory use case, or when it
crosses the boundary into the separate commercial platform.

## Conduct

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
