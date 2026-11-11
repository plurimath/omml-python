# Native OMML Python transport

The host-platform wheel bundles the generated Rust OMML model library.
`omml.parse(xml, root="OMath")` returns its generated model JSON.
`omml.roundtrip(xml, root="OMath")` returns its XML serialization.
All 186 model names are accepted as explicit roots. `Runtime(path)`
supports a caller-managed ABI library.

Typed refusals expose the native contract, including its model and
attribute location. Source scalar bindings preserve OMML coordinate and
integer representations. DateTime uses a qualified ISO input profile;
Ruby's broader DateTime.parse language is outside that profile.

Windows x86_64 wheels use the independently sealed `native-source.win_amd64.json`
and bundle `omml.dll`. The qualified production build uses
`--config profile.dev.package.omml-rs.opt-level=1` with debug information and
incremental compilation disabled; SDK optimization remains zero. An unoptimized
debug build overflowed the Windows caller stack at depth 64, so it is not shipped.
The production artifact passes default-caller depths 16/64 parse, XML write and model
JSON, and typed source-parser refusal/reuse at 128/256 without changing stack size
or parser limits. The existing source namespace, coordinate formatting and
DateTime profile limitations still apply.

For preparation on a source-validation host, use `scripts/build-wheel.py` with
`--platform-tag win_amd64 --prepare-dir DIR --target-metadata WINDOWS_METADATA`
and the normal pinned library, source and source-metadata arguments. Build the
actual wheel on Windows with `OMML_NATIVE` and `OMML_NATIVE_RECEIPT` pointing at
the qualified DLL and prepared `build-info.json`. Neither variable is required
after installation. Linux defaults and seals remain in `native-source.json`.
