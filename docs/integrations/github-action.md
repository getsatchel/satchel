# GitHub Action: preview and publish a context repo

Keep your context repo (the folder with `satchel.config.yaml`: glossary, queries, guides, semantic
models, eval suites) on GitHub, and let the Satchel Action check every pull request against your
server:

- **On a pull request** it uploads the folder and gets a **preview** release: the published
  release plus the pull request's changes. If the workspace has a [publish gate](../concepts/evals.md#the-publish-gate),
  it runs the required eval suites on the preview and on the published release, comments the
  comparison on the pull request, and fails the check when a measure regresses.
- **On a push to the default branch** (the merge) it imports and **publishes** the folder, the
  same as `satchel sync import`.

The Action needs Satchel 0.2.0 or later. Use the tag that matches your server's version.

## Workflow

```yaml
# .github/workflows/satchel.yml in your context repo
name: satchel
on:
  pull_request:
    paths: ["context/**"]
  push:
    branches: [main]
    paths: ["context/**"]

permissions:
  contents: read
  pull-requests: write   # the preview comment

concurrency:
  group: satchel-${{ github.ref }}
  cancel-in-progress: true

jobs:
  context:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: getsatchel/satchel/action@v0.2.0
        with:
          url: https://satchel.example.com
          path: context
          # A curator token is enough for pull requests; publishing needs an admin token.
          token: ${{ github.event_name == 'push' && secrets.SATCHEL_ADMIN_TOKEN || secrets.SATCHEL_TOKEN }}
```

Create the tokens for two service users and store them as repository secrets (`--role` applies
when the user is created):

```bash
satchel token create -w acme --user ci@acme.example --role curator --name github-previews
satchel token create -w acme --user ci-publish@acme.example --role admin --name github-publish
```
 Make the
`context` check required in the branch protection rules, so a pull request that fails the gate
cannot merge.

Pull requests from forks get no secrets, so the Action cannot run for them. Run it for branches
of the repository itself.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `url` | required | The Satchel server. It must be reachable from the runner. |
| `token` | required | A personal access token: curator or above to preview, admin to publish. |
| `path` | `.` | The folder holding `satchel.config.yaml`. |
| `mode` | `auto` | `auto`: pull request events preview, other events publish. Or `preview` / `publish`. |
| `run-evals` | `true` | Run the gate's suites on the preview. |
| `fail-on-gate` | `true` | Fail the job unless the gate passes (or there is no gate). |
| `wait-minutes` | `30` | How long to wait for the eval runs. |
| `comment` | `true` | Post the result on the pull request, and update the same comment on later pushes. |
| `github-token` | `github.token` | Token for the comment. |

Outputs: `preview-id`, `gate-status` (`none`, `pass`, `fail`, `pending`, or `running` when the
wait ran out) and, in publish mode, `release-seq`.

## What a preview does and does not change

A pull request preview never changes what agents are served or what curators are editing:

- Changed files get new versions that are **not** made current, so the Library, Changes and the
  published release stay as they were. Files new in the pull request are hidden from the Library
  until the merge. Deleted files are left out of the preview.
- The preview shows on the Changes page (the comment links it) with its test box and the gate,
  but without a **Publish** button: it is published by merging the pull request. When the merge
  is imported, the versions the preview created are reused, so the published content is exactly
  what the gate measured.
- Only the files are previewed. Connections, `publish:` settings, sources declared in
  `satchel.config.yaml` and **eval suites** are not applied until the merge. If the pull request
  changes a suite, the comment says so: the gate still runs the suites already on the server.
- Every new commit on the pull request builds a new preview; old previews stay in the release
  history.

## Publishing on merge

`POST /v1/sync/import` imports the folder and publishes a release when anything changed, like
`satchel sync import`. It needs an admin token, because `satchel.config.yaml` can change the
workspace's connections and publish gate. Sources declared in the config are not synced from an
upload (their paths would be read on the server); sync them from the Sources page, on a schedule,
or with `satchel source sync`. The gate is checked on the pull request, not again on the merge.

## API

The Action is a shell script (`action/satchel-context.sh`) over three calls, which other CI
systems can use the same way:

```bash
tar -czf context.tgz -C context .
curl -H "Authorization: Bearer $TOKEN" -F bundle=@context.tgz -F ref="$GIT_REF@$SHA" \
  "$SATCHEL_URL/v1/sync/preview"                       # -> preview_id, created/updated/removed
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
  "$SATCHEL_URL/v1/previews/$PREVIEW_ID/evals"         # start the gate; poll with GET
curl -H "Authorization: Bearer $TOKEN" -F bundle=@context.tgz -F ref="$GIT_REF@$SHA" \
  "$SATCHEL_URL/v1/sync/import"                        # on merge (admin)
```

A bundle is at most 20 MB (100 MB unpacked, 5,000 files) and may hold only files and folders.
Both calls are audited (`sync.preview`, `sync.import`, with the `ref`).
