# Leutheria shared skills

The public registry of skills for [Leutheria](https://github.com/AnuragK-O/Leutheria), a
voice assistant for macOS. A skill here is a short, readable list of steps, each one a call
to one of Leutheria's built-in tools. Nothing in this repository is code that runs on
anyone's Mac: the app reads a skill as data, shows every step before it installs it, and
asks again on its first run.

## Sharing a skill

1. In Leutheria, open the Library, pick one of your learned skills and choose **Share**. The
   app removes your home folder and personal values, shows you exactly what will be
   published, and opens GitHub's "new file" page here with the file filled in.
2. Submit it as a pull request from your own GitHub account. The app never holds GitHub
   credentials.
3. Automated checks run within a few minutes and comment on the PR:
   - **Structure**: the PR adds exactly one file, `skills/<name>/<content_hash>.json`, and
     changes nothing else; the file is valid against the tool catalog of the Leutheria
     version this registry is pinned to (`LEUTHERIA_REF`); it is at most 64 KiB.
   - **Screening**: a static analyser, then an AI reviewer, read the steps and the
     description.
   - Clean submissions are merged and published automatically. Doubtful ones get the
     `needs-human-review` label and a maintainer looks at them. Clearly bad ones are closed
     with the reasons.

Rules for a submission:

- One file per PR, never an edit or a deletion of an existing file. A new version of your
  skill is a new file (its steps changed, so its `content_hash` and file name changed).
  Changing only the wording keeps the same `content_hash`, so it can't be republished.
- A name belongs to whoever published it first; other people pick another name.
- No `source` field (the installing app sets it). `run_command` steps are allowed, and every
  user is asked before each one runs, every time.
- Descriptions are not shown to the AI model verbatim; the app writes its own from your
  summary and the step list. Write it for people.

By submitting you dedicate the skill file to the public domain under CC0 1.0 (see LICENSE).

## Repository layout

| Path | What | Who writes it |
|---|---|---|
| `skills/<name>/<content_hash>.json` | one published version | contributors, via PR |
| `reviews/<name>/<content_hash>.json` | its review record: version, author, published_at, screening result | CI only |
| `yanked.json` | withdrawn versions | the yank workflow |
| `index.json`, `index.json.sig` | the signed index the app reads | the publish workflow only; never edit by hand |
| `keys/public_keys.json` | public signing keys | maintainer, on rotation |
| `LEUTHERIA_REF` | the Leutheria commit whose `validate.py` and `agent/tool_catalog.json` CI uses | maintainer |
| `tools/` | the scripts every workflow runs (testable locally) | maintainer |
| `screen/` | static analyser + AI screener | maintainer |

## For the maintainer

### How publishing works

- **`submission.yml`** (`pull_request_target`): checks out the trusted scripts from the base
  commit and the PR only as data, then `tools/check_submission.py` (structure),
  `screen/screen.py` (screening, only if structure passed), `tools/decide.py` (decision
  table). On merge it squash-merges exactly the screened head (`--match-head-sha`), writes
  the review record with `tools/record_review.py` (which assigns the version: previous max
  for the name + 1, fixed forever), and calls `publish.yml`. Name ownership is checked three
  times: against the PR's base (structural), against current main just before merging, and
  by `record_review.py` against main as it is when the record is written, so two PRs racing
  for one new name can't make the second "version 2" of the first's skill. `build_index.py`
  also fails if any name has records by more than one author.
- **`publish.yml`** (push to main, called, or manual): `tools/build_index.py`,
  `tools/sign_index.py`, `tools/verify_index.py`, and commits `index.json` + `.sig` if
  they changed. A skill file without a review record is never indexed, so a file that
  reached main some other way stays unpublished.
- Pushes made by workflows don't trigger other workflows, so `submission` and `yank` call
  `publish` themselves.

Repository settings this needs: secrets `ANTHROPIC_API_KEY` and `REGISTRY_SIGNING_KEY`;
labels `needs-human-review`, `human-approved` and `human-approved-override`; Actions allowed to create and approve
pull requests and to push to `main` (if `main` is protected, let `github-actions[bot]` bypass
it).

### Reviewing a flagged submission

Read the PR's comment (static and screener findings) and the file itself. To approve:

1. **Approve the PR** (Files changed → Review changes → Approve) while it shows the commit you
   read. The approval is bound to that commit.
2. Add the `human-approved` label. The label is only a request to check: `tools/human_approval.py`
   looks for an APPROVED review whose `commit_id` is exactly the PR's current head, left by
   someone with write, maintain or admin access who isn't the author (a later "request
   changes" or a dismissal withdraws it). If there is none, the label is removed and the PR
   says why. Otherwise the workflow re-checks structure, re-screens, merges exactly that head
   (`--match-head-sha`) and records `verdict: human_approved`.

A new push to the PR removes both approval labels; review the new commit and start again.

A maintainer's approval overrides the AI screener, but **never a static-analysis block**: an
approved PR whose static verdict is `reject` is closed. If the block is a false positive you
have checked, use the separately named `human-approved-override` label instead (same review
requirement); the record says `human_approved` and the merge comment says the static block was
overridden. An approval with no static result at all (the screening step crashed) goes back
to review rather than merging. To reject, close the PR with a comment.

### Yanking

Actions → **yank** → Run workflow with the skill name, its `content_hash` and a reason. The
file stays (installed copies must still verify); the next index marks it `yanked: true`, which
hides it from Discover and shows installed copies as withdrawn. Installed copies are not
removed. Yanking a version doesn't free its name.

### Index format and expiry

`index.json` is `{"format": 1, "generated_at", "expires_at", "catalog_version", "skills": [...]}`
(each skill entry is listed in `tools/build_index.py`). Both timestamps are UTC in exactly the
form `YYYY-MM-DDTHH:MM:SSZ`, and both are covered by the signature.

**`expires_at`** is `generated_at` + 7 days (`common.INDEX_VALIDITY_DAYS`). A client must refuse
the index (treat the registry as unavailable, not merely warn) when `expires_at` is missing or
malformed, is not later than `generated_at`, is more than 8 days after `generated_at`
(`common.INDEX_MAX_VALIDITY_DAYS`), or is at or before the current time. That caps how long a
mirror, proxy or attacker replaying an old, validly signed index can hide a yank. `format` stays
1: the field is required from now on, and no client shipped before it.

`publish` runs on a daily schedule with `build_index.py --refresh`, which rewrites and re-signs
the index even when no skill changed, so the published index is never more than about a day old.
If the schedule stops (GitHub disables scheduled workflows after 60 days without repository
activity), clients start refusing the registry 7 days after the last run: run **publish** by
hand, and re-enable the workflow.

### Signing keys and rotation

```bash
python tools/keygen.py --private-out new-key.pem --keys keys/public_keys.json
```

prints `{"key_id", "public_key"}` (Ed25519, raw key in base64; key_id = first 16 hex of its
sha256) and adds it to `keys/public_keys.json`. Put the PEM text in the
`REGISTRY_SIGNING_KEY` secret and delete the file.

To rotate: generate the new key; add its public entry to Leutheria's
`agent/agent/core/registry_keys.py` (the client accepts any listed key) and ship that
release; once enough users have it, replace the secret with the new key and let `publish`
re-sign; later remove the old key from the client list and from `keys/public_keys.json`. If
the key leaked, remove it from the client in an urgent release first: until then a leaked key
can sign an index the app will accept.

### Changing the pinned Leutheria version

When Leutheria adds or changes tools, it re-exports `agent/tool_catalog.json`. Point
`LEUTHERIA_REF` at a commit containing the new catalog. The next publish re-validates every
non-yanked skill against it and fails if one no longer validates (say a tool it uses was
removed): yank that skill, then publish again.

### Recovering

If a merge's review record couldn't be pushed, the skill is merged but pending. Run, from a
checkout of main:

```bash
python tools/record_review.py --repo . --path skills/<name>/<hash>.json --verdict human_approved --author <login> --pr <n>
```

commit, push, and run **publish** by hand.

### Testing locally

`tools/local_pipeline.py` runs the whole flow on a copy, with the same scripts:

```bash
python tools/keygen.py --private-out /tmp/k.pem --keys /tmp/keys.json
python tools/local_pipeline.py submit --work /tmp/reg --leutheria <Leutheria checkout> \
  --skill my_skill.json --author me --key-file /tmp/k.pem --keys /tmp/keys.json
```

Leutheria's `agent/scripts/registry_pipeline_check.py` exercises all of it.
