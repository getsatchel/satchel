# Adapter maturity tiers

Every adapter declares a maturity in its manifest (`AdapterInfo.maturity`): `experimental`, `beta` or `stable`.
`satchel source kinds` and the **Sources** page in the UI show it next to the adapter, so users can tell how much to
rely on it. This page says what each tier promises and what an adapter needs to move up.

The author sets the tier. For adapters listed in Satchel's docs (see [Naming and listing](naming.md#getting-listed)),
the maintainers check that the declared tier meets the criteria below, and ask for it to be lowered if it doesn't.

## The tiers

| Tier | What users can expect |
|---|---|
| **experimental** | It works on the author's fixtures. The config, the mapping and the output can change in any release. Use it to try the tool out, not for context your agents depend on. |
| **beta** | It is tested on real projects and its config is mostly settled. Changes that alter the output or the config are listed in its release notes, but can happen in a minor release. |
| **stable** | Its config and mapping follow semver: a change that renames options or changes which items a sync produces needs a new major version of the adapter. Bugs are fixed, and it keeps up with the SDK. |

The built-in dbt, Cube and SQLMesh adapters are currently *beta*.

## experimental

Any adapter can start here. The minimum:

- It passes `assert_conformant` with a committed golden file.
- `info.kind` equals the entry-point name, and the package follows the [naming rule](naming.md).

## beta

Everything in *experimental*, and:

- **Real fixtures.** Its tests use real, trimmed artifacts from the tool (not hand-written toys), for each tool version it claims to support.
- **Malformed input is tested.** Bad or unexpected nodes produce `SourceWarning`s, not exceptions, and the tests prove it.
- **Documented.** A README lists the supported tool versions, every config option, the mapping from the tool's concepts to Satchel items, and what is not mapped.
- **Used.** It has synced a real project (not the author's fixture) end to end: `satchel source add` and `satchel source sync`, with the result checked in the UI or with `satchel search`.

## stable

Everything in *beta*, and:

- **Production use.** At least two teams other than the author use it in production, or one for three months or more.
- **Semver for its config and output.** As described above, from the release that declares *stable*.
- **A release record.** Release notes for each version, and a support window: say which tool versions are supported and for how long older ones are kept.
- **CI.** Tests run in CI against the SDK floor it declares and the latest SDK ([compatibility policy](compatibility.md#what-adapter-authors-should-declare)).
- **Maintained.** Issues get a first response within two weeks, and it supports a new SDK minor release within a month. It supports a new SDK major before the deprecation window ends.
- **Reviewed.** For a listed adapter, a Satchel maintainer has read the code: no server imports, no network access without the `network` capability, no writes outside the snapshot, and nothing sensitive (credentials, raw data rows) in `source_meta` or warnings.

## Promotion and demotion

To promote a listed adapter, open an issue labelled `adapter` that links the evidence for each criterion: CI runs,
fixtures, README sections and production users (a link or a note from them). A maintainer reviews it, and the
adapter's next release changes `AdapterInfo.maturity`.

A listed adapter can be moved down a tier, after a warning in an issue, when it stops meeting its tier. Examples:
it does not support a new SDK minor within a month, it has a security issue that isn't fixed, or it is abandoned.
Moving down only changes the listing and the badge the author is asked to set. Satchel never disables an installed
adapter on its own.
