# Plan: satellit + aggregator som enda arkitektur

Status 2026-11-27: fas 0 klar (planen i repot), fas 1 byggd (v0.9.0: compose med båda, import, nodens plats i Admin, mätverktygen i aggregatorn). Fas 2–4 återstår.

## Bakgrund

Idag är varje RDS Guard en komplett installation: radio, inspelning, transkribering,
karta, Trafikverket, push, MQTT och webbgränssnitt. Med `UPLINK_URL` skickar den
dessutom en kopia till en aggregator, som gör samma bearbetning en gång till.
Mycket finns alltså på två ställen (`event_store`/`web_server`/`notifier` på
satelliten, `aggregator/store`/`api`/`notifier` på aggregatorn), och
`LOCAL_PROCESSING=false` är en halv väg mot en tunn satellit.

Målet är **en** arkitektur:

- **Satelliten** är helt headless. Den hanterar en eller flera RTL-SDR, FM-demodulering
  och RDS-avkodning, upptäcker TA- och larmhändelser, spelar in ljudet och skickar allt
  till aggregatorn så fort det händer. Den har ingen webbport, ingen MQTT, ingen
  transkribering och ingen karta.
- **Aggregatorn** är obligatorisk och gör all bearbetning: transkribering, karta,
  Trafikverket, push, MQTT, webbgränssnitt och arkiv. Den tar emot flera satelliter, och
  varje satellit kan ha flera mottagare och kanaler.
- Det ska vara enkelt att ansluta en satellit, i tre fall: samma maskin, en annan maskin
  på samma LAN, och över internet via HTTPS.

Det här har användaren bestämt (2026-11-27):

| Fråga | Beslut |
|---|---|
| Ljud | Händelsen skickas direkt, ljudet när sändningen är slut (ingen strömning) |
| Historik på docker01 | Flyttas över till aggregatorn med ett importverktyg |
| Driftsätt | Samma maskin, samma LAN och internet ska alla stödjas |
| Livevy | Hjärtslag var 30:e sekund räcker; Debug-sidan och WS-konsolen tas bort |
| Anslutning | Satelliten anmäler sig och blir en **väntande nod**, som godkänns i Admin. En gemensam `JOIN_KEY` godkänner direkt (samma compose) |
| Flera dongler | Konfigfil `receivers.yml`, en post per dongel, identifierad med **serienummer** (aldrig löpnummer) |
| Gain och PPM | Hittas automatiskt per dongel, och gemensamt för alla stationer på samma dongel. Kalibreringen körs vid första start, när kvaliteten sjunker och från en knapp i Admin. Den kan överstyras per dongel i `receivers.yml`. Kommer i fas 3 |
| Dubbletter | Samma sändning hörd av flera noder eller mottagare blir **en** händelse: en transkribering, en avisering |
| MQTT | Bara händelser från aggregatorn; satellitens ämnen per RDS-grupp tas bort |
| EON (grupp 14A) | Tas bort |
| Ljud på satelliten | Levererat ljud sparas i 7 dagar (`SATELLITE_KEEP_DAYS`) |
| Nodens plats | Sätts i aggregatorns Admin; satelliten kan föreslå en |
| Bakåtkompatibilitet | Nej. Protokoll v2 krävs och gamla satelliter får "update needed" |

Det gäller fortfarande att allt ska gå lokalt på CPU och att ElevenLabs och Claude bara
är tillval. Ändringen lägger inte till några betalda anrop. Sammanslagningen av
dubbletter *sparar* pengar när två noder hör samma sändning.

## Målbild

```
 Satellit (headless, valfritt antal)                Aggregator (en)
 receivers.yml ─┬─ dongel A: rtl_sdr → Channelizer → redsea ×N ┐
                └─ dongel B: rtl_sdr → Channelizer → redsea    ┤
                                                               ▼
           RulesEngine → satellite.db (händelser + outbox) → uplink v2 ──HTTP(S)──→ :8081 ingest
           AudioRecorder → /data/spool/*.ogg ─────────────────┘                     │
           health.json (Docker-hälsokontroll)               slå ihop till sändningar ▼
                                                   transkribering → karta → TRV → alert/push/MQTT
                                                   :8080 webbgränssnitt (arkiv, karta, Admin, Noder)
```

### Tre sätt att köra

