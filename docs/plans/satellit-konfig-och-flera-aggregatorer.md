# Satellit: radion bara i receivers.yml, och flera aggregatorer

Status: förslag 2026-11-28 (issue #106), uppdaterat efter underhållarens svar samma dag. Väntar på godkännande. Inget är byggt.

## Bakgrund

Issue #106 tar upp tre saker:

1. Ska radion ställas in i `.env` eller i `receivers.yml`? I dag går båda.
   `FM_FREQUENCY`/`FM_FREQUENCIES`/`RTL_DEVICE_SERIAL` i `.env` gäller när
   `rds-data/receivers.yml` saknas. Finns filen vinner den, utan att något
   säger det. `FM_FREQUENCY` har dessutom ett tyst standardvärde (103.5M),
   så en satellit som ingen ställt in lyssnar ändå på något.
2. Den som kör en fristående satellit laddar ner den kombinerade
   `.env.example`, som till största delen handlar om aggregatorn.
3. En satellit kan bara rapportera till en aggregator: det finns en
   `AGGREGATOR_URL`, en token/join-identitet och en kö. Kompisen i Umeå vill
   köra en egen aggregator och samtidigt skicka till din.

Underhållarens beslut (2026-11-28): radion ställs bara in i
`receivers.yml`, och alla SDR-inställningar tas bort ur `.env`. Det ska
vara lika lätt som `.env` var. Satelliten ska själv kunna skicka till
flera aggregatorer (`ALSO_REPORT_TO`). En extra aggregator får allt, och
noden ser ut som vilken nod som helst där.

## Del A: radion bara i receivers.yml

### Beslut (2026-11-28)

- `receivers.yml` är det enda stället för radion. `FM_FREQUENCY`,
  `FM_FREQUENCIES` och `RTL_DEVICE_SERIAL` försvinner ur `.env.example`,
  README och `config.py`. Standardvärdet 103.5M försvinner också.
- **`serial:` krävs alltid**, även med en enda mottagare. Då syns det
  tydligt när en dongel byts.
- **Filen ska vara lika lätt som `.env` var:** fyll i serienumret och
  frekvensen, inget mer. `receivers.example.yml` görs om till en kort fil
  med kommenterade exempel för RTL-SDR och Airspy. Man tar bort `#` och
  ändrar värdena. Det som är valfritt (gain, ppm, sample_rate, lna/mixer/vga)
  flyttas längre ner, under en egen rubrik:

  ```yaml
  # rds-data/receivers.yml: which receiver listens to what.
  # Remove the # on the lines for your receiver and change the values.
  # Your receivers' serial numbers are in the log, or:
  #   docker compose exec rds-guard python3 -m dongles
  receivers:

    # --- An RTL-SDR dongle ---
    # - serial: "00000001"
    #   frequencies: [103.5M]

    # --- An Airspy Mini or R2 ---
    # - serial: "0x644064DC2B1C2E4F"
    #   frequencies: [103.5M, 102.9M]

  # More settings (all optional): see further down.
  ```

- **Ny installation utan receivers.yml:** satelliten skriver
  `rds-data/receivers.yml` enligt mallen ovan. De anslutna mottagarnas
  riktiga serienummer står redan i de kommenterade exemplen, så man bara
  tar bort `#` och skriver frekvensen. Sedan stoppar satelliten med ett
  tydligt fel: "Fyll i rds-data/receivers.yml och starta om". Den gissar
  ingen frekvens.
- **Befintlig installation (migrering):** saknas `receivers.yml` men
  `FM_FREQUENCY`/`FM_FREQUENCIES` är satta i `.env`, skriver satelliten
  en gång en `receivers.yml` från dem. Serienumret tas från
  `RTL_DEVICE_SERIAL`, eller från den enda anslutna mottagaren. Mallens
  kommenterade exempel följer med. Satelliten loggar att `.env`-raderna
  inte används längre och kan tas bort, och sedan kör den som vanligt.
  Ingen behöver göra något vid uppdateringen. Är flera mottagare anslutna
  och `RTL_DEVICE_SERIAL` saknas skrivs bara mallen, och satelliten
  stoppar som i dag.
- **Filen ska gå att ändra utan sudo.** Containern kör som root, så den
  fil satelliten skriver får samma ägare som mappen `rds-data/`.
- Finns både `receivers.yml` och `FM_*` i `.env`: en varning i loggen om
  att `.env`-raderna ignoreras.
- En fil där alla mottagare är bortkommenterade ger samma tydliga fel som
  en fil som saknas, inte ett YAML-fel.
- **Ny `.env.satellite.example`** med bara satellitens inställningar
  (aggregator, nodnamn, plats, inspelning). `docker-compose.satellite.yml`
  och README:s satellitavsnitt hänvisar till den. `.env.example` för
  kombinerad drift behåller sin satellitdel, men utan radion.
- README: avsnittet "Dongles: receivers.yml" blir det enda radioavsnittet,
  med ett kom igång-steg: starta, ta bort `#` i `rds-data/receivers.yml`,
  skriv frekvensen, starta om.

### Tester (A)

- Migrering: `.env` med en mottagare ger en `receivers.yml` som laddas
  och ger samma mottagare som i dag.
- Ny installation: mallen skrivs med de anslutna serienumren, och
  satelliten stoppar med rätt meddelande.
- En bortkommenterad fil ger samma meddelande.
- `serial:` saknas: fel.
- Mallen själv (`receivers.example.yml`) går att ladda när exemplen
  avkommenteras, både för RTL-SDR och för Airspy.

## Del B: flera aggregatorer

### Beslut (2026-11-28)

- `AGGREGATOR_URL` förblir **den egna aggregatorn** (den primära).
  Kombinerad compose sätter den till `http://aggregator:8081` som i dag.
- Ny inställning **`ALSO_REPORT_TO`**: en kommaseparerad lista med fler
  aggregatorer, t.ex. `ALSO_REPORT_TO=https://rds.example.com`. Den ligger
  separat eftersom kombinerad compose låser `AGGREGATOR_URL`.
- **En identitet per aggregator.** Token och join-id sparas per URL i
  satellitens databas. Befintlig identitet hör till den primära, så inget
  behöver göras om.
- **`JOIN_KEY` skickas bara till den primära.** Att skicka den till en
  annans aggregator vore att lämna ut nyckeln. De extra aggregatorerna går
  via vanlig join, och deras ägare godkänner i Admin → Noder (koden står i
  loggen, med aggregatorns adress).
- **En kö per aggregator.** Outbox-tabellen får kolumnen `target`. Varje
  ändring köas en gång per aggregator, och varje aggregator har egna
  arbetstrådar och egen backoff. En aggregator som ligger nere stoppar inte
  den andra.
- **Rensning:** en händelse och dess ljud tas bort först när alla
  aggregatorer har fått dem (eller gett upp efter `UPLINK_MAX_AGE_HOURS`).
  Det följer av att `purge` redan väntar på tom outbox för händelsen.
- **Radering** (`DELETE /events/...`) går till alla.
- **Heartbeat** skickas till alla, med samma innehåll.
- **Kommandon** (t.ex. "Kalibrera om") tas bara emot från den primära. En
  kompis aggregator ska inte kunna ändra din mottagare. Kommandon från
  andra loggas och ignoreras.
- **Loggen och hälsofilen** visar varje aggregators läge (ansluten, väntar
  på godkännande, kölängd).
- Kostnad: ingen. Bandbredden blir ungefär dubbel (0,7 MB per 2 min ljud
  per aggregator).

### Tester (B)

- Outbox: en ändring köas en gång per aggregator, och en som är nere
  blockerar inte den andra.
- En händelse rensas först när alla har fått den.
- Den befintliga identiteten flyttas till den primära.
- `JOIN_KEY` skickas inte till extra aggregatorer.
- Kommandon från en extra aggregator ignoreras.
- End to end (`tools/e2e`): två aggregatorer, den ena nere en stund.

### Leverans

Två PR:er, A först: A är fristående och minskar förvirringen direkt. B är
större (datamodell plus uplink). Varje PR har egna tester. Ingen release
förrän underhållaren ber om det. Release notes ska beskriva migreringen
(A) och `ALSO_REPORT_TO` (B).

## Öppna frågor

Inga kvar. Besvarat 2026-11-28: migreringen skriver filen automatiskt,
`serial:` krävs alltid, inställningen heter `ALSO_REPORT_TO`, och en extra
aggregator får allt och visar noden som vilken nod som helst. Kommandon tas
bara emot från den egna.
