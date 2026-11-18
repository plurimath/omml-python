"""Replay one pinned plurimath-testsuite omml generation on the installed omml wheel.

The pin (ci/testsuite-pin.yml, plurimath-stage/pin/1) is the same file as the
Ruby gem's spec/fixtures/testsuite-pin.yml: testsuite commit, language,
generation and snapshot digest. ci/testsuite-replay.json is this consumer's own
record of what it executes: the row count and the SHA-256 of the sorted executed
IDs. Every check in docs/stage-corpus-contract.adoc runs before any row does:

* the checkout is at the pinned commit;
* the named ledger entry (not the latest) is selected and matches the pin;
* the snapshot digest is recomputed from the files on disk;
* every payload is listed in provenance.yaml with its bytes, SHA-256 and row
  IDs, and nothing on disk is unlisted;
* row IDs are unique across the generation and the counts match the ledger.

Replay (boundary ``model-roundtrip``): the row's input is read and written
back through the installed package (omml.roundtrip()), and the written XML must equal
the row's expected ``to_xml`` output byte for byte. A row whose expected
outcome is a refusal must be refused. No normalisation of any kind is applied
to the output. A failing row is classed:

* ``output-bytes``: the written XML has the same element tree as the expected
  output, but different bytes;
* ``output-tree``: the element trees differ;
* ``refused``: the package refused an input whose expected outcome is output;
* ``accepted``: the package accepted an input whose expected outcome is a refusal;
* ``known-refusal-accepted``: a KNOWN_REFUSALS row was accepted (the list is stale);
* ``error``: the package raised something other than its refusal error.

KNOWN_REFUSALS lists rows whose input is not well-formed XML. Ruby's parser
recovers from them; this package's parser is strict (W3C) by design, so each
must be refused here, and a listed row that is accepted fails the replay.

The ledger is append-only: a row whose id has an ``-r2`` sibling is superseded
by it and only the sibling runs, as in the Ruby gem's scripts/replay_corpus.rb.
The Ruby model wire (``expected.model``) names Ruby classes and is not compared.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from xml.dom import Node, minidom

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "ci/testsuite-pin.yml"
EXPECTATION = ROOT / "ci/testsuite-replay.json"
REPOSITORY = "https://github.com/plurimath/plurimath-testsuite"
LANGUAGE = "omml"
BOUNDARY = "model-roundtrip"

# Rows refused on purpose: the input is not well-formed XML.
KNOWN_REFUSALS = {
    "omml-equation-arrays-007.model-roundtrip": "bare '&' in text is not well-formed",
    "omml-matrices-017.model-roundtrip": "'&nbsp;' is not an XML entity",
    "omml-unclosed-para.model-roundtrip": "m:oMathPara is never closed",
    "omml-crossed-close.model-roundtrip": "end tag m:r crosses the open m:t",
}


class ReplayError(RuntimeError):
    """The pinned generation failed verification or replay."""


def _yaml():
    import yaml
    loader = getattr(yaml, "CSafeLoader", None)
    if loader is None:
        raise ReplayError("PyYAML without libyaml: the generation must be read with CSafeLoader")
    return lambda data: yaml.load(data, Loader=loader)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pin(path=PIN) -> dict:
    pin = _yaml()(Path(path).read_bytes())
    required = {"schema", "repository", "commit", "language", "generation", "snapshot_digest"}
    if not isinstance(pin, dict) or set(pin) != required:
        raise ReplayError("pin must carry exactly the plurimath-stage/pin/1 keys")
    if pin["schema"] != "plurimath-stage/pin/1" or pin["repository"] != REPOSITORY:
        raise ReplayError("pin schema or repository differs from plurimath-stage/pin/1")
    if not re.fullmatch(r"[0-9a-f]{40}", str(pin["commit"])):
        raise ReplayError("pin commit must be a full 40-hex SHA")
    if pin["language"] != LANGUAGE:
        raise ReplayError(f"this consumer replays the {LANGUAGE} language only")
    if not isinstance(pin["generation"], int) or isinstance(pin["generation"], bool) or pin["generation"] < 1:
        raise ReplayError("pin generation must be a positive integer")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pin["snapshot_digest"])):
        raise ReplayError("pin snapshot_digest must be sha256:<64 hex>")
    return pin


def checkout(pin: dict, destination: Path) -> Path:
    """Fetch exactly the pinned commit into a fresh directory."""
    destination = Path(destination)
    if destination.exists():
        raise ReplayError(f"{destination} already exists; replay needs a fresh checkout")
    git = lambda *args: subprocess.run(["git", *args], cwd=destination, check=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    destination.mkdir(parents=True)
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("remote", "add", "origin", pin["repository"] + ".git")
    git("fetch", "-q", "--depth", "1", "origin", pin["commit"])
    git("checkout", "-q", "--detach", "FETCH_HEAD")
    return destination


def _safe(relative: str) -> str:
    parts = relative.split("/")
    if PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in parts) or "\\" in relative:
        raise ReplayError(f"unsafe path in generation: {relative!r}")
    return relative


def snapshot_digest(directory: Path) -> tuple[str, dict]:
    """sha256 over '<sha256>  <relative path>\\n' lines, ordered by path bytes."""
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ReplayError(f"symlink in generation: {path}")
        if path.is_file():
            relative = _safe(path.relative_to(directory).as_posix())
            files[relative] = path.read_bytes()
    if not files:
        raise ReplayError("empty generation")
    lines = "".join(f"{sha256(files[name])}  {name}\n" for name in sorted(files, key=lambda name: name.encode("utf-8")))
    return "sha256:" + sha256(lines.encode("utf-8")), files


def verify(checkout_root: Path, pin: dict) -> dict:
    """Run every contract check before any row is replayed."""
    load = _yaml()
    root = Path(checkout_root)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()
    if head != pin["commit"]:
        raise ReplayError(f"checkout is at {head}, pin names {pin['commit']}")
    ledger = load((root / "generations" / (pin["language"] + ".yaml")).read_bytes())
    if ledger.get("schema") != "plurimath-stage/ledger/1" or ledger.get("language") != pin["language"]:
        raise ReplayError("ledger schema or language differs")
    entries = [entry for entry in ledger["entries"] if entry.get("generation") == pin["generation"]]
    if len(entries) != 1:
        raise ReplayError(f"ledger has {len(entries)} entries for generation {pin['generation']}")
    entry = entries[0]
    if entry["snapshot_digest"] != pin["snapshot_digest"]:
        raise ReplayError("ledger snapshot digest differs from the pin")
    expected_path = f"stages/{pin['language']}/generations/{pin['generation']:06d}"
    if entry["path"] != expected_path:
        raise ReplayError(f"ledger path {entry['path']} is not {expected_path}")
    directory = root / _safe(entry["path"])
    digest, files = snapshot_digest(directory)
    if digest != pin["snapshot_digest"]:
        raise ReplayError(f"recomputed snapshot digest {digest} differs from the pin")
    provenance = load(files["provenance.yaml"])
    if (provenance.get("language"), provenance.get("generation")) != (pin["language"], pin["generation"]):
        raise ReplayError("provenance language or generation differs from the pin")
    listed = [_safe(payload["path"]) for payload in provenance["payloads"]]
    if len(set(listed)) != len(listed):
        raise ReplayError("provenance lists a payload twice")
    if set(listed) != set(files) - {"provenance.yaml"}:
        raise ReplayError("payload files on disk differ from provenance (missing or unlisted)")
    seen, documents = set(), []
    for payload in provenance["payloads"]:
        data = files[payload["path"]]
        if len(data) != payload["bytes"] or sha256(data) != payload["sha256"]:
            raise ReplayError(f"{payload['path']} bytes or SHA-256 differ from provenance")
        document = load(data)
        ids = [case["id"] for case in document["cases"]]
        if ids != list(payload["rows"]):
            raise ReplayError(f"{payload['path']} row IDs differ from provenance")
        if seen & set(ids) or len(set(ids)) != len(ids):
            raise ReplayError(f"{payload['path']} repeats a row ID")
        seen.update(ids)
        documents.append((payload["path"], document))
    if len(documents) != entry["payloads"] or len(seen) != entry["rows"]:
        raise ReplayError("payload or row count differs from the ledger entry")
    return {"head": head, "entry": entry, "documents": documents, "provenance_sha256": sha256(files["provenance.yaml"])}


def xml_tree(text):
    """The element tree of an XML text, or None when it is not well-formed.
    Used only to class a failing row; it never decides whether a row passes."""
    def visit(element):
        attributes = sorted(
            (attribute.namespaceURI or "", attribute.localName, attribute.value)
            for attribute in element.attributes.values()
            if attribute.namespaceURI != "http://www.w3.org/2000/xmlns/")
        has_elements = any(node.nodeType == Node.ELEMENT_NODE for node in element.childNodes)
        children = []
        for node in element.childNodes:
            if node.nodeType == Node.ELEMENT_NODE:
                children.append(visit(node))
            elif node.nodeType == Node.TEXT_NODE:
                if has_elements and not node.data.strip():
                    continue
                children.append(["text", node.data])
            elif node.nodeType == Node.CDATA_SECTION_NODE:
                children.append(["cdata", node.data])
            elif node.nodeType == Node.COMMENT_NODE:
                children.append(["comment", node.data])
            elif node.nodeType == Node.PROCESSING_INSTRUCTION_NODE:
                children.append(["instruction", node.target, node.data])
        return [element.namespaceURI, element.localName, attributes, children]
    try:
        return visit(minidom.parseString(text.encode("utf-8")).documentElement)
    except Exception:
        return None


def classify(actual: str, expected: str):
    """None when the bytes are equal, else (kind, detail)."""
    if actual == expected:
        return None
    at = next((i for i, (a, e) in enumerate(zip(actual, expected)) if a != e), min(len(actual), len(expected)))
    tree = xml_tree(actual)
    kind = "output-bytes" if tree is not None and tree == xml_tree(expected) else "output-tree"
    return kind, f"differs at character {at}: ...{actual[max(0, at - 80):at + 160]!r}"


def superseded_ids(ids):
    """A row id with an -r2 sibling is superseded by it."""
    return {identifier.replace("-r2", "", 1) for identifier in ids if "-r2" in identifier}


def _package():
    """Import the installed wheel once, never the source checkout."""
    import omml
    if Path(omml.__file__).resolve().is_relative_to(ROOT / "src"):
        raise ReplayError("replay must exercise an installed wheel, not the source checkout")
    return omml


def _refusal_types():
    return (_package().OMMLError,)


def _root(xml: str) -> str:
    """The codec root the input's own root element names (oMathPara or oMath);
    any other root goes to the oMath codec, which refuses it."""
    match = re.match(r"\s*(?:<\?.*?\?>\s*)?<(?:[\w.-]+:)?(\w+)", xml, re.S)
    return "OMathPara" if match and match.group(1) == "oMathPara" else "OMath"


def _roundtrip(xml: str) -> str:
    """What Omml.parse(xml).to_xml does."""
    return _package().roundtrip(xml, root=_root(xml))


def row_outcome(row, roundtrip, refusal_types):
    """None when the row passes, else (kind, detail)."""
    if row.get("options"):
        raise ReplayError(f"{row['id']}: unsupported options")
    try:
        actual = roundtrip(row["input"]["str"])
    except refusal_types as error:
        if row["id"] in KNOWN_REFUSALS or "refusal" in row["expected"]:
            return None
        return "refused", str(error)
    except Exception as error:
        return "error", f"{type(error).__name__}: {error}"
    if row["id"] in KNOWN_REFUSALS:
        return "known-refusal-accepted", actual
    if "refusal" in row["expected"]:
        return "accepted", actual
    return classify(actual, row["expected"]["output"]["str"])


def replay(verified: dict) -> dict:
    rows = []
    for path, document in verified["documents"]:
        if document.get("boundary") == BOUNDARY:
            rows.extend({**case, "payload": path} for case in document["cases"])
    ids = {row["id"] for row in rows}
    for identifier in KNOWN_REFUSALS:
        if identifier not in ids:
            raise ReplayError(f"{identifier}: listed known refusal is not in the corpus")
    superseded = superseded_ids(ids)
    refusal_types = _refusal_types()
    outcomes, executed = [], []
    for row in rows:
        if row["id"] in superseded:
            continue
        executed.append(row["id"])
        failure = row_outcome(row, _roundtrip, refusal_types)
        kind, detail = failure if failure else (None, None)
        outcomes.append({"id": row["id"], "payload": row["payload"], "kind": kind,
                         "failure": detail[:800] if detail else None})
    kinds = {}
    for outcome in outcomes:
        if outcome["kind"]:
            kinds[outcome["kind"]] = kinds.get(outcome["kind"], 0) + 1
    if len(executed) + len(superseded) != len(rows):
        raise ReplayError("a superseded id has no row in the corpus")
    return {
        "executed_ids": sorted(executed),
        "executed": {BOUNDARY: {"rows": len(executed),
                                "ids_sha256": sha256("".join(i + "\n" for i in sorted(executed)).encode("utf-8"))}},
        "total_rows": len(rows),
        "superseded": sorted(superseded),
        "known_refusals": [{"id": i, "reason": why} for i, why in KNOWN_REFUSALS.items()],
        "kinds": kinds,
        "failed": sum(kinds.values()),
        "outcomes": outcomes,
    }


def run(pin_path=PIN, expectation_path=EXPECTATION, workdir=None, evidence=None) -> dict:
    pin = load_pin(pin_path)
    expectation = json.loads(Path(expectation_path).read_text(encoding="utf-8"))
    if {key: expectation.get(key) for key in ("commit", "generation", "snapshot_digest")} != \
            {key: pin[key] for key in ("commit", "generation", "snapshot_digest")}:
        raise ReplayError("ci/testsuite-replay.json describes a different pin")
    workdir = Path(workdir or ROOT / ".testsuite")
    verified = verify(checkout(pin, workdir), pin)
    report = {"pin": pin, "provenance_sha256": verified["provenance_sha256"], **replay(verified)}
    if evidence:
        Path(evidence).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"executed": report["executed"], "total_rows": report["total_rows"],
                      "superseded": report["superseded"], "kinds": report["kinds"], "failed": report["failed"]}, indent=2))
    shown = [o for o in report["outcomes"] if o["kind"]][:40]
    for outcome in shown:
        print(f"FAIL {outcome['id']} [{outcome['kind']}] {outcome['failure'][:200]!r}")
    if report["executed"] != expectation["executed"]:
        raise ReplayError("executed rows or IDs differ from ci/testsuite-replay.json")
    if report["failed"]:
        raise ReplayError(f"{report['failed']} of {len(report['outcomes'])} pinned rows disagree with the installed wheel {report['kinds']}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", default=PIN)
    parser.add_argument("--expectation", default=EXPECTATION)
    parser.add_argument("--workdir")
    parser.add_argument("--evidence", default=ROOT / ".ci-evidence/corpus-replay.json")
    args = parser.parse_args()
    Path(args.evidence).parent.mkdir(exist_ok=True)
    run(args.pin, args.expectation, args.workdir, args.evidence)


if __name__ == "__main__":
    main()
