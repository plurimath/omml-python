from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

import omml


def tree(xml):
    def node(element):
        text = element.text or ""
        tail = element.tail or ""
        if not text.strip() and not element.tag.endswith("}t"):
            text = ""
        if not tail.strip():
            tail = ""
        return [element.tag, sorted(element.attrib.items()), text, tail, [node(child) for child in element]]
    return node(ET.fromstring(xml))


class InstalledOMML(unittest.TestCase):
    def test_source_xml_shapes(self):
        self.assertTrue(Path(omml.__file__).is_relative_to(sys.prefix))
        fixture = json.loads(Path(__file__).with_name("ruby-xml-roundtrips.json").read_bytes())
        self.assertEqual(len(fixture), 11)
        for row in fixture:
            with self.subTest(case=row["id"]):
                actual = omml.roundtrip(row["input"], root=row["root"])
                self.assertEqual(tree(actual), tree(row["expected_xml"]))

    def test_distinct_typed_anchor_fields(self):
        namespace = "http://schemas.openxmlformats.org/officeDocument/2006/math"
        xml = f'<m:CT_Anchor xmlns:m="{namespace}" m:simplePos="true"><m:simplePos m:x="125" m:y="-250"/></m:CT_Anchor>'
        model = omml.parse(xml, root="CTAnchor")
        self.assertIs(model["simple_pos"], True)
        self.assertEqual(model["simple_position"], {"x":"125.0","y":"-250.0"})

    def test_typed_refusals_resource_and_reuse(self):
        with self.assertRaises(omml.OMMLError) as malformed:
            omml.parse("<broken>")
        self.assertEqual(malformed.exception.category, "contract")
        with self.assertRaises(omml.OMMLError) as unknown:
            omml.parse("<x/>", root="Unknown")
        self.assertEqual(unknown.exception.category, "invalid_argument")
        namespace = "http://schemas.openxmlformats.org/officeDocument/2006/math"
        deep = "<m:r><m:t>x</m:t></m:r>"
        for _ in range(128):
            deep = f"<m:f><m:num>{deep}</m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>"
        with self.assertRaises(omml.OMMLError) as resource:
            omml.parse(f'<m:oMath xmlns:m="{namespace}">{deep}</m:oMath>')
        self.assertEqual(resource.exception.contract["reason"], "invalid_document")
        self.assertEqual(resource.exception.contract["detail"], "element nesting depth limit exceeded")
        self.assertIsInstance(omml.parse(f'<m:oMath xmlns:m="{namespace}"/>'), dict)

    def test_concurrent_native_calls(self):
        namespace = "http://schemas.openxmlformats.org/officeDocument/2006/math"
        xml = f'<m:oMath xmlns:m="{namespace}"><m:r><m:t>x</m:t></m:r></m:oMath>'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(omml.roundtrip, [xml] * 8))
        self.assertTrue(all(tree(result) == tree(xml) for result in results))


if __name__ == "__main__":
    unittest.main()
