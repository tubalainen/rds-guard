"""Measure the map engines on this installation's own traffic messages.

Runs inside the RDS Guard container and reads the latest transcribed
messages from the local web API. For each message it shows where the rules
engine places it and, when ANTHROPIC_API_KEY is set, where Claude does and
how Claude corrected the text, with the token cost. With --keyterms it also
transcribes the audio again with ElevenLabs, with and without the local
place names, to show whether the names help.

    docker compose exec rds-guard python3 /app/tools/measure_geolocation.py
    docker compose exec rds-guard python3 /app/tools/measure_geolocation.py --keyterms --limit 20

Costs: Claude Haiku about 1-3 öre per message; --keyterms transcribes each
clip twice with ElevenLabs (about 2-3 öre per clip and pass).
The report is also written to /data/geo-measurement.json.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import config  # noqa: E402
import geo_service  # noqa: E402
import geolocate  # noqa: E402

# Claude Haiku 5.5 list prices, USD per million tokens (check current prices)
PRICE_IN, PRICE_OUT = 0.10, 0.50


def get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def describe(placements):
    if not placements:
        return "  (ingen plats)"
    return "\n".join(
        f"  {p.label}  ±{p.radius_m} m"
        + (f"  riktning {p.bearing}°" if p.bearing is not None else "")
        + ("  båda riktningar" if p.both_directions else "")
        + ("  AVKLARAD" if p.cleared else "")
        for p in placements)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--location", default=config.NODE_LOCATION,
                    help="'Ort, Län' (default: NODE_LOCATION)")
    ap.add_argument("--api", default=f"http://127.0.0.1:{config.WEB_UI_PORT}")
    ap.add_argument("--keyterms", action="store_true",
                    help="also re-transcribe with ElevenLabs, with and without place names")
    args = ap.parse_args()

    svc = geo_service.GeoService(geolocate.create_geolocator("rules"))
    node = svc.locate_node(args.location)
    if node is None:
        sys.exit(f"Platsen '{args.location}' hittades inte. Ange --location 'Ort, Län'.")
    area = svc.area(node.point)
    rules = geolocate.Geolocator(None)
    claude = None
    if config.ANTHROPIC_API_KEY:
        claude = geolocate.Geolocator(geolocate.ClaudeReader(
            config.ANTHROPIC_API_KEY, model=config.CLAUDE_MODEL,
            timeout=config.CLAUDE_TIMEOUT))
    else:
        print("ANTHROPIC_API_KEY saknas: bara regelmotorn körs.\n")

    events = get(f"{args.api}/api/events?type=traffic&limit={args.limit}")["events"]
    report, tokens_in, tokens_out = [], 0, 0
    for ev in events:
        text = ev.get("transcription")
        if not text:
            continue
        print("=" * 78)
        print(f"#{ev['id']}  {ev.get('started_at')}  {ev.get('station_ps')}")
        print(text)
        row = {"id": ev["id"], "text": text}
        r = rules.locate(text, area, node.point, args.location)
        print("Regler:")
        print(describe(r.placements))
        row["rules"] = [vars(p) for p in r.placements]
        if claude:
            t0 = time.monotonic()
            c = claude.locate(text, area, node.point, args.location,
                              ev.get("radiotext"))
            usage = claude._reader.last_usage
            if usage:
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
            print(f"Claude ({time.monotonic() - t0:.1f} s"
                  + (f", {usage.input_tokens} in / {usage.output_tokens} ut" if usage else "")
                  + ("" if c.engine == "claude" else f", FEL: {c.error}") + "):")
            print(describe(c.placements))
            if c.corrected_text and c.corrected_text != text:
                print("Rättad text:")
                print(f"  {c.corrected_text}")
            row["claude"] = [vars(p) for p in c.placements]
            row["corrected"] = c.corrected_text
            row["claude_error"] = c.error
        if args.keyterms and ev.get("audio_url"):
            row["keyterms"] = compare_keyterms(args.api, ev, svc, node)
        report.append(row)

    print("=" * 78)
    n = len(report)
    print(f"{n} meddelanden.")
    print(f"Regler placerade {sum(1 for r in report if r['rules'])} av {n}.")
    if claude:
        cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
        print(f"Claude placerade {sum(1 for r in report if r.get('claude'))} av {n}; "
              f"{tokens_in} + {tokens_out} tokens, cirka {cost:.4f} USD "
              f"({cost / max(n, 1) * 100:.3f} cent per meddelande).")
    out = "/data/geo-measurement.json"
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        print(f"Rapporten finns i {out}")
    except OSError:
        pass


def compare_keyterms(api, ev, svc, node):
    """Transcribe a clip with ElevenLabs with and without place names."""
    from transcriber import create_transcriber
    if not config.ELEVENLABS_API_KEY:
        print("  (--keyterms kräver ELEVENLABS_API_KEY)")
        return None
    t = create_transcriber("elevenlabs", config.TRANSCRIPTION_LANGUAGE,
                           elevenlabs_api_key=config.ELEVENLABS_API_KEY,
                           elevenlabs_model=config.ELEVENLABS_MODEL,
                           elevenlabs_keyterms=config.ELEVENLABS_KEYTERMS,
                           fallback="none")
    with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
        f.write(urllib.request.urlopen(api + ev["audio_url"], timeout=60).read())
        f.flush()
        plain = t.transcribe(f.name)
        hinted = t.transcribe(f.name, svc.hints(node.point))
    print("ElevenLabs utan ortnamn:")
    print(f"  {plain}")
    print("ElevenLabs med ortnamn:")
    print(f"  {hinted}")
    return {"plain": plain, "with_place_names": hinted}


if __name__ == "__main__":
    main()
