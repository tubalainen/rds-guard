# Stationer: namn, område och identitet

Status: godkänd och byggd 2026-11-27 (gren claude-/upbeat-shannon-cu61mt).
API: `GET /api/v1/stations`, `PATCH /api/v1/station-settings` (admin),
`GET /api/v1/events?station=102800/E224`.

## Bakgrund

En nod (Hemma, Airspy R2 på taket i Karlstad) tar nu emot två P4-regioner:

| Frekvens | Station      | PI     | PS      |
|----------|--------------|--------|---------|
| 102,8    | P4 Örebro    | 0xE224 | "P4 SR" |
| 103,5    | P4 Värmland  | 0xE224 | "P4 SR" |

Tre saker fungerar inte med det:

1. **Stationer identifieras av PI.** Händelser slås ihop på `type + pi`
   inom ett tidsfönster (`aggregator/store.py`), stationslistan grupperas
   per PI (`list_stations`) och notisregler filtrerar på PI
   (`notify_rules.py`). Med samma PI på båda skulle ett meddelande från
   Värmland och ett från Örebro bli en händelse.
2. **Namnet saknas.** PS är bara "P4 SR"; UI, MQTT och push visar
   `station_ps`.
3. **Geolokaliseringen utgår från noden.** `place_of()` i
   `aggregator/pipeline.py` ger nodens plats (Karlstad) som mittpunkt för
   ortshjälp till transkriberingen, placering och öppna händelser. Ett
   Örebromeddelande borde utgå från Örebro län.

## Beslut (användaren, 2026-11-27)

- En station identifieras av **frekvens + PI**.
- Namnet hämtas **från RDS** (radiotext eller Long PS) när det finns där,
  och kan sättas/ändras per station i **Admin**.
- Kartan och aktuella meddelanden visar **alla stationer samtidigt med
  filter per station**, och stationens namn på varje meddelande.
- Geolokaliseringen utgår från **stationens område** (län), satt i Admin
  med ett förslag utifrån namnet. Nodens plats används bara när stationen
  saknar område.

## Genomförande

### 1. Stationsnyckel

- `station_key = "<frekvens i kHz>/<PI>"`, t.ex. `102800/E224`.
  Frekvensen normaliseras (satelliten skickar t.ex. `102.8M`).
- Ny kolumn `events.station_key`, fylls i vid ingest och i en migrering
  för befintliga händelser (de har redan `frequency` och `pi`).
- Sammanslagningen av samma sändning från flera noder/mottagare matchar på
  `type + station_key` i stället för `type + pi`.
- `list_stations()` grupperar per `station_key`.
- Notisregler: `stations` får innehålla stationsnycklar. Befintliga regler
  med bara PI fortsätter att matcha alla stationer med det PI:t.

### 2. Stationsregister i aggregatorn

Ny tabell `stations`:

| Kolumn        | Innehåll                                        |
|---------------|-------------------------------------------------|
| station_key   | `102800/E224`                                   |
| pi, frequency |                                                 |
| rds_name      | namn som hittats i RDS (Long PS / radiotext)    |
| name          | namn satt i Admin (vinner över rds_name)        |
| area          | "Örebro län" eller "Ort, Län", satt i Admin     |
| first/last_seen |                                               |

- Raden skapas automatiskt första gången stationen syns.
- `rds_name`: Long PS om den finns; annars "P4 <Region>" om radiotexten
  innehåller det. Mönstret avgörs av riktig data (se *Öppet* nedan).
- Visningsnamn = `name` → `rds_name` → PS → PI.
- `area`-förslag: namnet "P4 Örebro" → länet som gazetteern hittar för
  "Örebro". Förslaget visas i Admin och sparas först när ägaren godkänner.

### 3. Ett namnfält ut

Aggregatorn lägger `station_name` (och `station_key`) på varje händelse i
API, MQTT och push. `rdsui.js`, `map.js`, `notify_rules.py` och
`pipeline.py` visar `station_name` i stället för `station_ps`.

### 4. Geolokalisering

`place_of()` blir den enda ändringen i flödet:

1. stationens `area`, om satt;
2. annars nodens plats som i dag.

Ett län som område ger länets mittpunkt och en radie som täcker länet,
och texten till platsmotorn blir "Örebro län". Ortshjälp, placering och
`open_incidents` följer med automatiskt eftersom de redan tar sin
mittpunkt från `place_of()`.

### 5. UI

- Admin → ny vy **Stationer**: frekvens, PI, namn (från RDS / eget),
  område (förslag + ändra), vilka noder som hör den, senast hörd.
- Nodkortet visar stationsnamnet i stället för "P4 SR".
- Kartan och meddelandelistan: stationsfilter (alla som standard) och
  stationsnamn på varje meddelande. Kartans startvy täcker alla stationers
  områden.

### 6. Tester

- Två stationer med samma PI på olika frekvenser slås inte ihop.
- Samma station från två noder slås fortfarande ihop.
- Migrering av befintliga händelser och regler med bara PI.
- `place_of()` väljer stationens område före nodens plats.

## Kända begränsningar

- Samma region på två olika frekvenser (två sändare) blir två stationer.
  De kan få samma namn och område i Admin; att slå ihop dem är inte med.

## Öppet

- Vad radiotext och Long PS faktiskt innehåller för P4 Örebro och P4
  Värmland. Behövs för att RDS-namnet ska fungera; Admin-namnet fungerar
  oavsett.
