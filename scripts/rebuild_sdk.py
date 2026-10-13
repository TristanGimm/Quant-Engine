"""Rebuild upstream SDK metadata only; SDK code and license stay byte-identical.

Download: python -m pip download --no-deps ctrader-open-api==0.9.2 -d /tmp/sdk
Rebuild:  python scripts/rebuild_sdk.py /tmp/sdk/ctrader_open_api-0.9.2-py3-none-any.whl
"""
import base64
import csv
import hashlib
import io
from pathlib import Path
import sys
import zipfile

UPSTREAM_SHA256 = "dd163d9c590b264196146cc3fb2e3daf42d7331148ab9b15e03dfa0685f0fc53"
OLD = "ctrader_open_api-0.9.2.dist-info"
NEW = "ctrader_open_api-0.9.2+gimm.1.dist-info"
DEPENDENCIES = ["Twisted>=26.4.0", "pyOpenSSL>=26.0.0", "protobuf>=6.33.5,<7",
                "requests>=2.33.0", "inputimeout==1.0.4"]


def main():
    source = Path(sys.argv[1])
    if hashlib.sha256(source.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise SystemExit("Upstream SDK checksum mismatch")
    files = {}
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if name.endswith("/RECORD"):
                continue
            data = archive.read(name)
            if name == OLD + "/METADATA":
                lines = data.decode().splitlines()
                # Dependency fields live in the metadata header, before the description.
                split = lines.index("")
                header = [line for line in lines[:split] if not line.startswith("Requires-Dist:")]
                header = ["Version: 0.9.2+gimm.1" if line == "Version: 0.9.2" else line for line in header]
                header += ["Requires-Dist: " + dep for dep in DEPENDENCIES]
                data = ("\n".join(header + lines[split:]) + "\n").encode()
            files[name.replace(OLD, NEW)] = data
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([NEW + "/RECORD", "", ""])
    files[NEW + "/RECORD"] = record.getvalue().encode()
    destination = Path(__file__).resolve().parents[1] / "vendor" / "ctrader_open_api-0.9.2+gimm.1-py3-none-any.whl"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print(destination)


if __name__ == "__main__":
    main()
