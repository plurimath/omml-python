"""Repository-native CI: pinned public native build, wheel, fresh install, installed tests.

Configuration lives in ci/native-source.json. Native sources are fetched from their
public GitHub repositories at the pinned revision. For local qualification before
those repositories exist, set CI_LOCAL_SOURCES to a JSON object mapping repository
names ("owner/name") to local clones that contain the pinned revision.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile, time, venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "ci/native-source.json").read_text(encoding="utf-8"))
PACKAGE = CONFIG["package"]
NATIVE = CONFIG.get("native")
EVIDENCE = ROOT / ".ci-evidence"
STAGING = ROOT / ".native"
SOURCES = ROOT / ".native-src"
LOCAL = json.loads(os.environ.get("CI_LOCAL_SOURCES", "{}"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, label, cwd=ROOT, env=None, timeout=3600):
    EVIDENCE.mkdir(exist_ok=True)
    start = time.monotonic()
    result = subprocess.run([str(c) for c in command], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    (EVIDENCE / f"{label}.log").write_text(result.stdout, encoding="utf-8")
    (EVIDENCE / f"{label}.json").write_text(json.dumps({
        "command": [str(c) for c in command], "exit_code": result.returncode,
        "seconds": round(time.monotonic() - start, 3)}, indent=2) + "\n", encoding="utf-8")
    if result.returncode:
        print(result.stdout[-6000:])
        raise SystemExit(f"{label} exited {result.returncode}; full output in .ci-evidence")
    return result.stdout


def checkout(pin, name):
    """Clone repository@revision into .native-src/<name> (public URL, or a local override)."""
    repository, revision = pin["repository"], pin["revision"]
    target = SOURCES / name
    if (target / ".git").exists():
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
        if current == revision:
            return target
        shutil.rmtree(target)
    target.mkdir(parents=True)
    origin = LOCAL.get(repository) or f"https://github.com/{repository}.git"
    run(["git", "init", "-q", target], f"clone-{name}-init")
    run(["git", "-C", target, "fetch", "-q", "--depth", "1", origin, revision], f"clone-{name}-fetch")
    run(["git", "-C", target, "checkout", "-q", "FETCH_HEAD"], f"clone-{name}-checkout")
    got = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
    if got != revision:
        raise SystemExit(f"{repository}: expected {revision}, fetched {got}")
    return target


def native_dir():
    return ROOT / "src" / PACKAGE / "_native"


def library_file(stem):
    if sys.platform == "win32":
        return f"{stem}.dll"
    return f"lib{stem}." + ("dylib" if sys.platform == "darwin" else "so")


def sibling_checkouts():
    """Path dependencies (e.g. ../unitsdb-rs) are checked out next to the main source at their pins."""
    for dep in (NATIVE.get("deps") or {}).values():
        if dep.get("checkout_as"):
            checkout(dep, dep["checkout_as"])


def build_native():
    STAGING.mkdir(exist_ok=True)
    kind = NATIVE["kind"]
    sibling_checkouts()
    rust = checkout(NATIVE, "rust")
    if kind == "mml-dual":
        framework = checkout(NATIVE["framework"], "framework")
        run([sys.executable, ROOT / "build_native.py", "--framework-source", framework,
             "--rust-source", rust, "--target-dir", SOURCES / "target"], "build-native")
        shutil.copytree(native_dir(), STAGING / "_native", dirs_exist_ok=True)
    elif kind == "prebuilt-library":
        # The CI producer builds the pinned clean source and records a build receipt;
        # scripts/build-wheel.py validates it against the pins and the actual binary.
        run([sys.executable, ROOT / "scripts/build-native-receipt.py", "--rust-source", rust,
             "--target-dir", SOURCES / "target", "--out-dir", STAGING,
             "--library-name", library_file(NATIVE["library_stem"])], "build-native")
    elif kind == "cargo-build":
        pass  # the package's own wheel builder runs cargo itself
    else:
        raise SystemExit(f"unknown native kind {kind}")
    (STAGING / "receipt.json").write_text(json.dumps({
        "rust_revision": NATIVE["revision"], "files": {p.name: digest(p) for p in STAGING.rglob("*") if p.is_file()}},
        indent=2) + "\n")


def build_python():
    """Isolated build environment: Python 3.12+ runners ship without setuptools, and the
    package builders call pip with --no-build-isolation."""
    env_dir = ROOT / ".ci-build-venv"
    python = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        run([python, "-m", "pip", "install", "--quiet", "setuptools>=61", "wheel"], "build-env")
    return python


def build_wheel(dist):
    dist.mkdir(parents=True, exist_ok=True)
    py = build_python()
    if NATIVE is None:
        run([py, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", dist, ROOT], "wheel")
        return
    kind = NATIVE["kind"]
    sibling_checkouts()
    rust = checkout(NATIVE, "rust")
    # Wheel jobs run on fresh runners; restore exact notice sources without rebuilding.
    run(["cargo", "fetch", "--locked"], "fetch-notice-sources", cwd=rust)
    if kind == "mml-dual":
        shutil.copytree(STAGING / "_native", native_dir(), dirs_exist_ok=True)
        run([py, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", dist, ROOT], "wheel")
    elif kind == "prebuilt-library":
        library = STAGING / library_file(NATIVE["library_stem"])
        run([py, ROOT / "scripts/build-wheel.py", "--rust-source", rust, "--library", library,
             "--cargo-metadata", STAGING / "cargo-metadata.json", "--build-receipt", STAGING / "build-receipt.json",
             "--wheel-dir", dist], "wheel")
    elif kind == "cargo-build":
        run([py, ROOT / "scripts/build-wheel.py", "--rust-source", rust,
             "--target-dir", SOURCES / "target", "--wheel-dir", dist], "wheel")


def environment():
    return ROOT / ".ci-venv"


def install_wheel():
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    build_wheel(dist)
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, found {[w.name for w in wheels]}")
    if environment().exists():
        shutil.rmtree(environment())
    venv.EnvBuilder(with_pip=True, clear=True).create(environment())
    python = environment() / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    extra = json.loads(os.environ.get("CI_EXTRA_WHEELS", "[]"))  # e.g. unitsml needs the asciiunits wheel
    run([python, "-m", "pip", "install", "--no-index", *extra, wheels[0]], "install")
    (EVIDENCE / "wheel.json").write_text(json.dumps({"wheel": wheels[0].name, "sha256": digest(wheels[0])}, indent=2) + "\n")


def test_installed():
    python = environment() / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    with tempfile.TemporaryDirectory(prefix=f"{PACKAGE}-installed-") as work:
        tests = Path(work) / "tests"
        if (ROOT / "tests").exists():
            shutil.copytree(ROOT / "tests", tests)
        location = run([python, "-c", f"import {PACKAGE}, pathlib; print(pathlib.Path({PACKAGE}.__file__).resolve())"],
                       "import-location", cwd=work).strip()
        if Path(location).is_relative_to(ROOT / "src"):
            raise SystemExit(f"{PACKAGE} imported from the source tree, not the installed wheel: {location}")
        if CONFIG["tests"]:
            names = [Path(t).stem for t in CONFIG["tests"]]
            run([python, "-m", "unittest", "-v", *names], "test-installed", cwd=tests)
        else:
            run([python, "-c", SEAL_CHECK], "test-installed", cwd=work)


SEAL_CHECK = """
import hashlib, json, importlib.resources as r
data = r.files('unitsdb') / 'data'
manifest = json.loads((data / 'manifest.json').read_text())
text = json.dumps(manifest)
for name in ('unitsdb.json', 'lookup-index.json'):
    sha = hashlib.sha256((data / name).read_bytes()).hexdigest()
    assert sha in text, (name, sha)
    print(name, sha)
print('seals ok')
"""


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("step", choices=("build-native", "install-wheel", "test-installed"))
    step = cli.parse_args().step
    {"build-native": build_native, "install-wheel": install_wheel, "test-installed": test_installed}[step]()


if __name__ == "__main__":
    main()
