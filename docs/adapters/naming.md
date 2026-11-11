# Naming and listing community adapters

## The naming rule

A community adapter is a Python package published as **`satchel-source-<tool>`**.

| What | Rule | Example |
|---|---|---|
| Distribution name (PyPI) | `satchel-source-<tool>`, lower case, words joined with `-` | `satchel-source-lookml` |
| Import package | `satchel_source_<tool>` | `satchel_source_lookml` |
| `info.kind` and entry-point name | `<tool>` with `_` for `-`. It must match `^[a-z][a-z0-9_]{1,31}$` | `lookml` |
| Entry point | `[project.entry-points."satchel.sources"]`, `<kind> = "satchel_source_<tool>:adapter"` | `lookml = "satchel_source_lookml:adapter"` |
| `display_name` | The tool's own name, as its makers spell it | `LookML` |

`<tool>` is the name of the tool the adapter reads, not your company's name or a word like "connector". When one
tool has more than one kind of input, add the input after the tool, for example `satchel-source-looker-api`
(kind `looker_api`) next to `satchel-source-lookml`.

### Kinds must be unique

Satchel finds adapters by their entry-point name, so two installed packages with the same kind conflict: only one
of them loads, and which one isn't defined. So:

- Do not use a kind that a built-in adapter uses: `dbt`, `cube` and `sqlmesh`. To improve one of those, open a pull request against this repository instead.
- Before you publish, check that the PyPI name and the kind are not taken by a [listed adapter](wanted.md#community-adapters). If they are, talk to that adapter's author in an issue first.

### Don't look official

- Do not call the package "official", and do not use Satchel's logo in it.
- Only adapters maintained by the Satchel maintainers live under the `getsatchel` GitHub organisation.
- Say in your README who maintains the package, and link to the source code.

PyPI does not reserve the `satchel-source-` prefix, so anyone can publish a package with that name. A name says
nothing about who wrote the code. Being listed in these docs is what tells users the maintainers have looked at an
adapter, and installed adapters run inside the Satchel process as trusted code. Read an adapter before you install
it on a server.

### In-house adapters

An adapter you only use inside your own organisation does not need to be on PyPI, and can be named however your
internal package index wants. Its kind still must not clash with a built-in or listed adapter. Prefixing the kind
with your organisation (for example `acme_warehouse_docs`) avoids clashes.

## Getting listed

Listed adapters appear in the [community adapters](wanted.md#community-adapters) table. To be listed:

1. Follow the naming rule above, publish the package to PyPI, and make the source code public under an OSI-approved licence.
2. Meet at least the *experimental* criteria in [Maturity tiers](maturity.md), and set `AdapterInfo.maturity` honestly.
3. Open a pull request that adds a row to the table: the package, the kind, the tool, the tier, its capabilities, and a link to the repository. If an [integration request](https://github.com/getsatchel/satchel/issues/new?template=integration_request.yml) exists for the tool, link it.

A maintainer reads the code before merging, and checks the same things as for *stable* (see
[Maturity tiers](maturity.md#stable)): no server imports, no undeclared network access, and nothing sensitive in the
snapshot.

Moving an adapter into Satchel itself (making it built in) is decided case by case. It usually needs a *stable*
adapter, a widely used tool, and no heavy dependencies.
