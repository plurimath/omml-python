#!/usr/bin/env python3
"""Give Linux wheels a platform tag PyPI accepts, then check every wheel.

setuptools tags a wheel built on Linux `linux_<arch>`, and PyPI rejects that
tag. These wheels ship a ctypes-loaded native library, so their real Linux
compatibility is set by the glibc symbol versions that library needs.
`auditwheel show` measures that and names the matching manylinux (or
musllinux) policy. The wheel is then retagged with exactly that policy using
`wheel tags`, as parsanol-python does, so the native bytes and the checksums
recorded for them do not change. If auditwheel cannot place the wheel under
any policy, this script fails here instead of at upload time.

After that, every wheel goes through `twine check`, and its file name and
WHEEL tags are printed and saved to .ci-evidence/wheel-tags.json.

Usage: python scripts/wheel_tags.py DIST_DIR
"""
import json
import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

TOOLS = ["auditwheel==6.8.2", "twine==7.0.0", "wheel==0.48.0"]
POLICY = re.compile(r"(?:many|musl)linux_\d+_\d+_\w+")


def tools_python():
    env = Path(tempfile.mkdtemp(prefix="wheel-tools-"))
    venv.EnvBuilder(with_pip=True, clear=True).create(env)
    python = env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    output([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *TOOLS])
    return python


def output(command):
    command = [str(part) for part in command]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"exit {result.returncode}: {' '.join(command)}")
    return result.stdout


def platform_tag(wheel):
    return wheel.name[:-len(".whl")].split("-")[-1]


def wheel_tags(wheel):
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist()
                    if n.count("/") == 1 and n.endswith(".dist-info/WHEEL"))
        lines = archive.read(name).decode("utf-8").splitlines()
    return [line.split(":", 1)[1].strip() for line in lines if line.startswith("Tag:")]


def retag_linux(python, wheel):
    report = json.loads(output([python, "-m", "auditwheel", "show", "--json", wheel]))
    tag = report.get("overall_tag") or ""
    if not POLICY.fullmatch(tag):
        sys.stderr.write(json.dumps(report, indent=2) + "\n")
        raise SystemExit(f"{wheel.name}: auditwheel found no manylinux/musllinux policy ({tag!r})")
    output([python, "-m", "wheel", "tags", "--remove", "--platform-tag", tag, wheel])
    return tag


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    dist = Path(sys.argv[1]).resolve()
    if not any(dist.glob("*.whl")):
        raise SystemExit(f"no wheels in {dist}")
    python = tools_python()
    for wheel in sorted(dist.glob("*.whl")):
        if platform_tag(wheel).startswith("linux_"):
            if not sys.platform.startswith("linux"):
                raise SystemExit(f"{wheel.name}: Linux wheel found on {sys.platform}")
            print(f"{wheel.name}: auditwheel policy {retag_linux(python, wheel)}")
    wheels = sorted(dist.glob("*.whl"))
    left = [w.name for w in wheels if platform_tag(w).startswith("linux_")]
    if left:
        raise SystemExit(f"wheels still tagged linux_*: {left}")
    print(output([python, "-m", "twine", "check", *wheels]), end="")
    evidence = [{"wheel": w.name, "tags": wheel_tags(w)} for w in wheels]
    for entry in evidence:
        print(f"wheel {entry['wheel']}: Tag {', '.join(entry['tags'])}")
    folder = Path.cwd() / ".ci-evidence"
    folder.mkdir(exist_ok=True)
    (folder / "wheel-tags.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