| Fall | Satellitens adress till aggregatorn | Godkännande |
|---|---|---|
| Samma maskin (en compose) | `http://aggregator:8081` på compose-nätet; ingest-porten publiceras inte | `JOIN_KEY` i den gemensamma `.env`, godkänns direkt |
| Annan maskin på LAN | `http://<lan-ip>:8081` | Väntande nod, godkänns i Admin (eller `JOIN_KEY`) |
| Internet | `https://rds.example.com` genom proxy/tunnel, som idag | Väntande nod, godkänns i Admin |

Compose-filer:

- `docker-compose.yml` i roten: **båda** tjänsterna. Det blir standardinstallationen.
- `docker-compose.satellite.yml`: bara satelliten.
- `aggregator/docker-compose.yml`: bara aggregatorn.

## Protokoll v2 (`docs/uplink-protocol.md` skrivs om)

Det byggs vidare på v1: outbox, idempotenta PUT och protokollhuvud i alla svar. Huvudet
blir `X-RDS-Guard-Protocol: 2`, och aggregatorn svarar 426 på allt annat.

1. **Anslutning:** satelliten skapar själv sin token (32 slumpbyte) och ett `join_id` en
   gång, och sparar båda. Sedan anropar den `POST /api/v1/ingest/join` med
   `Authorization: Bearer <egen token>`. Body:
   - `join_id`, `node_name`, `version`, `suggested_location`, `receivers[]`
     (sammanfattning)
   - `join_key` (valfri)

   Aggregatorn sparar bara hashen av token, som en väntande nod. När noden godkänns blir
   samma hash nodens token. Inget hemligt skickas tillbaka, så ett svar som försvinner
   gör ingen skada: satelliten frågar bara igen. Svar:
   - `202 {"status":"pending","verify":"K7Q-3PD"}` medan noden väntar
   - `200 {"status":"approved","node":"…"}` när den är godkänd. Därefter används token
     som vanligt.
   - `403 rejected`

   Satelliten frågar igen var 15:e sekund. `verify` är en kort kod som räknas fram ur
   `join_id`. Den visas både i satellitens logg och i Admin, så att man godkänner rätt
   nod.
   - **Godkänna utan Admin:** är `ADMIN_PASSWORD` tom finns även
     `python3 -m aggregator.nodes pending|approve <verify>|reject <verify>`.
   - **Namnkrock:** förslaget blir "Namn (2)", och namnet kan ändras vid godkännandet.
   - **Ny anslutning efter förlorad `satellite.db`:** den kommer som en ny väntande nod.
     Vid godkännandet kan man välja "Ersätt nod X", och då behålls historiken (den nya
     token ersätter den gamla).
   - **Skydd:**
     - begränsat antal anrop per klient-IP; `X-Forwarded-For` används bara om
       `TRUSTED_PROXIES` är satt, annars motpartens adress
     - dessutom ett tak för alla anrop totalt
     - högst 20 väntande noder
     - väntande noder försvinner efter 24 timmar
     - en `join_key` som inte stämmer ger 403, utan att säga varför

   Har man redan en token i `UPLINK_TOKEN` (som idag) hoppas steget över.
2. **Händelse:** `PUT /events/{uid}` får ett nytt fält, `receiver` (mottagarens id från
   `receivers.yml`, eller `"0"` före fas 3). `frequency` finns redan. EON skickas inte.
3. **Hjärtslag:** `receivers[]` med `{id, serial, state, error, stations[]}`. Det ersätter
   dagens `pipeline` och `stations`. `location` byts mot `suggested_location`, och
   `local_processing` tas bort.
4. **Två köer i outboxen, med var sin arbetstråd:**
   - Händelser och delete går i den ena, ljud i den andra, äldst först inom varje kö.
     Då kan en ny sändnings start skickas medan en ljudfil laddas upp (upp till 120 s).
   - En ljudpost väntar tills dess händelse är kvitterad. Annars kan ljudkön efter ett
     avbrott hinna före händelsen, få 404 (som räknas som DROP) och tappa klippet.
     Test krävs.
   - Väntan vid omförsök och spärrtid (upp till 5 min) avbryts när en ny händelse köas,
     så att en start försöks direkt.
