<div align="center">

# Quatm Core

**A Python-native, AGPL-3.0 runtime for quantum experiments.**

Quatm Core is the open runtime for cold-atom, ion-trap, quantum-optics and
quantum-chemistry experiments. Describe instruments once, write the experiment
as a Python class, and let the framework handle sequencing, scans, data and
services.

[Website](https://quatm.com/) · [Documentation](https://quatm.com/en/docs/) ·
[Getting started](https://quatm.com/en/docs/getting-started/) ·
[Contributing](CONTRIBUTING.md) · [Issues](https://github.com/quatm-team/quatm-core/issues)

[![Website](https://img.shields.io/static/v1?label=Website&message=quatm.com&color=0b5cad)](https://quatm.com/)
[![Docs](https://img.shields.io/static/v1?label=Docs&message=read%20online&color=1f6f8b)](https://quatm.com/en/docs/)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-8f5735)](LICENSE)
[![Python: 3.9+](https://img.shields.io/static/v1?label=Python&message=3.9%2B&color=3776AB&logo=python&logoColor=white)](https://www.python.org/)
[![Core tests](https://github.com/quatm-team/quatm-core/actions/workflows/core-tests.yml/badge.svg)](https://github.com/quatm-team/quatm-core/actions/workflows/core-tests.yml)

</div>

## What is in this repository

```text
quatm/experiment/   Experiment lifecycle, parameters, scans and timing
quatm/physics/      Atomic and ionic physics constants and models
quatm/servers/      Properties, messaging, data/image services and RPC
quatm/utils/        Shared utilities, safe evaluation and scheduling helpers
tests/              Core-only regression tests
docs/               Public documentation entry points
```

The desktop GUI, web console, licensing layer, analysis tools, monitoring tools
and vendor driver catalogue are maintained separately as the commercial Quatm
platform. The open core is complete without them.

## Install and test

Python 3.9 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
python -m unittest discover -s tests -t . -q
```

The distribution is named `quatm-core`; the Python package remains `quatm`:

```python
import quatm
from quatm.experiment import Experiment
```

A user project supplies `configuration/`, `experiments/` and `Data/`. A
compatible GUI platform can create the minimal skeleton on a new machine.

## Community

Quatm Core is developed in the open. Bug reports, documentation fixes, driver
interface discussions and well-scoped improvements are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). For usage questions, start with the
[documentation](https://quatm.com/en/docs/).

## License

Quatm Core is released under the **GNU AGPL-3.0**. See [LICENSE](LICENSE) for
the full terms.

Commercial extensions, managed deployment and support are available separately;
contact **sales@quatm.com** if your laboratory needs them. The commercial
offering does not change the open-source terms of this repository.
