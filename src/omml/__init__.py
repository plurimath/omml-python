"""Thin transport over the canonical generated OMML Rust model library."""

from ctypes import CDLL, c_char_p, c_size_t, c_uint32, c_void_p, string_at
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig


class OMMLError(ValueError):
    def __init__(self, error):
        self.category = error["category"]
        self.contract = error.get("contract")
        self.detail = error
        super().__init__(error.get("message") or str(self.contract))


class NativeRuntimeError(RuntimeError):
    pass


class Runtime:
    def __init__(self, library_path=None):
        if library_path is None:
            directory = Path(__file__).with_name("_native")
            receipt = json.loads((directory / "build-info.json").read_bytes())
            expected = "omml.dll" if sys.platform == "win32" else "libomml.so"
            platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
            if receipt["abi_version"] != 1 or receipt["library"] != expected or receipt["platform_tag"] != platform:
                raise NativeRuntimeError("Unsupported bundled OMML ABI")
            library_path = directory / receipt["library"]
            if sha256(library_path.read_bytes()).hexdigest() != receipt["sha256"]:
                raise NativeRuntimeError("Bundled OMML library digest mismatch")
        self._library = CDLL(str(library_path))
        library = self._library
        library.omml_abi_version.argtypes = []
        library.omml_abi_version.restype = c_uint32
        if library.omml_abi_version() != 1:
            raise NativeRuntimeError("Unsupported OMML ABI")
        library.omml_roundtrip_json_v1.argtypes = [c_char_p, c_size_t, c_char_p, c_size_t]
        library.omml_roundtrip_json_v1.restype = c_void_p
        library.omml_free_json_v1.argtypes = [c_void_p]
        library.omml_free_json_v1.restype = None

    def evaluate(self, xml, root="OMath"):
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        if not isinstance(xml, bytes) or not isinstance(root, str):
            raise TypeError("OMML input must be str or bytes; root must be str")
        root_bytes = root.encode("utf-8")
        pointer = self._library.omml_roundtrip_json_v1(root_bytes, len(root_bytes), xml, len(xml))
        if not pointer:
            raise NativeRuntimeError("OMML native ABI returned a null result")
        try:
            reply = json.loads(string_at(pointer))
        finally:
            self._library.omml_free_json_v1(pointer)
        if not isinstance(reply, dict) or reply.get("abi_version") != 1:
            raise NativeRuntimeError("Invalid OMML native ABI envelope")
        if reply.get("ok") is False and isinstance(reply.get("error"), dict):
            raise OMMLError(reply["error"])
        if reply.get("ok") is not True or not isinstance(reply.get("result"), dict):
            raise NativeRuntimeError("Invalid OMML native result")
        return reply["result"]


@lru_cache(maxsize=1)
def _runtime():
    return Runtime()


def parse(xml, root="OMath"):
    """Return the Rust generated model's official serialized JSON object."""
    return _runtime().evaluate(xml, root)["model"]


def roundtrip(xml, root="OMath"):
    """Parse and serialize XML through the Rust generated OMML model."""
    return _runtime().evaluate(xml, root)["xml"]
