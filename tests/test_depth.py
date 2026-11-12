"""Actual installed default-caller stack: no worker or stack-size override."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


def probe(depth):
    import omml
    import xml.etree.ElementTree as ET
    namespace = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    leaf = "<m:r><m:t>x</m:t></m:r>"
    nested = leaf
    for _ in range(depth):
        nested = f"<m:f><m:num>{nested}</m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>"
    xml = f'<m:oMath xmlns:m="{namespace}">{nested}</m:oMath>'
    if depth < 128:
        model = omml.parse(xml)
        rendered = omml.roundtrip(xml)
        assert isinstance(model, dict) and isinstance(rendered, str)
        assert sum(node.tag == "{" + namespace + "}f" for node in ET.fromstring(rendered).iter()) == depth
    else:
        try:
            omml.parse(xml)
        except omml.OMMLError as error:
            assert error.contract["detail"] == "element nesting depth limit exceeded"
        else:
            raise AssertionError("Expected the native source parser resource refusal")
    assert isinstance(omml.parse(f'<m:oMath xmlns:m="{namespace}">{leaf}</m:oMath>'), dict)
    print(json.dumps({"depth": depth, "reuse": True}))


class InstalledDepth(unittest.TestCase):
    def test_default_stack_parse_xml_model_and_resource_reuse(self):
        clean = {k:v for k,v in os.environ.items() if k not in (
            "RUST_MIN_STACK", "PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH", "PARSANOL_LIB")}
        for depth in (16, 64, 128, 256):
            with self.subTest(depth=depth):
                result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()),
                                         "--depth-probe", str(depth)],
                                        capture_output=True, text=True, timeout=180, env=clean)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(json.loads(result.stdout)["reuse"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--depth-probe":
        probe(int(sys.argv[2]))
    else:
        unittest.main()
