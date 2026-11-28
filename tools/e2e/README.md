# End-to-end tests without a radio

The real satellite and aggregator images, with a stand-in Airspy that sends
synthetic FM stations with RDS (pilot, PS, PI, PTY, TA, RadioText), paced
to the wall clock. A scenario script switches what the stations send and
checks the aggregator's API.

- `rds_signal.py`: RDS group coding (CRC, offset words, the RDS character
  set), biphase on 57 kHz, FM modulation at an offset in complex baseband.
- `fake_airspy_rx`, `fake_airspy_info`: stand-ins for the Airspy tools. What
  each station sends is read from a JSON control file every 50 ms.
- `libairspy_stub.c`: the satellite opens an Airspy only to turn its AGCs off
  (LNA/Mixer/VGA by hand); build with `gcc -shared -fPIC -o libairspy.so.0`.
- `scenarios.py`: full announcement, single corrupted TA groups, PI switch at
  the start/middle/end, both stations at once, VMA, silence, aggregator
  down, satellite restart, longer than MAX_RECORDING_SEC, and (with
  `--speech`) transcription.

The stand-ins are mounted over the image's own files, so the images under
test are unchanged:

```yaml
  rds-guard:
    image: ghcr.io/tubalainen/rds-guard:latest
    environment:
      E2E_CONTROL: /e2e-run/control.json
    volumes:
      - ./rds-data:/data                      # with a receivers.yml
      - <repo>/tools/e2e:/e2e:ro
      - ./run:/e2e-run
      - <repo>/tools/e2e/fake_airspy_rx:/usr/bin/airspy_rx:ro
      - <repo>/tools/e2e/fake_airspy_info:/usr/bin/airspy_info:ro
      - ./libairspy.so.0:/usr/lib/x86_64-linux-gnu/libairspy.so.1.0.11:ro
```

Use `TRANSCRIPTION_ENGINE=local` and `GEO_ENGINE=rules` so a run costs
nothing. Then:

```
python3 tools/e2e/scenarios.py --control run/control.json --api http://127.0.0.1:18080
```
