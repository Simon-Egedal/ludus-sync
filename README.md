# ludus-sync

Synkroniserer dit Ludus-skema til Google Kalender, hver gang du logger ind på Windows.

- Moduler oprettes/opdateres i Google Kalender
- **Aflyste moduler** fjernes
- **Studietimer** bliver lilla (farven "Drue")
- **Lektier** skrives i noten på modulet
- Rører kun begivenheder scriptet selv har oprettet – resten af din kalender er urørt

## Opsætning

1. **Installér**
   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m playwright install chromium
   ```

2. **Google-adgang** (én gang)
   - Gå til <https://console.cloud.google.com/>, opret et projekt, og slå **Google Calendar API** til.
   - *OAuth consent screen*: vælg "External", tilføj dig selv som testbruger.
   - *Credentials → Create credentials → OAuth client ID → Desktop app*. Download JSON-filen og gem den som `credentials.json` i denne mappe.

3. **Konfiguration**: kopiér `config.example.toml` til `config.toml` og udfyld skole-URL og brugernavn.

4. **Log ind** (gemmer adgangskoden i Windows Credential Manager og forbinder Google):
   ```powershell
   .venv\Scripts\python -m ludus_sync setup
   ```

5. **Test**: `.venv\Scripts\python -m ludus_sync sync`

6. **Kør ved opstart**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
   ```

Log: `%APPDATA%\ludus-sync\sync.log`

## Sådan læses Ludus

Scriptet logger ind på `/ui/main`, åbner **Skemaer → Mit skema** uge for uge og **Lektier** for samme periode.
I ugeskemaet har hver modul en CSS-klasse der fortæller typen:

| Klasse | Betyder | I Google Kalender |
|---|---|---|
| `ugeskema-skemabrik-element` | almindeligt modul | oprettes |
| `ugeskema-aendret-lektion-element` med lokale "Studietime …" | studietime | lilla, "Studietime · …" |
| `ugeskema-aflyst-lektion-element` | aflyst | fjernes |
| `ugeskema-begivenhed-element` | begivenhed (fx lektiecafé) | oprettes |

Lektier sættes på første modul i faget den dag. Hvis Ludus ændrer layout, gemmer
`python -m ludus_sync discover` siderne i `%APPDATA%\ludus-sync\discover` til fejlsøgning.

## Lessor Workforce (vagter)

Et separat program (`ludus_sync/lessor.py`) lægger dine vagter fra Lessor Workforce i Google Kalender, 8 uger frem.
Det logger ind på <https://workforce.lessor.dk/> med virksomhedsnavn, brugernavn og adgangskode og henter
dit personlige iCal-link under **Profil → iCal** (kun "Vagter" skal være sat til). Linket gemmes, så senere
kørsler bare henter kalenderen uden browser; holder linket op med at virke, logges der ind igen.

1. Udfyld `[lessor]`-sektionen i `config.toml` (se `config.example.toml`).
2. `.venv\Scripts\python -m ludus_sync.lessor setup` – gemmer Lessor-adgangskoden (eller brug `LESSOR_PASSWORD` i `.env`).
3. Test: `.venv\Scripts\python -m ludus_sync.lessor sync`
4. Kør `install_startup.ps1` igen – så oprettes også opgaven `LessorSync`.

Vagterne lægges i den kalender `[lessor] calendar_id` peger på (fx din arbejdskalender) og mærkes
separat, så de to programmer aldrig sletter hinandens begivenheder. Heldagsposter (ferie, fravær) springes over.
Log: `%APPDATA%\ludus-sync\lessor.log`. Fejlsøgning: `python -m ludus_sync.lessor discover --headed`
gemmer siderne i `%APPDATA%\ludus-sync\discover_lessor`.
