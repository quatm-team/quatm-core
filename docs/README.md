# Quatm Core documentation

Public guides for installation, experiment programming, device integration,
data handling and deployment are maintained at:

- <https://quatm.com/en/docs/>
- <https://quatm.com/开发文档/>

This repository documents the AGPL core runtime. GUI, web console, licensing,
analysis, monitoring and vendor-driver documentation belongs to the separate
commercial platform.

## Local checks

From the repository root:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -t . -q
```