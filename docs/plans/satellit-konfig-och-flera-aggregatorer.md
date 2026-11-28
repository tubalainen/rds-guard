# Satellit: radion bara i receivers.yml, och flera aggregatorer

Status: förslag 2026-11-28 (issue #106), väntar på godkännande. Inget är byggt.

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
`receivers.yml`, och alla SDR-inställningar tas bort ur `.env`. Satelliten
ska själv kunna skicka till flera aggregatorer.

## Del A: radion bara i receivers.yml

### Förslag

- `receivers.yml` blir det enda stället för radion. `FM_FREQUENCY`,
  `FM_FREQUENCIES` och `RTL_DEVICE_SERIAL` försvinner ur `.env.example`,
  README och `config.py`. Standardvärdet 103.5M försvinner också.
- **`serial:` är valfritt när exakt en mottagare är ansluten.** Den minsta
  filen blir då:

  ```yaml
  receivers:
    - frequencies: [103.5M]
  ```

  Med två eller fler anslutna mottagare krävs `serial:` som i dag.
- **Utan receivers.yml** skriver satelliten `rds-data/receivers.example.yml`
  med de mottagare som är anslutna (serienummer, typ) och en kommenterad
  mall. Sedan stoppar den med ett tydligt fel: "Skapa rds-data/receivers.yml
  (se receivers.example.yml bredvid)". Den gissar inte en frekvens.
- **Migrering av befintliga installationer:** saknas `receivers.yml` men
  `FM_FREQUENCY`/`FM_FREQUENCIES` är satta i `.env`, skriver satelliten en
  gång en `receivers.yml` från dem (med serienumret på den enda anslutna
  mottagaren, eller `RTL_DEVICE_SERIAL`). Den loggar att `.env`-raderna inte
  används längre och kan tas bort. Ingen behöver göra något vid
  uppdateringen.
- Finns både `receivers.yml` och `FM_*` i `.env`: en varning i loggen om
  att `.env`-raderna ignoreras.
- **Ny `.env.satellite.example`** med bara satellitens inställningar
  (aggregator, nodnamn, plats, inspelning). `docker-compose.satellite.yml`
  och README:s satellitavsnitt hänvisar till den. `.env.example` för
  kombinerad drift behåller sin satellitdel, men utan radion.
- README: avsnittet "Dongles: receivers.yml" blir det enda radioavsnittet,
  med ett "kom igång"-steg: starta, läs serienumret i loggen eller i
  `receivers.example.yml`, skriv filen, starta om.

### Invändningar (mina)

- **Ett steg till för nya användare.** Med `.env` räckte en rad. Nu behövs
  en fil i `rds-data/`, som Docker skapar som root vid första starten. Det
  mildras av att `serial:` är valfritt och av mallen som satelliten
  skriver, men det är fortfarande två filer i stället för en.
- **Migreringen skriver i användarens datamapp.** Det är bekvämt men lite
  magiskt. Alternativet är att stoppa med ett fel och be användaren göra
  det själv, men då slutar varje befintlig satellit fungera vid
  uppdateringen tills någon agerar.

## Del B: flera aggregatorer

### Förslag

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

### Tester

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

1. A: ska satelliten skriva `receivers.yml` själv från `.env` vid
   uppdateringen (förslaget), eller stoppa och be användaren?
2. A: räcker valfritt `serial:` med en mottagare, eller vill du att
   serienumret alltid står i filen, så att det blir tydligt när en dongel
   byts?
3. B: namnet `ALSO_REPORT_TO`, eller `AGGREGATOR_URLS` (där den första är
   den primära)? Det senare fungerar inte i kombinerad compose utan ändring
   i compose-filen.
4. B: ska en extra aggregator få andra inställningar än den primära, t.ex.
   bara VMA och inte trafikmeddelanden, eller inget ljud? Förslaget är
   "allt till alla" tills någon behöver mer.
5. B: ska den extra aggregatorn visa noden som "speglad", eller ser den ut
   som vilken satellit som helst? Förslaget: som vilken satellit som helst.
