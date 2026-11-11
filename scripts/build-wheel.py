"""Package the pinned native OMML build in a host-platform wheel."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--rust-source", required=True, type=Path)
    cli.add_argument("--library", required=True, type=Path)
    cli.add_argument("--cargo-metadata", required=True, type=Path)
    cli.add_argument("--wheel-dir", required=True, type=Path)
    args = cli.parse_args()
    source = args.rust_source.resolve()
    pins = json.loads((ROOT / "native-source.json").read_bytes())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != pins["native_source_commit"]:
        raise RuntimeError("Rust checkout is at a different revision")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=source):
        raise RuntimeError("Rust checkout must be clean")
    if digest(source / "Cargo.lock") != pins["cargo_lock_sha256"]:
        raise RuntimeError("Cargo.lock differs from the pinned bytes")
    binary = args.library.resolve()
    platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    if platform_tag != pins["platform_tag"] or binary.name != "libomml.so":
        raise RuntimeError("Native library does not target this wheel platform")
    if digest(binary) != pins["library_sha256"]:
        raise RuntimeError("Library differs from the pinned native build")
    if "cargo_metadata_sha256" in pins and digest(args.cargo_metadata) != pins["cargo_metadata_sha256"]:
        raise RuntimeError("Cargo metadata differs from the pinned native build")
    metadata = json.loads(args.cargo_metadata.read_bytes())
    if Path(metadata["workspace_root"]).resolve() != source:
        raise RuntimeError("Cargo metadata belongs to a different checkout")
    packages = {package["name"]: package for package in metadata["packages"]}
    for name, expected in pins.get("native_source_dependencies", {}).items():
        checkout = Path(packages[name]["manifest_path"]).parent
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=checkout, text=True
        ).splitlines()
        package_source = packages[name]["source"]
        if package_source and package_source.startswith("git+"):
            if not package_source.endswith("#" + expected):
                raise RuntimeError(f"Cargo resolved a different dependency revision: {name}")
            status = [line for line in status if line != "?? .cargo-ok"]
        if revision != expected or status:
            raise RuntimeError(f"Native source dependency differs from its pinned clean checkout: {name}")
    for name, assets in pins.get("native_data", {}).items():
        checkout = Path(packages[name]["manifest_path"]).parent
        for asset, expected in assets.items():
            if digest(checkout / asset) != expected:
                raise RuntimeError(f"Bundled native data differs from its pin: {name}/{asset}")
    root_id = metadata["resolve"]["root"]
    root_node = next(node for node in metadata["resolve"]["nodes"] if node["id"] == root_id)
    if not set(pins.get("features", ())).issubset(root_node["features"]):
        raise RuntimeError("Cargo metadata lacks required native features")
    env = os.environ.copy()
    with TemporaryDirectory(prefix="omml-wheel-") as temporary:
        staging = Path(temporary)
        notices = staging / "licenses"
        notices.mkdir()
        dependencies = []
        for package in metadata["packages"]:
            base = Path(package["manifest_path"]).parent
            name_version = f"{package['name']}-{package['version']}"
            files = {path for path in base.iterdir() if path.is_file() and
                     path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))}
            if package.get("license_file"):
                files.add(base / package["license_file"])
            if package["name"] in ("parsanol", "parsanol-derive"):
                # The Parsanol workspace license covers both nested crates.
                workspace = Path(subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"], cwd=base, text=True
                ).strip())
                files.update(path for path in workspace.iterdir() if path.is_file()
                             and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")))
            recorded = {}
            if not files:
                if not package.get("license"):
                    raise RuntimeError(f"Native source has no declared license: {name_version}")
                destination = notices / name_version / "SOURCE-LICENSE.json"
                destination.parent.mkdir(exist_ok=True)
                destination.write_text(json.dumps({
                    "package": package["name"], "version": package["version"],
                    "spdx": package["license"], "source": package["source"],
                    "source_revision": pins.get("native_source_dependencies", {}).get(package["name"]),
                    "manifest_sha256": digest(base / "Cargo.toml"),
                    "notice": "Upstream source declares SPDX metadata and has no standalone license or notice file.",
                }, indent=2) + "\n")
                recorded[str(destination.relative_to(notices))] = digest(destination)
                source_manifest = destination.parent / "Cargo.toml"
                shutil.copyfile(base / "Cargo.toml", source_manifest)
                recorded[str(source_manifest.relative_to(notices))] = digest(source_manifest)
            for path in sorted(files):
                destination = notices / name_version / path.name
                destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(path, destination)
                recorded[str(destination.relative_to(notices))] = digest(destination)
            dependencies.append({
                "name": package["name"], "version": package["version"],
                "license": package.get("license"), "notices": recorded,
            })
        (notices / "manifest.json").write_text(json.dumps(dependencies, indent=2) + "\n")
        receipt = staging / "build-info.json"
        receipt.write_text(json.dumps({
            **pins, "library": binary.name, "sha256": digest(binary), "abi_version": 1,
            "cargo_metadata_sha256": digest(args.cargo_metadata),
        }, indent=2) + "\n")
        env["OMML_NATIVE"] = str(binary)
        env["OMML_NATIVE_RECEIPT"] = str(receipt)
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps",
             "--wheel-dir", str(args.wheel_dir.resolve()), "."],
            cwd=ROOT, env=env, check=True,
        )


if __name__ == "__main__":
    main()
