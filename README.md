# calibration-engine

Il motore di taratura di MeasureStream. Riceve i job da metrology-service sul topic Kafka
`calibration-jobs`, legge via REST ciò che gli serve, esegue il calcolo e pubblica l'esito su
`calibration-results`. Non ha database e non espone API.

| Job | Che cosa produce |
|---|---|
| `SESSION_FIT` | Funzione di taratura per ogni sensore della sessione, covarianza, campo tarato, errori as-found, budget per punto, conformità, decisione suggerita |
| `VERIFICATION` | Errori as-left della nuova funzione sui punti di verifica |
| `CERT_FIT` | Funzione dell'esemplare ricavata dai punti di un certificato esterno |
| `DOCUMENTS` | DCC XML validato con l'XSD, PDF leggibile, grafici, immagine del Block A |

## La pipeline è di Mario Attrovio

Il calcolo metrologico — fit lineare, quadratico, cubico e Steinhart-Hart, propagazione GUM con
covarianza, budget per punto, PFA sull'as-found, DCC XML PTB 3.3.0, PDF — è il lavoro di tesi di
**Mario Attrovio**, nato nel repository `MeasureStream/calibration`. Questo repository ne è il
seguito: è stato creato clonando quello originale, quindi la storia e la paternità di ogni riga
restano leggibili con `git log` e `git blame`.

Il suo albero vive in [`pipeline/`](pipeline/) così com'era, con il suo README, i suoi documenti e
i suoi test. Il pacchetto `src/calibration_engine` è ciò che gli sta attorno: contratti dei
messaggi, configurazione, consumer Kafka, client REST.

Le correzioni metrologiche previste dalla specifica (tolleranza dal template, as-found e as-left
separati, regressori centrati nel fit cubico, funzione nel formato del template, campione dal suo
certificato) entrano una alla volta, ciascuna con il suo test, nel passo 3.5 del piano.

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
