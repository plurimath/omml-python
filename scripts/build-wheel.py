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
import struct
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def validated_receipt(path, pins, binary, metadata_path, platform_tag):
    """CI-produced artifact path: every receipt field must agree with the pins, the host
    platform and the actual files. Nothing is skipped or rewritten."""
    receipt = json.loads(Path(path).read_bytes())
    if receipt.get("kind") != "native-build-receipt/v1":
        raise RuntimeError("Build receipt has an unknown kind")
    if receipt["native_source_commit"] != pins["native_source_commit"]:
        raise RuntimeError("Build receipt source commit differs from the pinned native source")
    if receipt["cargo_lock_sha256"] != pins["cargo_lock_sha256"]:
        raise RuntimeError("Build receipt Cargo.lock differs from the pinned lockfile")
    if receipt["platform_tag"] != pins["platform_tag"] or receipt["platform_tag"] != platform_tag:
        raise RuntimeError("Build receipt platform differs from the pinned wheel platform")
    if receipt["library"] != binary.name:
        raise RuntimeError("Build receipt names a different library")
    if digest(binary) != receipt["library_sha256"]:
        raise RuntimeError("Library differs from the binary recorded in the build receipt")
    if digest(metadata_path) != receipt["cargo_metadata_sha256"]:
        raise RuntimeError("Cargo metadata differs from the build receipt")
    return receipt


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--rust-source", required=True, type=Path)
    cli.add_argument("--library", required=True, type=Path)
    cli.add_argument("--cargo-metadata", required=True, type=Path)
    cli.add_argument("--wheel-dir", type=Path)
    cli.add_argument("--prepare-dir", type=Path, help="Validate and emit native receipt/notices without building a wheel")
    cli.add_argument("--platform-tag", choices=("linux_x86_64", "win_amd64"))
    cli.add_argument("--target-metadata", type=Path, help="Exact target metadata when preparing on another platform")
    cli.add_argument("--build-receipt", type=Path, help="CI build receipt from scripts/build-native-receipt.py; replaces only the fixed library_sha256 comparison, after validating the receipt")
    args = cli.parse_args()
    source = args.rust_source.resolve()
    platform_tag = args.platform_tag or sysconfig.get_platform().replace("-", "_").replace(".", "_")
    if args.platform_tag and not args.prepare_dir:
        raise RuntimeError("Explicit target platforms are only supported for receipt preparation")
    if not args.prepare_dir and not args.wheel_dir:
        cli.error("--wheel-dir or --prepare-dir is required")
    pin_file = ROOT / ("native-source.win_amd64.json" if platform_tag == "win_amd64" else "native-source.json")
    pins = json.loads(pin_file.read_bytes())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != pins["native_source_commit"]:
        raise RuntimeError("Rust checkout is at a different revision")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=source):
        raise RuntimeError("Rust checkout must be clean")
    if digest(source / "Cargo.lock") != pins["cargo_lock_sha256"]:
        raise RuntimeError("Cargo.lock differs from the pinned bytes")
    binary = args.library.resolve()
    expected_library = "omml.dll" if platform_tag == "win_amd64" else "libomml.so"
    if platform_tag != pins["platform_tag"] or binary.name != expected_library:
        raise RuntimeError("Native library does not target this wheel platform")
    build_receipt = None
    if args.build_receipt:
        build_receipt = validated_receipt(args.build_receipt, pins, binary, args.cargo_metadata, platform_tag)
    elif digest(binary) != pins["library_sha256"]:
        raise RuntimeError("Library differs from the pinned native build")
    if platform_tag == "win_amd64":
        image = binary.read_bytes()
        pe = struct.unpack_from("<I", image, 0x3c)[0]
        if image[:2] != b"MZ" or image[pe:pe+4] != b"PE\0\0" or struct.unpack_from("<H", image, pe+4)[0] != 0x8664:
            raise RuntimeError("Native library is not a Windows AMD64 PE image")
    metadata_pin = pins.get("source_metadata_sha256", pins.get("cargo_metadata_sha256"))
    if metadata_pin and not build_receipt and digest(args.cargo_metadata) != metadata_pin:
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
    target_metadata = metadata
    if args.target_metadata:
        if digest(args.target_metadata) != pins["cargo_metadata_sha256"]:
            raise RuntimeError("Target metadata differs from its pinned native build")
        target_metadata = json.loads(args.target_metadata.read_bytes())
        identity = lambda package: (package["name"], package["version"], package["source"])
        known = {identity(package) for package in metadata["packages"]}
        if any(identity(package) not in known for package in target_metadata["packages"]):
            raise RuntimeError("Target metadata contains an unverified source dependency")
    elif pins.get("source_metadata_sha256"):
        raise RuntimeError("This target requires its sealed --target-metadata")
    target_names = {package["name"] for package in target_metadata["packages"]}
    target_root = target_metadata["resolve"]["root"]
    target_node = next(node for node in target_metadata["resolve"]["nodes"] if node["id"] == target_root)
    if not set(pins.get("features", ())).issubset(target_node["features"]):
        raise RuntimeError("Target metadata lacks required native features")
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
            if package["name"] not in target_names:
                continue
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
            "binary_provenance": "ci-build-receipt" if build_receipt else "pinned-binary",
            "build_receipt": build_receipt,
            "cargo_metadata_sha256": digest(args.target_metadata or args.cargo_metadata),
        }, indent=2) + "\n")
        if args.prepare_dir:
            shutil.copytree(staging, args.prepare_dir)
            return
        env["OMML_NATIVE"] = str(binary)
        env["OMML_NATIVE_RECEIPT"] = str(receipt)
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps",
             "--wheel-dir", str(args.wheel_dir.resolve()), "."],
            cwd=ROOT, env=env, check=True,
        )


if __name__ == "__main__":
    main()