5. **Ingen dataförlust:**
   - Satelliten spelar in och köar även innan den är godkänd.
   - `UPLINK_MAX_AGE_HOURS` får standardvärdet 168 (7 dagar, samma som
     `SATELLITE_KEEP_DAYS`).
   - `MAX_RECORDING_SEC` får ett tak på 1200 s (ca 7 MB Opus), så att ett klipp aldrig
     går över ljudgränsen på 8 MiB. Standardvärdet 600 s är ca 3,6 MB.

## Sammanslagning av sändningar (aggregatorn)

- Ny tabell `receptions`, med en rad per nod och satellit-uid: `event_uid`, `node_id`,
  `receiver`, `rev`, `frequency`, `started_at`, `ended_at`, `state`, `audio_path`,
  `audio_received_at`. Det är det ingest lagrar i dag.
- `events` blir **sändningen**. Dess `event_uid` är den första mottagningens uid, så alla
  API:er, kartans `event_key`, notify-loggen och gränssnittet fortsätter att använda
  `event_uid` utan ändring.
- Regel för matchning (`aggregator/broadcasts.py`, ny): samma `type` och `pi`, och
  tidsintervallen överlappar eller starterna ligger inom `MATCH_WINDOW_SEC`
  (standard 30). Tiderna rättas först med nodens uppmätta klockavvikelse
  (`clock_offset_sec`), eftersom satelliternas klockor kan gå fel. Regeln täcker även
  samma PI på två frekvenser, alltså två sändare eller två dongler.
- Den **primära mottagningen** är den vars ljud transkriberas. `events.node_id` följer
  den. Platsen som ger ortnamnstips och kartan är den primära nodens plats, och annars
  plats från någon annan nod som hörde sändningen. Tas den primära mottagningen bort
  väljs en ny, och sändningen räknas om.
- Sändningens fält räknas om ur dess mottagningar:
  - start = tidigaste, slut = senaste
  - RadioText = unionen, i ordning
  - `active` om någon mottagning är aktiv, `end` när alla har slutat
- **Bästa ljudet:** när det första klippet har kommit väntar aggregatorn
  `BEST_AUDIO_WAIT_SEC` (standard 10 s), eller tills alla mottagningar som har slutat har
  skickat sitt ljud. Sedan väljs det längsta klippet, som ett enkelt mått på att
  mottagningen var hel. Det transkriberas en gång. Varje mottagnings ljud kan spelas upp
  separat i händelsevyn. Kostnad: texten kommer upp till 10 s senare.
- Larm (`alert_decision`), push och MQTT körs per sändning. De befintliga tidsgränserna
  gäller från sändningens slut.
- **Sådant som i dag gäller per nod:**
  - Nodfiltret i sökningen och push-regler som är begränsade till vissa noder matchar om
    *någon* av sändningens mottagningar kommer från noden.
  - I larmets payload är `node` den primära noden, och en ny lista `nodes[]` tar med
    alla som hörde den.
  - Tas en nod bort försvinner dess mottagningar. Sändningar som då saknar mottagningar
    tas bort, och de andra räknas om (ersätter dagens CASCADE från `events.node_id`).
- En DELETE (för kort, under 10 s) tar bort mottagningen. Är det den sista tas
  sändningen bort.
- Migrering av en befintlig aggregator-databas: varje händelse blir en sändning med en
  mottagning (idempotent, i `store.init_db`).
- Gränssnittet visar vilka noder och mottagare som hörde sändningen ("Hörd av"), i
  händelsevyn och i kartans popup (den finns redan för incidenter).

## Satelliten blir headless

**Kvar** (uppdateras):

| Modul | Ändring |
|---|---|
| `pipeline.py` | En övervakningstråd per mottagare. Inställningarna skickas in i stället för att läsas från globala variabler. `PipelineStatus` per mottagare |
| `channelizer.py` | Oförändrad |
| `audio_tee.py` | Oförändrad |
| `audio_recorder.py` | Gör bara OGG, ingen WAV och ingen transkribering |
| `uplink.py` | Protokoll v2: anslutning, token sparad i `satellite.db`, två köer |
| `rds_guard.py` | Bara `RulesEngine`, `StationInfo`, `Stats`, main och hälsofilen. Bort: WS, MQTT, notify, geo, TRV, backfill/rebuild, deferred alerts, EON |
| `status.py` | Bara hjärtslag och hälsa, ingen geo/TRV |
| `config.py` | Bara SDR, inspelning och uplink, plus `receivers.yml`. Avslutar med ett tydligt fel om `AGGREGATOR_URL` saknas |
| `event_store.py` | Bantas till `satellite.db`: events (kärnkolumner, `event_uid`, `audio_path`, `audio_delivered_at`), outbox och settings (`join_id`, token). Importerar inte längre `geolocate` eller `trafikverket` |

