# Architecture diagram

`rds-guard.architecture.json` describes how RDS Guard satellites, the uplink
and the aggregator fit together. It is rendered with
[Archify](https://github.com/tt-a1i/archify):

| File | Use |
|---|---|
| `rds-guard.architecture.svg` | Static image shown in the READMEs (follows the reader's light/dark theme) |
| `rds-guard.architecture.html` | Interactive version: download and open in a browser to click components, follow connections and open the cited source lines |
| `rds-guard.architecture.json` | Source of both; every component cites the code it describes |

Source references are pinned to commit `e2f0f09`. After an architecture change,
update the JSON (components, connections, `sources` and
`meta.repository.revision`), then regenerate from the repository root:

```bash
node <archify>/bin/archify.mjs finalize architecture docs/architecture/rds-guard.architecture.json docs/architecture/rds-guard.architecture.html --repo-root . --quality showcase
node <archify>/bin/export-svg.mjs docs/architecture/rds-guard.architecture.html docs/architecture/rds-guard.architecture.svg auto
```

`<archify>` is the folder of an installed Archify skill. `export-svg` refuses
to overwrite an existing SVG, so delete the old one first.
