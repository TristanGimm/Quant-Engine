This wheel contains the official Spotware ctrader-open-api 0.9.2 SDK.
The Python source files and the upstream license are unchanged. Only wheel
metadata (version, dependency constraints, RECORD) is changed. The original
SDK pins outdated Twisted, OpenSSL, Requests and Protobuf versions with known
vulnerabilities. The local version allows patched dependencies, pinned by
our requirements files and exercised by tests/test_engine.py.

Source: https://github.com/spotware/OpenApiPy
PyPI: https://pypi.org/project/ctrader-open-api/0.9.2/
License: included in the wheel's dist-info directory.

Reproduce using scripts/rebuild_sdk.py; it checks the upstream SHA-256.
This compatibility patch is maintained locally, not endorsed by Spotware.