- **Ny databasfil** `/data/satellite.db` och ny ljudmapp `/data/spool/`. Gamla
  `events.db` och `/data/audio/` rörs inte, så att importen kan läsa dem och rensningen
  på 7 dagar aldrig raderar historiken.
- **`receivers.yml`** (monteras som `./receivers.yml`).
  - En dongel identifieras alltid med sitt **serienummer** (`SN` i `rtl_test`), aldrig
    med löpnummer, eftersom löpnumret ändras med USB-ordningen. Serienumret slås upp
    till ett index varje gång pipelinen startar (`_resolve_device_serial`, utbyggd).
  - `gain` och `ppm` är `auto` om de inte anges.
  - Saknas filen och exakt en dongel är ansluten, bildas en mottagare av
    `FM_FREQUENCY`/`FM_FREQUENCIES` med den dongelns serienummer. Med flera dongler och
    ingen fil avslutar satelliten med ett fel som listar serienumren.
  - Kontroller:
    - högst 4 frekvenser per dongel, inom 2 MHz
    - ett serienummer som saknas ger fel, med en lista över anslutna dongler
    - ansluten dongel som inte finns i filen ger en rad i loggen
    - samma frekvens på två dongler ger en varning
    - **dubbla serienummer** (många dongler levereras med `00000001`) ger ett fel som
      förklarar hur man ger dem unika nummer med `rtl_eeprom -d <index> -s <nytt>`
  - Verktyget `python3 -m dongles` listar anslutna dongler: serienummer, namn, tuner och
    tillåtna gain-steg.
  - Ny beroende: `pyyaml`.

  ```yaml
  receivers:
    - serial: "00000101"          # krävs
      name: tak                   # valfritt, visas i Admin
      frequencies: [103.5M, 102.9M]
      # gain: auto  (standard)   eller t.ex. gain: 28.0 för att överstyra
      # ppm: auto   (standard)   eller t.ex. ppm: 12
    - serial: "00000102"
      frequencies: [99.1M]
      gain: 19.7                  # överstyrt: ingen gain-kalibrering för denna
  ```
- **En pipeline för alla:** även en ensam station tas emot med `rtl_sdr` och
  `Channelizer`, med mittfrekvensen förskjuten ca 250 kHz för att undvika DC-spiken.
  `rtl_fm` försvinner. Då mäter kalibreringen exakt samma signalkedja som används i
  drift. CPU-lasten på en Raspberry Pi 4 mäts innan beslutet är slutgiltigt.
- **Tillstånd per mottagare och frekvens:** `StationState` (TA, PTY), `StationInfo`,
  `_pi_to_freq`/`_freq_to_pi` och statistiken är i dag globala och nycklade på PI. Med
  samma PI på två mottagare skulle de krocka. De får nyckeln `(receiver, frequency)`.
- **Hälsa utan port:** satelliten skriver `/data/health.json` var 10:e sekund (tid,
  status per mottagare, uplink-läge, kölängd). `HEALTHCHECK` kör
  `python3 -m satellite_health`, som är friskt om filen är under 60 s gammal och minst en
  mottagare kör. Loggen visar anslutning, verifieringskod, "väntar på godkännande" och
  "godkänd".
- **Bort ur satellitens image:**
  - modellsteget (KB-Whisper)
  - `faster-whisper`, `av`, `anthropic`, `paho-mqtt`, `tzdata`
  - `static/`, `geodata/`, `tools/`, `EXPOSE 8022`

  Kvar: redsea, rtl-sdr, ffmpeg, numpy, requests och pyyaml. Imagen blir mycket mindre
  och passar en Raspberry Pi.
- **Buggar som rättas på vägen** (hittade under kartläggningen; verifieras med test
  först):
  1. TA och PTY saknar EON:s `was_known`-skydd. Den första gruppen efter start eller för
     ett nytt PI kan därför ge ett falskt "end", eller en start mitt i ett meddelande.
  2. I flerkanalsläget uppdateras station och statistik två gånger per grupp
     (`_make_station_callback` och `process_group`).
  3. `AUDIO_FORMAT` används inte och tas bort.

