# Plan: Trafikverkets händelser (Situation) i RDS Guard

Status: förslag, inget är byggt. Undersökt 2026-11-26.

## Varför

Radion säger "E18 vid Bergviksmotet mot Grums". Vår placering blir då
±500 m i bästa fall och ±5–25 km i sämre fall, och sedan hörs inget mer
förrän nästa sändning. Trafikverket publicerar samma händelser med exakt
position (punkt och ofta vägsträcka), påverkad riktning, påverkansgrad och
en text som uppdateras medan händelsen pågår ("Bärgning pågår", ny
beräknad sluttid, körfält öppnat, borttagen).

Med det kan vi:

1. **Placera exakt**: flytta stiftet till Trafikverkets koordinat och rita
   den drabbade vägsträckan.
2. **Visa förloppet**: en tidslinje per händelse med Trafikverkets
   versioner, även de som aldrig sänds i radion.
3. **Avsluta korrekt**: när Trafikverket tar bort eller pausar händelsen
   blir stiftet grått, även om radion aldrig säger "öppet igen".
4. **Förbättra transkriberingen** (bonus): namnen i aktiva händelser nära
   noden ("Trafikplats Våxnäs", "Väg 61") blir ord som transkriberaren
   lyssnar efter.

## Vad API:t ger (verifierat i Trafikverkets schema)

- Endpoint `https://api.trafikinfo.trafikverket.se/v2/data.json`, POST med
  XML-fråga och `authenticationkey`. Nås från molnmiljön (svarar 401 utan
  nyckel).
- Objekttyp `Situation`, namespace `Road.TrafficInfo`, **schemaversion
  1.6** (nytt i 1.6: `Deviation.Suspended`).
- En `Situation` (Id, VersionTime, ModifiedTime, Deleted) innehåller en
  lista `Deviation`. Fält i Deviation som vi har nytta av:

| Fält | Användning |
|---|---|
| `Id`, `VersionTime` | identitet och version, för tidslinjen |
| `MessageType` / `MessageTypeValue` | Olycka, Hinder, Restriktion, Trafikmeddelande, Viktig trafikinformation, Vägarbete, Färjor |
| `MessageCode` / `MessageCodeValue` | t.ex. "Beläggningsarbete" |
| `Header`, `Message` | rubrik och fri text, det som uppdateras ("Bärgning pågår") |
| `LocationDescriptor`, `PositionalDescription`, `RoadName` | platsbeskrivning, platsnamn till matchning och transkribering |
| `RoadNumber`, `RoadNumberNumeric` | "Väg 73" / 73, för matchning mot radions väg |
| `AffectedDirection(Value)` | BothDirections / OneDirection; linjens koordinater är ordnade i färdriktningen |
| `Geometry.Point.WGS84`, `Geometry.Line.WGS84` | exakt punkt och sträcka (WKT) |
| `SeverityCode` / `SeverityText` | 1, 2, 4, 5: ingen … mycket stor påverkan |
| `TrafficRestrictionType`, `NumberOfLanesRestricted`, `TemporaryLimit` | "körfält blockerat", antal körfält |
| `StartTime`, `EndTime`, `ValidUntilFurtherNotice` | giltighet och beräknad sluttid |
| `CreationTime`, `Creator`, `CountyNo` | när och av vem, län |
| `Suspended`, `Situation.Deleted` | händelsen inaktiv eller borttagen |
| `SafetyRelatedMessage`, `WebLink`, `IconId` | övrigt |

- **Bara förändringar**: `changeid="0"` första gången, sedan
  `changeid=<LASTCHANGEID>` med `includedeletedobjects="true"` ger endast
  ändrade och borttagna poster. Borttagna poster finns kvar 24 h med
  `Deleted=true`.
- **Strömning**: svaret har fältet `INFO/SSEURL`, alltså stöd för
  Server-Sent Events. Dokumentationssidan om strömning kräver inloggning;
  detaljerna verifieras med nyckeln innan vi väljer det.
- **Geografiskt filter**: `<WITHIN name="Deviation.Geometry.Point.WGS84"
  shape="center" value="13.50 59.38" radius="150000m"/>` (radie i meter
  med suffixet `m`), eller `CountyNo`.

Exempel på fråga:

```xml
<REQUEST>
  <LOGIN authenticationkey="NYCKEL"/>
  <QUERY objecttype="Situation" namespace="Road.TrafficInfo"
         schemaversion="1.6" changeid="0" includedeletedobjects="true">
    <FILTER>
      <WITHIN name="Deviation.Geometry.Point.WGS84" shape="center"
              value="13.50 59.38" radius="150000m"/>
      <IN name="Deviation.MessageType"
          value="Olycka,Hinder,Restriktion,Trafikmeddelande,Viktig trafikinformation"/>
    </FILTER>
  </QUERY>
</REQUEST>
```

## Hur det passar arkitekturen

Följer regeln "lokalt först, extra tjänster som tillval":

- Ny inställning `TRAFIKVERKET_API_KEY`. Tom = allt fungerar exakt som
  i dag. Nyckeln är gratis, inga kostnader per meddelande, men kräver
  internet.
- Trafikverket **ersätter inte** vår egen placering; den används för att
  bekräfta och förfina den. Radiomeddelanden som inte matchar något hos
  Trafikverket placeras som i dag.
- Fungerar med både regelmotorn och Claude.

