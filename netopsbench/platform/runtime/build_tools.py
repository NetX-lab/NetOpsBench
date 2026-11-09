"""Install a verified official Containerlab release in a private tools directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .toolchain import DEFAULT_TOOLS, binary_version, release_spec


def install_release(archive: Path, output: Path) -> None:
    spec = release_spec()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != spec["archive_sha256"]:
        raise RuntimeError("Official Containerlab archive checksum mismatch")
    # Extract only the executable, never paths or symlinks supplied by an archive.
    with tarfile.open(archive) as tar:
        member = tar.getmember("containerlab")
        if not member.isfile():
            raise RuntimeError("Official archive has no regular Containerlab executable")
        stream = tar.extractfile(member)
        if stream is None:
            raise RuntimeError("Official archive has no readable Containerlab executable")
        with stream:
            data = stream.read()
    if hashlib.sha256(data).hexdigest() != spec["binary_sha256"]:
        raise RuntimeError("Official Containerlab executable checksum mismatch")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".containerlab-", dir=output) as temp:
        binary = Path(temp) / "containerlab"
        binary.write_bytes(data)
        binary.chmod(0o755)
        if binary_version(binary) != spec["version"]:
            raise RuntimeError("Official Containerlab version mismatch")
        manifest = Path(temp) / "manifest.json"
        manifest.write_text(json.dumps(spec, indent=2) + "\n")
        # Publish identity last; partial installs cannot pass runtime validation.
        (output / "manifest.json").unlink(missing_ok=True)
        binary.replace(output / "containerlab")
        manifest.replace(output / "manifest.json")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded official release archive")
    parser.add_argument("--output", type=Path, default=DEFAULT_TOOLS)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="netopsbench-release-") as temp:
        archive = args.archive or Path(temp) / "containerlab.tar.gz"
        if args.archive is None:
            with urllib.request.urlopen(release_spec()["archive_url"], timeout=120) as response:
                archive.write_bytes(response.read())
        install_release(archive, args.output.resolve())
    print(f"Installed official Containerlab {release_spec()['version']} in {args.output.resolve()}")


if __name__ == "__main__":
    _main()
