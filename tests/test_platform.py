import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
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
        self.assertIn("Tag: py3-none-" + platform, distribution("omml").read_text("WHEEL"))
        manifest = json.loads((native / "licenses" / "manifest.json").read_bytes())
        for dependency in manifest:
            for name, digest in dependency["notices"].items():
                self.assertEqual(hashlib.sha256((native / "licenses" / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
