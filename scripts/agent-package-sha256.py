#!/usr/bin/env python3
"""Rebuild the agent package from this checkout and print its SHA-256.

The CronRack server builds cronrack-agent.tar.gz the same way, byte for byte
(server/agents/package.py: sorted files, fixed owners and times). So you can check what a
server hands out without trusting that server:

    git clone https://github.com/diischm/cronrack && cd cronrack
    git checkout <the version your server runs>
    python3 scripts/agent-package-sha256.py

and then install with the first hash it prints:

    sudo bash install.sh --server https://SERVER --expect-sha256 <hash>

install.sh refuses to install anything if the download doesn't match.

Standard library only; no Django and nothing from the network. The lines after the first
list each file's own SHA-256 and the hash of the uncompressed tar, which still match if a
different zlib ever compresses the same files to different bytes.
"""

import gzip
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_package_module():
    # Loaded by file path, so nothing else from server/ (and no Django) is imported.
    spec = importlib.util.spec_from_file_location(
        "cronrack_agent_package", REPO / "server" / "agents" / "package.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    package = load_package_module()
    built = package.build_package()
    print(f"{built.sha256}  {package.TARBALL_NAME}")
    with tarfile.open(fileobj=io.BytesIO(built.data), mode="r:gz") as tar:
        for info in tar.getmembers():
            if info.isfile():
                data = tar.extractfile(info).read()
                print(f"  {hashlib.sha256(data).hexdigest()}  {info.name}")
    print(f"  {hashlib.sha256(gzip.decompress(built.data)).hexdigest()}  (uncompressed tar)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
