import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import re
import sys
import sysconfig
import unittest

import omml


class InstalledPlatform(unittest.TestCase):
    def test_bundled_native_matches_installed_wheel_platform(self):
        omml.Runtime()
        native = Path(omml.__file__).with_name("_native")
        receipt = json.loads((native / "build-info.json").read_bytes())
        expected = "omml.dll" if sys.platform == "win32" else "libomml.so"
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        self.assertEqual(receipt["library"], expected)
        self.assertEqual(receipt["platform_tag"], platform)
        self.assertEqual(hashlib.sha256((native / expected).read_bytes()).hexdigest(), receipt["sha256"])
        tags = [line.split(":", 1)[1].strip()
                for line in distribution("omml").read_text("WHEEL").splitlines()
                if line.startswith("Tag:")]
        if platform.startswith("linux_"):
            # scripts/wheel_tags.py retags linux_<arch> with the measured policy.
            arch = re.escape(platform[len("linux_"):])
            policy = re.compile(r"py3-none-(?:many|musl)linux_\d+_\d+_" + arch)
            self.assertTrue(tags and all(policy.fullmatch(tag) for tag in tags), tags)
        else:
            self.assertIn("py3-none-" + platform, tags)
        manifest = json.loads((native / "licenses" / "manifest.json").read_bytes())
        for dependency in manifest:
            for name, digest in dependency["notices"].items():
                self.assertEqual(hashlib.sha256((native / "licenses" / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
