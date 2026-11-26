# Plan: bättre tolkning och placering av trafikmeddelanden (v2)

Status: plan 2026-11-26. Inget byggt ännu.

## Problemet

Ett trafikmeddelande (RDS-TP) är sällan "en händelse på en plats". Det är
oftast en blandning av:

- **nya händelser** (en eller flera),
- **uppdateringar** av något som redan sagts ("bilen med haveri på E18 som
  jag nämnde står nu …", "nu är vägen öppen igen"),
- **påminnelser/upprepningar** ("den näst senaste störningen är vilt …
  precis samma i går"),
- **allmänna varningar för ett område** (halt väglag, snö, oväder, "på
  mindre vägar i norra Värmland"),
- och sådant som inte är trafik (telefonnummer, programprat).

Dagens modell (`geolocate.py`) behandlar allt som en platt lista
"incidents" och kan bara säga `updates_incident` och `cleared`. Det ger de
fel som syns på kartan:

1. **Fel citat i popupen.** Universitetsmotet–Alstermotet (vilt) visar
   början av hela meddelandet, som handlar om Rv 63. Vi sparar ingen
   koppling mellan en händelse och *den del av texten* som handlar om den.
2. **Upprepningar blir nya händelser** eller ändrar beskrivning/status på
   en händelse fast inget nytt hänt. "Omnämnd N gånger" blir därför
   missvisande, och en upprepning av något från i går kan inte kopplas,
   eftersom bara händelser från de senaste 12 h skickas med.
3. **Områdesvarningar** ("Halt väglag på mindre vägar", Sunne ±5 km)
   placeras som en punkt vid en ort, eller försvinner helt när radien blir
   över 25 km. De är inte samma sak som en olycka och borde inte se ut så.
4. **Flertydiga namn.** Claude får bara namnsträngar ("Stolpen (locality,
   Värmlands län)") och vi väljer sedan bland alla med samma namn. Det finns
   fem Stolpen i Värmland; vilken som väljs avgörs av vikt och avstånd, inte
   av sammanhanget (E18, Ölme).
5. **Inget facit.** Vi vet inte hur bra placeringen är, så vi kan inte se om
   en ändring blir bättre eller sämre.

## Princip (oförändrad)

- Allt ska fungera lokalt utan Claude (regelmotorn). Claude är ett tillval
  som gör tolkningen bättre och faller tillbaka till reglerna vid fel.
- Koordinater kommer **bara** från ortnamnsregistret (eller Trafikverket).
  Claude väljer bland verkliga platser, uppfinner aldrig positioner.
- Servern kontrollerar alltid det Claude säger (finns händelsen, ligger
  platsen nära, finns citatet i texten).

## Ny bearbetningsmodell

### 1. Meddelandet delas upp i *poster*, var och en med en typ

Claude (och regelmotorn, enklare) returnerar `items` i stället för
`incidents`. Varje post:

| Fält | Betydelse |
|---|---|
| `kind` | `new` · `update` · `repeat` · `cleared` · `area_warning` |
| `evidence` | Ordagrant utdrag ur den rättade texten som posten bygger på |
| `incident_id` | För `update`/`repeat`/`cleared`: vilken känd händelse |
| `when` | `now` · `earlier` ("för en stund sen") · `yesterday` · `unknown` |
| `road`, `between`, `places`, `toward`, `coming_from`, `both_directions` | Som i dag, men platser anges som **id** ur kandidatlistan (se 2) |
| `area` | För `area_warning`: kommun/län/ort + ev. "mindre vägar", "norra" |
| `what` | Kort svensk beskrivning |
| `confidence` | `high` · `medium` · `low` |

Hur servern hanterar typerna (deterministiskt, i `merge_placements`):

- **new**: placeras och skapar en händelse (eller slås ihop om den
  geografiskt matchar en öppen, som i dag).
- **update**: uppdaterar `what`, riktning, ev. bättre plats. Kräver att
  `incident_id` finns och att platsen (om någon) ligger nära; annars
  behandlas den som `new`.
- **repeat**: räknas som omnämnande (`last_seen`, "Omnämnd N gånger") men
  ändrar **inte** beskrivning eller status. Utan träff och med
  `when=yesterday` skapas den som en händelse märkt "tidigare rapporterad".
- **cleared**: stänger händelsen (grå). Utan träff: sparas inte på kartan.
- **area_warning**: blir en **områdesvarning**, ett eget slag av post (se 4),
  inte en punkt.
- `confidence=low`: visas i listan men inte på kartan.

### 2. Kandidatplatser med id och sammanhang

`candidate_names()` ger i dag namn + typ + län. Nytt: varje kandidat får
ett id och en kort beskrivning av var den ligger, så att Claude kan välja
rätt bland namnlikheter:

```
p17  Stolpen (ort) — 1 km från Ölme, 9 km O om Kristinehamn, vid E18
p18  Stolpen (gård) — 3 km SO om Storfors
p19  Stolpen (gård) — 5 km SO om Årjäng
```

"vid E18" kräver vägdata. Steg 1 utan vägdata: närmaste större ort +
avstånd + väderstreck (finns redan i registret). Steg 2 (valfritt): vägnummer
för trafikplatser (`ref` finns för motorvägskorsningar) och, när
Trafikverket-integrationen är på, Trafikverkets aktiva händelser som
kandidater med exakt position.

Claude returnerar id:n; `resolve()` används som i dag men låst till de
valda platserna. Regelmotorn fortsätter välja som i dag.

### 3. Rikare bild av kända händelser

`open_incidents` skickas med för de senaste **36 h** (inte 12) så att "samma
som i går" kan kopplas, med: id, väg, plats, beskrivning, status, första
och senaste gång, samt senaste `evidence`. Fönstret för vad som *visas som
pågående* på kartan är fortfarande 12 h.

### 4. Områdesvarningar som eget lager

Ny tabell `area_warnings` (område, beskrivning, första/senaste gång,
händelser). På kartan visas de inte som cirklar utan som en lista ovanför
kartan ("Halt väglag: Sunne, mindre vägar · 17:47") och, när området är en
kommun, en svag ytmarkering. Ingen gräns på 25 km. Regelmotorn känner igen
de vanligaste ("halt väglag", "halka", "snö", "blixthalka", "oväder") utan
specifik plats.

### 5. Citatet per händelse

`incident_events` får en kolumn `evidence`. Popupen visar utdraget för just
den händelsen i stället för början av meddelandet. Servern kontrollerar att
utdraget finns i texten (annars: meningen som innehåller platsnamnet).

### 6. Regelmotorn (lokalt, gratis)

Samma utdata, enklare: dela på meningar/`_NEW_PART` som i dag, plus:
- `repeat` vid "nämnde", "som jag sa", "fortfarande", "kvar", "sedan
  tidigare", "samma … i går";
- `area_warning` vid väglagsord utan vägnummer/trafikplats;
- `evidence` = meningen/delen som posten bygger på.

## Facit och mätning (görs först)

1. Samla 40–60 riktiga meddelanden från docker01
   (`curl -s 'http://docker01:8022/api/events?limit=60'`), helst blandade.
2. Märk upp förväntat resultat i `tests/fixtures/geo_corpus.json`: poster,
   typ, väg, plats (namn + ungefärlig position), vilken tidigare post en
   uppdatering gäller.
3. Bygg ut `tools/measure_geolocation.py` att räkna: rätt antal poster,
   rätt typ, placering inom radien, rätt koppling till tidigare händelse.
   Kör regelmotorn (gratis) och Claude (Haiku ≈ 0,5 kr för hela korpusen).
4. Varje steg nedan ska förbättra siffrorna, annars backas det.

## Kostnad

Haiku 5.5 kostar $0,10 / $0,50 per miljon in-/ut-token. Ett meddelande med
kandidatlista och kända händelser blir ca 4 000 token in och ca 1 000 ut
inklusive tänkande ≈ **$0,001 (≈ 1 öre) per meddelande**, ungefär som i dag
plus 30 %. Sonnet 5.5 ($2 / $10) blir ca $0,02 (≈ 20 öre) per meddelande;
erbjuds bara som inställning (`GEO_MODEL`) om mätningen visar att Haiku inte
räcker. `effort` sätts uttryckligen (Haiku 5.5 har `medium` som standard)
och mäts mot facit: `low` om det håller.

## Steg

1. **Facit + mätverktyg** (ovan). Inget annat ändras.
2. **Ny modell, servern**: `items`/`kind`/`evidence`, kandidat-id med
   sammanhang, 36 h-fönster, typhantering i `merge_placements`, kolumn
   `evidence`. Regelmotorn ger samma format. Tester.
3. **Områdesvarningar**: tabell, API, lista på kartsidan.
4. **UI**: citat per händelse i popup, märken "Uppdatering"/"Påminnelse",
   "tidigare rapporterad".
5. **Placera om**: knapp under Inställningar som bygger om händelserna från
   sparade meddelanden med den nya modellen (samma mekanism som
   "Placerar äldre meddelanden …").
6. Senare: Trafikverkets händelser som kandidater (se
   `trafikverket-situation.md`).

## Öppna frågor

- Ska områdesvarningar synas som yta på kartan, eller räcker en lista?
- Får "placera om" skriva över de händelser som finns i dag?