## Automatisk gain och PPM per dongel (fas 3)

Problemet: `-1` (tunerns egen AGC) och fasta värden blir ofta fel. FM-stationer är så
starka att förstärkaren överstyrs. Då blir det intermodulation och sämre RDS på alla
kanaler, även om ADC:n inte klipper synligt. Kalibreringen mäter därför det som till slut
räknas, avkodad RDS, och inte bara signalnivån.

Ny modul `gain_tuner.py`. Den kör per dongel och tar bara den dongeln ur drift; de andra
fortsätter. Dongelns kanaler tas med samma `_StationDSP` som i drift.

1. **Steg som finns:** `rtl_test -d <index>` ger dongelns tillåtna gain-steg (R820T: 29
   steg, 0–49,6 dB).
2. **PPM** (om `ppm: auto`): spela in några sekunder med medelhög gain. För varje station
   ger FM-diskriminatorns medelvärde (DC-nivån) hur långt bärvågen ligger från sin
   nominella frekvens. Felet i ppm är förskjutningen i Hz delat med stationens frekvens i
   MHz. Ta medianen över stationerna, och avrunda till heltal. Exempel: 50 ppm vid
   100 MHz är 5 kHz, vilket syns tydligt. Tunern och ADC:n delar samma kristall, så ett
   enda värde gäller. Rimlighetsgräns ±150 ppm, annars används 0 med en varning.
3. **Snabb svepning över alla gain-steg** (ca 1 s inspelning per steg, med den nya ppm),
   som ger per steg:
   - andel klippta ADC-värden (0 eller 255) och IQ-nivå i dBFS
   - per station: SNR för 19 kHz-pilotton och 57 kHz RDS-bärvåg i den demodulerade
     MPX-signalen

   Steg som klipper mer än 0,05 % stryks. Steg 4 prövar sedan de 4 stegen med bäst
   *sämsta* RDS-SNR.
4. **Kontroll med riktig avkodning:** för varje kandidat körs redsea i 8 s per station,
   vilket ger grupper/s och blockfel (`-E`, BLER).
5. **Val (harmoni mellan stationerna):**
   - Poäng per steg = den **sämsta** stationens RDS-kvalitet (maximin), så att ingen
     station offras för en annan.
   - Vid lika poäng (inom 5 %) vinner **lägre** gain, för marginal mot överstyrning när
     förhållandena ändras.
   - Ger ingen kandidat RDS (ingen antenn, ingen signal) används 20 dB, med en tydlig
     varning i loggen och i hjärtslaget.
6. **Tid:** ca 1,5 minut per dongel. Loggen visar en rad per steg (gain, klippning, SNR
   per station, grupper/s) medan det pågår, och en tabell med valet på slutet.

**När den körs:**
- **Vid första start**, och när en dongel med nytt serienummer eller nya frekvenser
  dyker upp. Resultatet sparas i tabellen `receiver_calibration` i `satellite.db`
  (serienummer, frekvenser, gain, ppm, poäng per station, tid, orsak) och återanvänds
  vid omstart.
- **När kvaliteten sjunker:** satelliten följer grupper/s och BLER per station. Ligger
  någon station under 60 % av det kalibrerade värdet i 15 minuter, kalibreras dongeln om,
  högst en gång per 6 timmar.
- **Från Admin:** knappen "Kalibrera om" per dongel under Noder. Kommandot skickas i
  svaret på hjärtslaget (`commands[]`, med id, så att det bara körs en gång). Resultatet
  syns i nästa hjärtslag. Det är ett valfritt fält, så protokollet förblir v2.
- **Aldrig mitt i en sändning:** pågår TA eller larm på någon av dongelns stationer
  skjuts kalibreringen upp tills sändningen är slut.

**Överstyrning:** `gain: <dB>` i `receivers.yml` stänger av gain-kalibreringen för den
dongeln, och värdet avrundas till närmaste tillåtna steg. `ppm: <tal>` gör samma sak för
PPM. Utan något angivet är allt automatiskt; användaren behöver inte ställa in något.

**Synligt:**
- Hjärtslaget har per mottagare: `gain`, `gain_mode` (auto/manual), `ppm`, `ppm_mode`,
  `calibrated_at`, kvalitet per station och senaste orsak.
