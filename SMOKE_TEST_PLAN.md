# Post-build smoke test plan

Working document. We iterate on this until we agree, then implement.

## Goal

After either build workflow (`build-pr.yaml`, `build-main.yaml`) publishes a snap to
the store, automatically run the manual validation checklist used for Renovate
PRs. A red/green check on the commit/PR is the only required feedback.

## Trigger

A new workflow `.github/workflows/smoke-test.yaml`:

```yaml
on:
  workflow_run:
    workflows: ["Build PR when labeled", "Build main branch"]
    types: [completed]
```

Runs only when `github.event.workflow_run.conclusion == 'success'`. Channel to
test is derived from the upstream run:

- main: `latest/edge`
- PR:   `latest/edge/pr-<PR#>` (extracted from the build workflow run)

`workflow_run` is preferred over a `needs:` job inside the existing workflows
because the shared reusable workflow (`canonical/inference-snaps-dev/...build-publish-snap.yaml@v2`)
doesn't surface the published channel as an output and we don't want to fork it.

## Runners & architecture matrix

Matches the build matrix:

- `[self-hosted, amd64, noble, medium]`
- `[self-hosted, arm64, noble, medium]`

`fail-fast: false` so a failure on one arch doesn't mask the other.

## Test environment

Each matrix job spins up a fresh LXD VM on the runner. LXD is not pre-installed
on the `noble medium` self-hosted runners, so the workflow installs it itself:

```yaml
- name: Install LXD
  run: |
    sudo snap install lxd
    sudo lxd init --auto
```

(`canonical/action-build` does install LXD, but it's a build action that also
pulls in snapcraft and sets `SNAPCRAFT_BUILD_ENVIRONMENT=lxd` — wrong tool for
a test workflow. Explicit two-line install is cleaner.)

Then per matrix job:

```
lxc launch ubuntu:24.04 owui-smoke --vm
lxc exec owui-smoke -- snap install open-webui --channel=<derived-channel>
lxc exec owui-smoke -- snap install gemma4 --channel=stable
lxc exec owui-smoke -- snap connect open-webui:config gemma4:open-webui
```

Rationale: real systemd + real strict confinement, clean per-run state, no
cross-contamination from prior runs on the host runner. Tear down on exit
(`lxc delete -f`) including on failure.

The model load is allowed to be slow — the harness polls, hard cap **15 min**
on the gemma4 model-registration poll.

## Test harness

A small Python script under `tests/smoke/` driven by `pytest`, executed inside
the VM via `lxc exec`. Drives Open WebUI through its REST API.

### Steps (mapped to the manual checklist)

| Manual step | Automated check |
|---|---|
| Snap installs | `snap install` exit code |
| Server does not crash | poll `journalctl -u snap.open-webui.server` for traceback while waiting for port 8080 |
| UI reachable | `GET /health` → 200 (poll, with timeout) |
| Admin signup works | `POST /api/v1/auths/signup` with fixture creds, capture JWT |
| Release notes / version | `GET /api/config` (or `/api/version`) — exact-match assert against the pinned version in `dependencies/requirements.txt` (currently `open-webui==0.9.2`). Test reads that file at runtime so it stays in sync with each Renovate bump |
| gemma4 endpoint registered | poll `GET /api/models` until a gemma model appears (covers the OpenAI-compatible endpoint registration via snap interface; `GET /ollama/api/tags` is wrong here — gemma4 is not an Ollama backend) |
| Text prompt | `POST /api/chat/completions` text-only, assert non-empty `choices[0].message.content` |
| Image prompt | `POST /api/chat/completions` with `image_url` content part referencing fixture PNG, assert non-empty response |
| Audio prompt | `POST /api/v1/audio/transcriptions` with fixture WAV, assert transcript non-empty |
| PDF / RAG prompt | upload fixture PDF via `POST /api/v1/files/`, poll `GET /api/v1/files/{id}/process/status` until `completed`, then `POST /api/chat/completions` with `"files": [{"type": "file", "id": "<uuid>"}]` and a question whose answer is a synthetic fact only in the PDF; assert the answer substring appears in `choices[0].message.content` |

