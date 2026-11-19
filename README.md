# calibration-engine

Il motore di taratura di MeasureStream. Riceve i job da metrology-service sul topic Kafka
`calibration-jobs`, legge via REST ciò che gli serve, esegue il calcolo e pubblica l'esito su
`calibration-results`. Non ha database e non espone API.

| Job | Che cosa produce | Stato |
|---|---|---|
| `SESSION_FIT` | Funzione di taratura per ogni sensore della sessione, covarianza, campo tarato, errori as-found, budget per punto, conformità, decisione suggerita | Attivo |
| `DOCUMENTS` | DCC XML (validato con l'XSD se configurato) e PDF, caricati su metrology-service | Attivo |
| `VERIFICATION` | Errori as-left della nuova funzione sui punti di verifica | Risponde `NOT_SUPPORTED` (passo 3.5) |
| `CERT_FIT` | Funzione dell'esemplare ricavata dai punti di un certificato esterno | Risponde `NOT_SUPPORTED` (passo 5.3) |

Come i gradini di una sessione diventano l'ingresso della pipeline, e come le sue uscite
diventano il contratto: `src/calibration_engine/adapter.py`, e il § 5 di
`IMPLEMENTAZIONE_METROLOGIA.md` nella cartella dei repository.

## La pipeline è di Mario Attrovio

Il calcolo metrologico — fit lineare, quadratico, cubico e Steinhart-Hart, propagazione GUM con
covarianza, budget per punto, PFA sull'as-found, DCC XML PTB 3.3.0, PDF — è il lavoro di tesi di
**Mario Attrovio**, nato nel repository `MeasureStream/calibration`. Questo repository ne è il
seguito: è stato creato clonando quello originale, quindi la storia e la paternità di ogni riga
restano leggibili con `git log` e `git blame`.

Il suo albero vive in [`pipeline/`](pipeline/) così com'era, con il suo README, i suoi documenti e
i suoi test. Il pacchetto `src/calibration_engine` è ciò che gli sta attorno: contratti dei
messaggi, configurazione, consumer Kafka, client REST.

La pipeline non è modificata: la usa `src/calibration_engine/adapter.py`, che le scrive i file
d'ingresso e ne traduce le uscite. Tolleranza e soglia di PFA (criticità C7) le arrivano già dal
template e dalla procedura come argomenti. Le altre correzioni metrologiche (as-found e as-left su
punti separati, regressori centrati nel fit cubico, campione dal suo certificato) entrano una
alla volta, ciascuna con il suo test, nel passo 3.5 del piano.

## Sviluppo

Python 3.12, progetto gestito con [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest
uv run calibration-engine check
```

`uv run pytest` esegue sia i test del motore (`tests/`) sia quelli di Mario (`pipeline/test/`).

## Configurazione

Nessun segreto sta nel repository, che è pubblico: il segreto del client Keycloak del motore
arriva come variabile d'ambiente o file montato.

| Variabile | Contenuto |
|---|---|
| `CALIBRATION_PIPELINE_DIR` | Cartella della pipeline (nell'immagine: `/app/pipeline`) |
| `ENGINE_KAFKA_BOOTSTRAP_SERVERS` | Kafka (predefinito `kafka:29092`) |
| `ENGINE_CONSUMER_GROUP` | Gruppo del consumer (predefinito `calibration-engine`) |
| `ENGINE_METROLOGY_URL` | Il gateway, da cui passano metrology-service e il registro dei template |
| `ENGINE_TOKEN_URL`, `ENGINE_CLIENT_ID` | Il client Keycloak con client credentials |
| `ENGINE_CLIENT_SECRET` | Il segreto, oppure il file `/run/secrets/engine_client_secret` |
| `ENGINE_JOB_TIMEOUT_SECONDS` | Oltre, la pipeline si termina e il job risulta `TIMEOUT` (predefinito 600) |
| `ENGINE_DCC_XSD_PATH` | L'XSD PTB 3.3.0 del DCC; senza, il DCC non è validato |
| `ENGINE_HEARTBEAT_FILE` | Il file che il consumer tocca a ogni giro (predefinito `/tmp/calibration-engine.heartbeat`) |

## Comandi

| Comando | A cosa serve |
|---|---|
| `calibration-engine consume` | Il consumer dei job: è il comando predefinito dell'immagine |
| `calibration-engine health` | L'healthcheck di Docker: vero se il consumer ha fatto un giro negli ultimi 180 s |
| `calibration-engine check` | Dipendenze e pipeline al loro posto |
| `calibration-engine memory-probe` | Un job tipico (fit cubico, grafici, PDF, DCC) con picco di memoria e durata |
| `calibration-engine whoami` | Token da Keycloak e chiamata a metrology-service attraverso il gateway |