- Aggregatorns nodkort visar detta, och knappen "Kalibrera om".
- `python3 -m gain_tuner --serial <SN>` kör en kalibrering för hand och skriver ut
  tabellen. Den sparar inget utan `--save`, och vägrar köra om satelliten använder
  dongeln (då används Admin-knappen).

`RTL_GAIN` och `PPM_CORRECTION` i `.env` försvinner. Ett äldre `RTL_GAIN` läses som
överstyrning för den enda dongeln, med en varning om att flytta det till
`receivers.yml`.

## Aggregatorn tar över det satelliten hade

- **Noder i Admin:**
  - Ny lista "Väntande" med namn, version, IP, verifieringskod och föreslagen plats.
    Godkänn med namn och plats, eller Avvisa.
  - Nodkortet visar per mottagare och station: PS, frekvens, PI, TP/TA, grupper/s,
    pipeline-läge och fel, drifttid, plats och klockavvikelse.
  - Nodens plats blir kolumnen `nodes.location`, som kan ändras i Admin.
    `pipeline.node_place` använder den, och annars den föreslagna platsen.
- **Ingest:** `JOIN_KEY` (miljövariabel) och `join_requests` i `store.py`, med samma
  migreringsmönster som i dag (`CREATE IF NOT EXISTS` och `ADD COLUMN`).
- **Importverktyg** `python3 -m aggregator.import_satellite /import/events.db /import/audio --node "docker01"`:
  - Läser den gamla satellitens databas: händelser, ljud, transkriptioner (inklusive
    korrigerade), `incidents`, `incident_events` (där `event_key` översätts från id till
    `event_uid`; uid skapas där det saknas), `area_warnings`, push-regler och TRV-tabeller.
  - Hoppar över uid som aggregatorn redan har, alltså sådant som redan kom via uplink.
    Incidenter, områdesvarningar och TRV-kopplingar tas bara med för händelser som
    faktiskt importeras.
  - Push-regler: har aggregatorn bara standardreglerna ersätts de av satellitens.
    Annars läggs bara de regler till som inte redan finns.
  - Transkriberar inget på nytt.
  - Skriver alltid aggregatorns aktuella schema (mottagningar och sändningar efter
    fas 2). Därför fungerar det också för den som hoppar direkt från v0.8.x till v1.0.0.
  - Skriver en rad per händelse medan den kör och en summering på slutet.
  - Den gemensamma compose-filen monterar `./rds-data` skrivskyddat på `/import`.
- **MQTT:** aggregatorns ämnen räcker (`alert`, `{pi}/traffic/ta`, transkription,
  `notify/*`, `trafikverket/update`, `nodes/*/status`, `aggregator/status`). Ämnet
  `{pi}/traffic/ta` publiceras redan när sändningens start kommer in. README får en
  guide för att flytta Home Assistant-konfigurationen från `rds/…` till aggregatorns
  prefix. `MQTT_TOPIC_PREFIX=rds` kan väljas för att slippa ändra ämnen som finns i båda.
- **Utvärderingsverktygen** (`tools/eval_geolocation.py`, `measure_geolocation.py`,
  `geo_corpus.json`) flyttas till aggregatorns image. `--stored` ska fungera mot
  `/api/v1/events`, och `measure_geolocation` ska läsa aggregatorns `config` i stället
  för satellitens.

## Faser och releaser

Varje fas släpps för sig, och docker01 fungerar hela tiden. Användaren testar en release
på docker01 genom `docker compose pull` (som tidigare).

**Fas 0: planen i repot.** Den här planen läggs som `docs/plans/satellit-aggregator.md`,
med en statusrad överst som uppdateras efter varje fas (som `placering-v2.md`).

**Fas 1, v0.9.0: aggregatorn redo att ta över** (protokoll v1 är kvar, inget går sönder)
- Nodens plats i Admin, och det utökade nodkortet.
- Importverktyget.
- `docker-compose.yml` i roten med båda tjänsterna, och `docker-compose.satellite.yml`.
- Utvärderingsverktygen i aggregatorns image.
- README: hur man byter till en compose med båda.
- På docker01 (där bara satelliten körs i dag): aggregatorn läggs till i samma compose,
  historiken importeras, satelliten får `LOCAL_PROCESSING=false` och aggregatorns
  gränssnitt används. Det testar målbilden innan något tas bort.

