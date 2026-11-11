# Adapter compatibility policy

This page says what `satchel-adapter-sdk` promises to adapter authors: what counts as a breaking change, how the IR
version relates to the SDK's version, and what an adapter should declare so it keeps working across Satchel
releases.

## Two version numbers

| Version | Where | What it versions |
|---|---|---|
| **SDK version** | `satchel-adapter-sdk` on PyPI (`sdk/pyproject.toml`), for example `1.2.1` | The Python package: the IR models, the adapter contract, the helpers and the conformance kit. |
| **IR version** | `satchel_adapter_sdk.IR_VERSION`, carried in every `SourceSnapshot.ir_version`, for example `1.0.0` | The shape and meaning of a snapshot only. |

Both follow [semantic versioning](https://semver.org). They are tied together in three ways:

1. **The majors are always equal.** SDK `1.x` emits and reads IR `1.x`. A new IR major is released only in a new SDK major.
2. **An IR minor bump is shipped in an SDK minor release.** The IR never changes in an SDK patch release.
3. **The SDK can change without changing the IR.** For example, SDK 1.2 added folder inputs (`FileInput(multiple=True)`) and `AdapterInfo.requires` while the IR stayed at `1.0.0`.

The Satchel server is versioned separately (0.x, pre-alpha). The SDK is the stable contract: you can depend on it even
though the server is not 1.0 yet.

## What the public API is

These are covered by the policy:

- Everything exported from `satchel_adapter_sdk` (its `__all__`): the IR models and their fields, defaults and validation; `AdapterInfo`, `FileInput`, `SourceFiles` and the `SourceAdapter` protocol; the `Capability` and `Maturity` values; `MdlBuilder` and `validate_mdl`; `normalise_fqn`, `fingerprint_bytes` and `is_compatible`.
- `satchel_adapter_sdk.testing.assert_conformant` and the checks it runs, plus the `satchel_adapter_sdk.pytest_plugin` (`--update-golden`).
- The `satchel.sources` entry-point group, and the rule that the entry-point name equals `info.kind`.
- The mapping from IR to context items described in [Writing a source adapter](writing-an-adapter.md#2-the-three-pieces) (for example, `SourceAsset` becomes an `asset_doc`).

Not covered: names that start with an underscore, the modules' internal structure, and anything in the `satchel`
server package. Adapters must not import `satchel.*` at all.

## What counts as breaking

A change is **breaking** if an adapter that was correct and passed the conformance kit could stop working, or
could produce a different result in Satchel, without changing its own code. Breaking changes need a new major
version.

| Change | Bump |
|---|---|
| Remove or rename an exported name, an IR model, a field or a method | major |
| Add a required field, or remove the default of an optional one | major |
| Change what a field means, its units or the item it maps to | major |
| Tighten validation so that a snapshot that used to validate is rejected | major |
| Remove a `Capability` or `Maturity` value, or change the entry-point group | major |
| Add a conformance check that fails adapters that conformed before | major (until then, it runs as a warning) |
| Add an optional field with a default, or a new optional IR model | minor (IR minor too) |
| Add a `Capability` value, a helper, a `FileInput` or `AdapterInfo` option | minor |
| Loosen validation | minor |
| Fix a bug, where documented behaviour stays the same; documentation | patch |

Two things that look harmless are breaking. IR models use `extra="forbid"`, so a renamed field breaks snapshots
that set the old name. And a field whose value Satchel shows to agents must keep its meaning: for example,
`endorsed` must keep meaning "the upstream author declared this".

## How Satchel checks it

Adapters run inside the Satchel process and build their snapshot with the SDK installed next to the server. After
each `load`, the server checks `is_compatible(snapshot.ir_version)`, which compares only the major. A snapshot of a
different major fails the sync with a message that names the adapter and tells you to upgrade it or Satchel. Within
a major, a newer server reads every older snapshot: new fields are optional, and the server never requires one.

Because there is one SDK per environment, installing an adapter can upgrade it. For example, an adapter that needs
SDK `>=1.3` makes pip upgrade the server's 1.2. So each Satchel release works with **every SDK minor release of
its major, from the one it ships with onwards**. A new SDK minor never breaks the server. The release notes of
each Satchel version name the SDK version it ships with.

## What adapter authors should declare

- Depend on `satchel-adapter-sdk>=1.N,<2`, where `1.N` is the lowest SDK minor whose features you use. The [template](../../templates/adapter) starts at `>=1.2,<2`.
- Do not set `ir_version` yourself; leave the default so the snapshot carries the installed SDK's IR version.
- Run your tests in CI against both your floor (`1.N`) and the latest SDK.
- If you use a field that was added in IR `1.N`, your floor must be at least the SDK version that added it.

## Deprecation and major releases

A major release forces every installed adapter to move at once, because an environment holds one SDK. So majors
are rare, and they are announced in advance:

1. Anything a major release removes or changes is first deprecated in a minor release. It keeps working there, but raises a `DeprecationWarning` and is listed in the SDK release notes with its replacement.
2. At least one Satchel minor release ships with the deprecation before the major release.
3. The built-in adapters (dbt, Cube, SQLMesh) move to the new major in the same Satchel release that adopts it. Adapters listed as *stable* (see [Maturity tiers](maturity.md)) are told about the change in their repositories before the release.

Questions about whether a change is breaking go in an issue labelled `adapter`. When in doubt, treat it as
breaking.