## Komponenter

### 1. `trafikverket.py` (ny, delas av satellit och aggregator)

- `TrafikverketFeed`: en tråd som hämtar ändringar med `changeid` var
  60:e sekund (eller SSE om det visar sig stabilt), filtrerat på
  område runt noden/noderna och meddelandetyper.
- Tolkar WKT (`POINT (lon lat z)`, `LINESTRING (...)`) utan nya beroenden.
- Skriver till databasen via callbacks, som `geo_service` gör i dag.
- Backoff och loggning vid fel; ett fel stoppar aldrig resten av RDS Guard.

### 2. Lagring (i `geolocate.py`-schemat, båda databaserna)

```sql
trv_deviations (id TEXT PK, situation_id, message_type, road_number,
                header, message, location, severity_code, severity_text,
                restriction, direction, lat, lon, line_wkt, start_time,
                end_time, version_time, status, first_seen, last_seen)
trv_deviation_versions (deviation_id, version_time, changed_json)
                -- bara de fält som ändrats, för tidslinjen
incidents.trv_id, incidents.trv_score   -- kopplingen
```

`LASTCHANGEID` sparas så att en omstart fortsätter där den slutade.
Rensas med samma retention som incidenterna.

### 3. Matchning radio ↔ Trafikverket (`geolocate.match_trv`)

Körs när ett radiomeddelande har placerats, och igen när en ny
Trafikverkshändelse kommer in (radion kan vara före Trafikverket).
Poäng utifrån:

- **väg**: samma vägnummer (krav när båda har ett);
- **avstånd**: Trafikverkets punkt eller linje inom vår cirkel (+ marginal);
- **tid**: Trafikverkets `CreationTime`/`StartTime` inom −3 h … +30 min
  från sändningen;
- **typ**: "olycka", "vägarbete", "hinder", "djur på vägen" i texten mot
  `MessageType`/`MessageCode`;
- **riktning**: radions bäring mot linjens riktning, om båda finns.

Bästa träff över en tröskel kopplas. Vid en träff: stiftet flyttas till
Trafikverkets punkt, radien sätts till ~200 m, `level = "trafikverket"`,
linjen sparas. Om flera radiohändelser pekar på samma Trafikverkshändelse
slås de ihop.

Mäts med `tools/measure_geolocation.py`, som utökas med Trafikverkets
historik för de 40 riktiga P4 Värmland-meddelandena (kräver att data
samlas in några veckor).

### 4. Uppdateringar och status

- Ny version hos Trafikverket → rad i `trv_deviation_versions`,
  incidentens `last_seen` förlängs (stannar på kartan så länge den är
  aktiv hos Trafikverket, inte bara 12 h).
- `Deleted` eller `Suspended` → incidenten blir grå ("Avslutad enligt
  Trafikverket").
- WebSocket-meddelande `trafikverket` så att kartan uppdateras direkt.

### 5. Webbgränssnitt (`static/shared/map.js`, händelsesidan)

- Kopplad incident: exakt stift, drabbad sträcka ritad som linje,
  påverkansgrad som färg, liten Trafikverket-markering.
- Popup och händelsesida: **tidslinje** med radiosändningar och
  Trafikverkets versioner sammanflätade i tidsordning ("13:05 Radio …",
  "13:12 TRV: Olycka, Stor påverkan, ett körfält blockerat",
  "13:40 TRV: Bärgning pågår, beräknas klart 14:30", "14:22 TRV: borttagen").
- Status-sidan visar flödets hälsa (senaste hämtning, antal aktiva).

### 6. MQTT och pushregler

- `rds/alert` får fältet `trafikverket` (id, typ, påverkan, länk) när
  meddelandet matchats.
- Nytt ämne `rds/trafikverket/update` för versioner av kopplade händelser.
- Valfritt i pushreglerna: "skicka även Trafikverkets uppdateringar för
  händelser som matchat regeln" (t.ex. när E18 öppnar igen).

### 7. Transkriberingshjälp

`GeoService.hints()` får namnen ur aktiva Trafikverkshändelser nära noden
(vägnummer, trafikplatser i `LocationDescriptor`). Till Claude skickas
aktiva händelser som kandidater, så att Claude kan säga "detta är TRV-id X".

### 8. Aggregatorn

Samma modul, ett flöde med ett område som täcker alla noders
`NODE_LOCATION`. Satelliter med `LOCAL_PROCESSING=false` behöver ingen
nyckel.

## Ordning (förslag på releaser)

1. **v0.6.0 – grund**: `trafikverket.py`, lagring, status, inställningar,
   tester med inspelade svar (inga nätverksanrop i testerna).
2. **v0.6.0 – matchning och karta**: matchning, exakt stift, linje,
   tidslinje, grått vid borttagen.
3. **v0.6.x – push och MQTT**, transkriberingshjälp, aggregatorn,
   mätverktyget.

## Risker och öppna frågor

- Trafikverket publicerar inte alltid samma händelser som P4, och ibland
  senare. Matchningen måste klara att vänta och att inte hitta något.
- Mindre olyckor på kommunala vägar saknas ofta hos Trafikverket.
- Vägarbeten är många och långlivade: risk för felmatchning om de tas med.
- Villkoren för användning (licens, anropsfrekvens) ska läsas på
  datautbytesportalen; strömningssidan kräver inloggning.