**Fas 2, v0.10.0: protokoll v2 och sammanslagning** (båda sidor samtidigt; vänners
satelliter måste uppdateras)
- Anslutningsflödet (`join`, väntande noder, `JOIN_KEY`, verifieringskod, CLI).
- `receiver` i händelser (`"0"` tills vidare); två köer i outboxen med väckning.
- `receptions` och sammanslagning av sändningar, "Hörd av" per händelse.
- Ett test av hela livscykeln i `RulesEngine` ner till outbox-rader (TA på,
  RadioText, TA av, under 10 s, larm). Det är skyddsnätet inför fas 3.
- Release notes: en uppgraderingsguide för vänners satelliter.

**Fas 3, v1.0.0: satelliten headless, flera mottagare**
- Allt som står under "Satelliten blir headless": ny `satellite.db` och spool,
  `receivers.yml` med serienummer, en pipeline per mottagare (alltid `rtl_sdr` och
  `Channelizer`) och tillstånd per mottagare, automatisk gain och PPM med Admin-knapp,
  hjärtslag med `receivers[]` (aggregatorns nodkort visar dem), hälsokontroll, bantad
  image, fel vid start utan aggregator, buggfixar. `receivers.yml` kommer först här, så
  att den inte måste dras genom satellitens gränssnitt och MQTT som tas bort i samma
  release.
  `LOCAL_PROCESSING`, `web_server.py`, `notifier.py` (satellitens),
  `static/index.html`, `static/js/*` och `static/css/debug.css` tas bort.
- Release notes med en tydlig uppgraderingsguide: `rds-guard:latest` är nu bara en
  satellit, man behöver aggregatorn, och så här flyttar man.

**Fas 4, v1.0.1: städning av repot** (ingen ändring i beteende)
- Satellitens kod flyttas till `satellite/`, och modulerna som bara aggregatorn använder
  flyttas in i `aggregator/`: `transcriber`, `geolocate`, `gazetteer`, `geo_service`,
  `trafikverket`, `notify_rules`, `admin_auth`, `geodata/`, `static/shared/`. Det
  framgår då tydligt vilken del som använder vad.
- `pytest.ini`, Dockerfiles och CI-matrisen följer med.
- Arkitekturdiagrammet (`docs/architecture/*.json`, regenereras med Archify) uppdateras.

## Filer som berörs (urval)

- Satelliten: `rds_guard.py`, `pipeline.py`, `audio_recorder.py`, `uplink.py`,
  `event_store.py`, `status.py`, `config.py`, `Dockerfile`, `requirements.txt`,
  `entrypoint.sh`, `.env.example`, `channelizer.py` (används för kalibreringen), nya
  `receivers.example.yml`, `satellite_health.py`, `dongles.py` och `gain_tuner.py`.
- Aggregatorn: `aggregator/ingest.py` (join, v2), `store.py` (receptions,
  join_requests, `nodes.location`), `pipeline.py` (per sändning), `api.py` (väntande
  noder, plats, mottagningar), `static/js/aggregator.js` och `static/shared/rdsui.js`
  ("Hörd av", ljud per mottagning), `config.py` (`JOIN_KEY`, `MATCH_WINDOW_SEC`), nya
  `broadcasts.py` och `import_satellite.py`, `Dockerfile` (tools), `.env.example`.
- Gemensamt: `docker-compose*.yml`, `README.md`, `aggregator/README.md`,
  `docs/uplink-protocol.md`, `docs/release-notes/*`, `RELEASE_VERSION`,
  `.github/workflows/docker-publish.yml` och `security-scan.yml` (sökvägar i fas 4).

Det som återanvänds: outbox och kollaps (`event_store._queue`, `outbox_*`),
`Uplink._request` och svarsklassningen, `ingest.auth_middleware`, `RateLimiter`,
`validate_event`, `store.authenticate`, `nodes.setup_snippet`, `alert_decision`,
`geo_service.Backfill`, `admin_auth.AdminAuth` och det delade gränssnittet `RDSUI`.

## Tester

- **Flyttas, tas inte bort:** satellitens tester för funktioner som flyttar
  (`test_web_notify`, `test_geo_backfill`, `test_incident_timeline`, delarna av
  `test_admin_auth` och `test_trafikverket*` som går via `web_server`/`rds_guard`)
  skrivs om mot aggregatorn där motsvarigheten inte redan är testad.
  Modultesterna (`test_geolocate`, `test_notify_rules`, `test_transcriber`,
  `test_trafikverket`) följer med modulerna.
