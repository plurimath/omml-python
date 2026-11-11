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
