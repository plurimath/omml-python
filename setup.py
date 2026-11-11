from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sysconfig

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        return True


class NativeWheel(bdist_wheel):
    def get_tag(self):
        return "py3", "none", sysconfig.get_platform().replace("-", "_").replace(".", "_")


class BuildNative(build_py):
    def run(self):
        super().run()
        try:
            binary = Path(os.environ["OMML_NATIVE"])
            receipt = Path(os.environ["OMML_NATIVE_RECEIPT"])
        except KeyError as error:
            raise RuntimeError("Supply the pinned omml-rs library and its native receipt") from error
        info = json.loads(receipt.read_bytes())
        if binary.name != info["library"] or sha256(binary.read_bytes()).hexdigest() != info["sha256"]:
            raise RuntimeError("Native library disagrees with its receipt")
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        if info["platform_tag"] != platform:
            raise RuntimeError("Native library receipt targets another platform")
        native = Path(self.build_lib) / "omml" / "_native"
        native.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(binary, native / binary.name)
        shutil.copyfile(receipt, native / "build-info.json")
        notices = receipt.parent / "licenses"
        if not notices.is_dir():
            raise RuntimeError("Native dependency licenses are required")
        shutil.copytree(notices, native / "licenses", dirs_exist_ok=True)


setup(distclass=NativeDistribution, cmdclass={"build_py": BuildNative, "bdist_wheel": NativeWheel})