- **Nya tester:**
  - join: väntande, godkänd, avvisad, fel nyckel, `JOIN_KEY`, utgång, gräns för antal
    anrop, token lämnas ut en gång
  - tolkning och kontroll av `receivers.yml`, och reservvägen via `FM_FREQUENCY`
  - join: ett godkännande-svar som försvinner (satelliten frågar igen och fortsätter),
    namnkrock, "Ersätt nod", CLI-godkännande
  - outboxens köer: en händelse passerar ett ljud som håller på; ljud väntar på sin
    händelse efter ett avbrott; en ny händelse avbryter väntan vid omförsök
  - matchning av sändningar: samma PI på två noder och på två mottagare, klockavvikelse,
    intervall som inte överlappar, sista DELETE, borttagen primär mottagning, längsta
    klippet väljs, en avisering per sändning, nodfilter och nodbegränsade regler,
    borttagen nod
  - `MAX_RECORDING_SEC` över taket
  - dongler: tolkning av `rtl_test` (serienummer, index, gain-steg), dubbla serienummer,
    serienummer som saknas, reservvägen med en enda dongel
  - gain_tuner, med syntetisk IQ: klippningsmåttet, PPM ur en FM-bärvåg med känd
    förskjutning, pilottonens SNR; valet (maximin, lägre gain vid lika poäng, ingen
    signal ger 20 dB); avrundning av överstyrning till närmaste steg
  - när kalibreringen körs: ny dongel eller nya frekvenser, sparat resultat återanvänds,
    sjunkande kvalitet (15 min, högst var 6:e timme), uppskjuten under TA,
    Admin-kommandot körs en gång
  - import: id översätts till uid, uid som redan finns hoppas över, kör man två gånger
    blir det inga dubbletter
  - satelliten utan `AGGREGATOR_URL` avslutar med ett fel; hälsofilen
  - `was_known`-skyddet för TA och PTY
- `pytest -q` (CI-flödet `tests.yml`) ska vara grönt efter varje fas.

## Verifiering

1. `pytest -q` lokalt och i CI.
2. Bygg båda imagen (`docker build .` och `docker build -f aggregator/Dockerfile .`).
   Kontrollera att satellitens image saknar faster-whisper, modellen och port 8022.
3. Nytt verktyg, `tools/fake_satellite.py`: talar protokoll v2 mot en körande aggregator
   utan radio. Det ansluter, väntar på godkännande, skickar hjärtslag, en händelse
   (start, update, end) och en OGG-fil, och kan simulera två noder som hör samma
   sändning. Det skriver en rad per steg medan det kör. Med det testas hela kedjan i
   den gemensamma compose-filen (`docker compose up`) i en molnsession:
   - noden dyker upp som väntande och godkänns
   - en händelse syns i arkivet, med en transkribering lokalt (KB-Whisper)
   - en avisering skickas, en gång
4. På docker01 efter varje release:
   - Fas 1: importen är klar och historiken syns i aggregatorn.
   - Fas 2: satelliten ansluter med `JOIN_KEY` och går till "godkänd".
   - Fas 3: `docker compose ps` visar satelliten som healthy, utan port.
   - Fas 3, gain: före bytet körs
     `python3 -m gain_tuner --serial <SN>`. Den väljer gain och ppm, och sedan jämförs
     grupper/s och BLER med dagens `RTL_GAIN=8` på P4 Värmland. Den automatiska ska vara
     minst lika bra. Nodkortet visar gain, ppm och tid för kalibreringen.
   - Ett riktigt TA-meddelande på P4 Värmland syns som en händelse i aggregatorn inom
     några sekunder från start, och med text efter slut.

## Kvar att bevaka

- `rds.tuben.org` pekar i dag på satellitens gränssnitt. Det ska peka på aggregatorns
  webbport, som inte har inloggning för arkivet, precis som satellitens i dag. Ingest
  (8081) får en egen adress eller sökväg om vänner ska ansluta över internet.
- Aggregatorns webbport är 8080. I den gemensamma compose-filen mappas den till 8022 som
  standard (`WEB_UI_PORT`), så att bokmärken och proxyn på docker01 fungerar som förut.