Dictate-mode (TTS) deliberately excluded from v1 — it adds another moving part. Can be added later.

## Fixtures

Committed under `tests/smoke/fixtures/`:

- `image.png` — small, generic, license-clean
- `audio.wav` — short, known transcript (assertion compares case-insensitive
  substring, not exact match — STT output drifts between model versions)
- `rag.pdf` — one-paragraph synthetic document containing a made-up fact not
  present in any model's training data (e.g. "Project Zephyr has a budget of
  42 million credits"). Question asked: "What is Project Zephyr's budget?"
  Assertion: case-insensitive substring `"42 million"` in response. Synthetic
  content ensures a correct answer requires the RAG pipeline, not model memory.

## Failure handling

- Each step is a separate pytest test → easy to read which one failed in the
  Actions UI.
- On failure: dump `journalctl -u snap.open-webui.server` and `snap logs` into
  the job log before VM teardown.
- No PR comment, no status-check beyond the workflow's own green/red.

## Why API-only (no browser/DOM)

The manual checklist phrases steps as "UI shows X", but the harness drives Open
WebUI purely through its REST API. Tradeoff, stated explicitly:

**What API-only catches** (and where upstream bumps actually tend to break):
- snap install + strict confinement
- server start / crash on boot
- snap-interface plumbing (`open-webui:config ↔ gemma4:open-webui`) and the
  periodic-job that registers the model
- prompt / image / audio handler paths inside Open WebUI

**What API-only misses:**
- frontend bundle breakage (broken JS, blank page)
- UI routing bugs (admin-signup form missing, release-notes panel 404s)
- regressions in the React app that don't surface on the JSON API

**Why we accept that for v1:** Open WebUI's frontend is upstream's code, not
ours. Selector maintenance against a churning React app is a real tax, and
Playwright inside an LXD VM is meaningful extra weight (browser install,
headless display, flake budget) for a class of bug that is rarer than the
backend regressions the manual checklist exists to catch.

**Escalation trigger:** if we ever ship a release where the API smoke passed
but a user-visible UI regression slipped through, add a thin Playwright layer
on top — page loads, admin-signup form present in DOM, version string visible.
Not before.

## Out of scope (v1)

- Dictate mode (voice-in + voice-out)
- TTS validation
- Performance / latency assertions
- Testing across multiple gemma4 model variants

## Resolved decisions

1. **Channel derivation from `workflow_run`** — use
   `github.event.workflow_run.pull_requests[0].number` directly. The empty-
   `pull_requests[]` pitfall is a fork-PR problem; same-repo label-triggered
   PRs (our case) populate it. No artifact handoff needed.
2. **gemma4 cold-start budget** — 15 min hard cap on the model-registration
   poll.
3. **LXD on self-hosted runners** — not pre-installed. Workflow installs it
   via `snap install lxd && sudo lxd init --auto` before each matrix job.
   `canonical/action-build` does install LXD but as a side-effect of setting
   up snapcraft for builds — wrong tool for a test workflow.
4. **Version-string assertion** — exact match. Test reads
   `dependencies/requirements.txt` at runtime and asserts `GET /api/config`
   reports the same version. Auto-stays-in-sync with Renovate.
5. **Fixture audio** — generic "the quick brown fox" WAV, case-insensitive
   substring assert on `"fox"` in the transcript.
6. **PDF / RAG flow** — upload via `POST /api/v1/files/`, poll
   `GET /api/v1/files/{id}/process/status` until `completed` (hard cap 2 min),
   then chat completion with `"files": [{"type": "file", "id": "<uuid>"}]`.
   Synthetic fixture content ("Project Zephyr, 42 million credits") ensures the
   model cannot answer from training data alone.

## Iteration log

- 2026-05-13: initial draft. Decisions locked in: gemma4 stable, slow model
  startup OK, red/green only, run on every build arch (amd64 + arm64).
- 2026-05-13: 5 open questions resolved (see above). Ready to implement.
- 2026-05-14: PDF/RAG test added. Flow: upload → poll indexing → chat with file ref. Synthetic fixture chosen so a correct answer requires the RAG pipeline.
