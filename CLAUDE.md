# CLAUDE.md

Working notes for Claude Code sessions on RDS Guard. Read this before
changing code, releasing or testing. README.md describes the product.
docs/plans/ holds the written plans, each with a status line.

## Layout in one paragraph

Two images are built from this repo. The **satellite**
(`ghcr.io/tubalainen/rds-guard`, root `Dockerfile`) is headless. It handles
the RTL-SDR or Airspy input (`sdr.py`, `dongles.py`, `receivers.py`,
`channelizer.py`), RDS decoding with redsea, TA and alarm detection
(`rds_guard.py`, `pipeline.py`), recording, and the uplink to the aggregator
(`uplink.py`). The **aggregator** (`ghcr.io/tubalainen/rds-guard-aggregator`,
`aggregator/`) handles transcription (`transcriber.py`), the map and
geolocation (`geolocate.py`, `gazetteer.py`, `roads.py`, `geo_service.py`),
Trafikverket, push, MQTT and the web UI on 8080. Ingest is on 8081.

## Principles

- **Everything must run locally, on CPU, without paid services.**
  ElevenLabs (transcription) and the Claude API (geolocation) are opt-in
  improvements, and each falls back to a local path when no key is set
  (`TRANSCRIPTION_ENGINE=elevenlabs` + `TRANSCRIPTION_FALLBACK=local`).
  Every new feature needs a free local path. State the per-message cost of
  any paid option before proposing it.
- **Stay in scope.** What matters is TA (traffic announcements) and PTY
  Alarm (emergencies). Don't log or build on RT+ or EON. The AF list is
  diagnostic only.
- **A station is frequency + PI, not PI alone.** P4 stations share PI
  0xE224 ("P4 SR") for the national programme and send their own PI in
  regional hours, e.g. 0xE824 on 103.5. Station names and areas are set in
  Admin → Stationer. Events are tracked per station (frequency). See
  docs/plans/stationer.md.
- **Never truncate event text in the UI.** Show the whole message.

## How to work

- The chat is in Swedish, and so is the web UI. README, docs, code
  comments and release notes are in English.
- For larger changes, write a plan in `docs/plans/` first, ask questions
  and challenge the idea where it is weak. Wait for approval before
  building.
- Analyse logs and measurements before proposing a cause. Don't infer
  hardware faults or cabling from device names. When the maintainer states
  a fact about their own setup (e.g. which station a frequency carries),
  take it as given.
- Work in parallel where you can.
- Scripts that loop over many items (test, measuring, bulk and
  re-transcription scripts) print one line per item as they go, to stderr
  with `flush=True`, and print errors at once. A summary at the end is not
  enough.
- Keep secrets out of the repo, the docs and commit messages. If one is
  pasted into a chat, remind the person to revoke it.
- Both Dockerfiles copy files one by one. A new module or tool must be
  added to the right `COPY` line, or the image will lack it.

## Releasing

Releases are made only when the maintainer asks for one. "Make a PR and
merge it" means merge on green CI. It is not a release.

1. **Check the version number.** Several sessions often work in parallel.
   Fetch `master` and check `RELEASE_VERSION` and the tags. Take the next
   free number, and tell the other active sessions which number you take
   and which files you touch. Check `master` again right before merging. If
   you lose a race, renumber your PR (make it a draft first). After
   merging, announce "merged, next free number is vX".
2. **Open the release PR.** It contains:
   - the change itself;
   - `RELEASE_VERSION` bumped;
   - `docs/release-notes/vX.Y.Z.md`, in English. End it with how to update
     (`docker compose pull && docker compose up -d`) and which image
     changed. Add upgrade steps if `.env`, `receivers.yml` or the compose
     file change;
   - the status line in the relevant `docs/plans/` file updated.
3. **Merge** only when the `pytest` check is green.
4. **Confirm the publish.** `release.yml` tags `master` and creates the
   GitHub release, with the notes file above the generated notes.
   `docker-publish.yml` builds both images as `X.Y.Z`, `X.Y` and `latest`,
   for amd64 and arm64. Confirm that both workflows went green, then tell
   the maintainer. Cloud sessions cannot push tags, which is why the
   workflow cuts the release.

To test a change on the live installation, the maintainer prefers a real
release over building a branch image on the host. Fixing forward in the
next release is fine. On the host: `docker compose pull && docker compose
up -d`. `pull` does not replace `docker-compose.yml`, so when the compose
file changes, give the commands to fetch the new one.

**Dependabot:** a daily scheduled task merges safe pip PRs, those that only
change `requirements.txt` or `aggregator/requirements.txt` and have a green
CI. It never releases. `av` must stay below 19, because PyAV 19 fails to
decode the audio clips. The satellite's `numpy` must stay below 2.5.

## Testing

Pick the cheapest level that proves the change, and say which one you
used.

- **pytest:** `pip install -r requirements-dev.txt && python -m pytest -q`.
  Tests are in `tests/`, with the aggregator's in `tests/aggregator/`.
  CI runs this on Python 3.12. A bug fix comes with a test that fails on
  the old code. The tests never load Whisper or PyAV, so a change to `av`,
  `faster-whisper`, `ctranslate2` or `numpy` needs its own check: decode an
  OGG/Opus clip with `faster_whisper.audio.decode_audio`, and check that
  wheels exist for x86_64 and aarch64.
- **UI changes:** run the aggregator with test data or a mocked API, check
  the change in headless Chromium/Playwright, and look at the screenshots.
- **End to end:** run the published images, or ones built from the
  branch, with a fake `airspy_rx`/`airspy_info` that streams simulated FM
  stations. The stations come from an RDS encoder whose output redsea has
  been checked to decode. Scenarios worth covering:
  - RadioText with å/ä/ö;
  - single corrupted TA groups;
  - a PI switch (0xE224 ↔ regional PI) at the start, middle and end of an
    announcement;
  - two stations announcing at once;
  - PTY Alarm;
  - signal lost;
  - aggregator down;
  - satellite restart;
  - an overlong announcement;
  - Swedish transcription.

  This harness is not committed yet.
- **Geolocation:** `tools/geo_corpus.json` holds 61 real messages with
  answers. Measure changes with `tools/eval_geolocation.py` (`--stored`,
  `--save-claude`, `--replay`) and `tools/measure_geolocation.py`. Both are
  in the aggregator image under `/app/tools/`.
- **Live, on the maintainer's host:** cloud sessions cannot reach it, so
  ask the maintainer to run commands and paste the output. Useful ones:
  - `docker compose logs`;
  - `python3 -m dongles`;
  - `iq_side.py <freq> --serial …` (in the satellite image as
    `/app/iq_side.py`; it should report "normal I/Q");
  - `python3 -m gain_tuner --serial … --frequencies … [--save]`;
  - Admin → Noder → Kalibrera om;
  - read-only sqlite (`file:…?mode=ro`) for database questions.

  After a release, the maintainer often lets it run overnight or over a
  weekend and pastes the logs.
- **Re-transcription:** `aggregator/retranscribe.py` runs as a dry run by
  default. Then run it with `--yes --limit 5`, then on everything.
  `--engine local` is free; ElevenLabs costs 2–3 öre per message.
