# satchel-source-example

A minimal, working Satchel source adapter. Copy this folder, rename the package and the entry point,
and replace the parsing in `satchel_source_example/__init__.py`.

```bash
uv sync                                  # or: pip install -e . pytest
uv run pytest --update-golden            # first run writes tests/golden/snapshot.json
uv run pytest                            # conformance kit + your own tests
uv run satchel source validate example --export tests/fixtures/export.json   # needs satchel installed
```

The full walkthrough is in [`docs/adapters/writing-an-adapter.md`](../../docs/adapters/writing-an-adapter.md);
the contract (IR, rules, versioning) in [`sdk/README.md`](../../sdk/README.md).
