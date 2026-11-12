"""Build the pinned native library from a clean pinned checkout and write a build receipt.

The receipt records what CI actually built: source commit, Cargo.lock hash, target and
platform, compiler and Cargo versions, the exact build invocation, and the resulting
library hash. scripts/build-wheel.py --build-receipt validates this receipt against
the repository pins and the actual binary before packing. It does not replace the pinned
library_sha256 path for historical source pins; current public pins require a build receipt.
"""

import argparse
from hashlib import sha256
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--rust-source", required=True, type=Path)
    cli.add_argument("--target-dir", required=True, type=Path)
    cli.add_argument("--out-dir", required=True, type=Path)
    cli.add_argument("--pins", type=Path, default=ROOT / "native-source.json")
    cli.add_argument("--library-name", required=True, help="cdylib file name, e.g. libomml.so")
    args = cli.parse_args()
    source = args.rust_source.resolve()
    pins = json.loads(args.pins.read_bytes())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != pins["native_source_commit"]:
        raise SystemExit(f"refusing to build: checkout is {revision}, pinned {pins['native_source_commit']}")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source):
        raise SystemExit("refusing to build: native checkout is not clean")
    lock = digest(source / "Cargo.lock")
    if lock != pins["cargo_lock_sha256"]:
        raise SystemExit(f"refusing to build: Cargo.lock {lock} differs from pinned {pins['cargo_lock_sha256']}")
    library_name = args.library_name
    rustc = subprocess.check_output(["rustc", "-vV"], text=True)
    host = next(l.removeprefix("host: ") for l in rustc.splitlines() if l.startswith("host: "))
    cargo = subprocess.check_output(["cargo", "-V"], text=True).strip()
    target = args.target_dir.resolve()
    invocation = ["cargo", "build", "--locked", "--release", "--lib"]
    for feature in pins.get("features", []):
        invocation += ["--features", feature]
    env = dict(os.environ, CARGO_TARGET_DIR=str(target))
    subprocess.run(invocation, cwd=source, env=env, check=True)
    built = target / "release" / library_name
    if not built.is_file():
        raise SystemExit(f"cargo did not produce {library_name}")
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    library = out / library_name
    shutil.copy2(built, library)
    metadata = out / "cargo-metadata.json"
    metadata.write_bytes(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version", "1", "--filter-platform", host], cwd=source, env=env))
    receipt = {
        "kind": "native-build-receipt/v1",
        "build_host": {"system": platform.system(), "release": platform.release(),
                       "version": platform.version(), "machine": platform.machine(), "python": sys.version},
        "native_source_commit": revision,
        "cargo_lock_sha256": lock,
        "platform_tag": sysconfig.get_platform().replace("-", "_").replace(".", "_"),
        "target": host,
        "rustc": rustc.strip().splitlines()[0],
        "rustc_verbose": rustc.strip(),
        "cargo": cargo,
        "invocation": invocation,
        "profile": "release",
        "library": library_name,
        "library_sha256": digest(library),
        "cargo_metadata_sha256": digest(metadata),
    }
    (out / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({k: receipt[k] for k in ("native_source_commit", "platform_tag", "library", "library_sha256")}))


if __name__ == "__main__":
    sys.exit(main())
