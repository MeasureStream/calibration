# Contratti dei messaggi

I messaggi fra metrology-service (Kotlin) e calibration-engine (Python). Sono il confine fra i
due servizi: nessuno dei due conosce il codice dell'altro, solo questi documenti.

| Topic | Da → a | Chiave | Messaggio |
|---|---|---|---|
| `calibration-jobs` | metrology-service → motore | `jobId` | `CalibrationJob` |
| `calibration-results` | motore → metrology-service | `jobId` | `CalibrationResult` |

I messaggi sono piccoli: i dati grossi (gradini della sessione, template, procedura, certificato
del campione) il motore li legge via REST, e i documenti prodotti li carica con
`POST /API/metrology/documents`, citandone qui solo l'identificativo e l'impronta.

Coefficienti e covarianza di `calibration-result.example.json` sono quelli veri della run cubica
di riferimento di Mario (`pipeline/test_runs/run5_cubic_13p`); punti, budget e conformità sono
illustrativi.

## Regole

- **Gli esempi sono identici nei due repository**: `contracts/*.example.json` sta sia qui sia in
  `metrology-service/contracts/`, e i test di entrambi li leggono, li deserializzano e li
  riserializzano, pretendendo lo stesso JSON. Chi cambia un esempio lo cambia in tutti e due.
- **`schemaVersion`** è la versione del contratto. Un campo nuovo è sempre facoltativo, con un
  default, così una delle due parti può essere aggiornata prima dell'altra; un cambiamento che
  non si può fare così incrementa `schemaVersion`.
- **La funzione di taratura ha la forma del blocco `calibration` del template**: `type`, la
  formula sotto una chiave che si chiama come `type`, `input` e `c[]`. È la forma che
  sensor-manager sa già valutare, e quella che `calibration-activated` porterà.
- Istanti in ISO 8601 UTC, identificativi del database come numeri, `muExtendedId` come numero
  (l'EUID a 32 bit della MU).
- **Idempotenza**: un risultato si riconosce dal `jobId`. Consegnato due volte, produce un solo
  risultato.
