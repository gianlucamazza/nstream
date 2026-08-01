# ADR 0027 — Circuit breaker per addon sulle fonti stream

- Stato: proposed
- Data: 2026-08-01
- Contesto: ADR 0024 (scoperta multi-sorgente), ADR 0025 (classificazione sorgenti morte),
  [[ws-anti-theater]], [[ws-measure-before-optimizing]]

## Contesto

ADR 0025 ricorda le **sorgenti** morte (una release che risponde 404/410) in una negative
cache persistente. Non esiste l'equivalente un livello sopra: un **addon** irraggiungibile
viene interrogato di nuovo a ogni gather di ogni esecuzione.

Il 2026-08-01 l'origine di Torrentio è caduta (Cloudflare `HTTP 522`, "connection timed out"
verso l'origine). Misura con il client di nstream, non con curl:

| fonte                               | latenza   | esito                           |
| ----------------------------------- | --------- | ------------------------------- |
| comet.elfhosted.com                 | 0.1s      | ok                              |
| torrentsdb.com                      | 0.1s      | ok                              |
| tmdb-addon / anime-kitsu / catalogs | 0.0s      | ok                              |
| **torrentio.strem.fun (built-in)**  | **84.1s** | `NetworkError` dopo 4 tentativi |

Costo end-to-end di una singola `nstream --json --explain`: **1m49s**, con la risposta
completa già disponibile da Comet in meno di due secondi. `_GATHER_BUDGET = 25.0`
(`src/nstream/api.py:46`) limita quanto si _attende_ una fonte bloccata, ma non evita di
ripagare quell'attesa a ogni gather e a ogni invocazione — e i gather per run sono più d'uno
(ricerca, catalogo, stream).

### Cosa NON è il problema

La politica di retry è già corretta e non va toccata: `net.http_get_json`
(`src/nstream/net.py:154`) fa backoff esponenziale **con jitter** (`util.backoff`), onora
`Retry-After` sui `429` e non ritenta i `4xx`. Il difetto è un altro, ed è di memoria: **ogni
esecuzione riparte convinta che la fonte sia sana**, perché ogni run CLI è un processo nuovo.

### Il vincolo che decide la forma della soluzione

`TIMEOUT = 20.0` con `retries=3`. Cloudflare impiega ~60s a emettere il `522` (misurato:
59.5s). **nstream quel codice non lo riceve mai**: il client scade prima, quattro volte di
seguito. L'aritmetica della misura lo conferma — 84.1s ÷ 4 ≈ 21s per tentativo (timeout +
backoff); quattro `522` effettivamente ricevuti sarebbero costati ~240s.

Conseguenza: un'apertura del breaker guidata dal **codice di stato** sarebbe codice morto
proprio sul caso che ha motivato questo ADR. Il segnale disponibile è il **timeout ripetuto**,
non la risposta.

## Decisione

### 1. Un breaker per addon, non uno globale

Stato per base-URL dell'addon (Torrentio built-in incluso), persistito in `state/`. Un solo
breaker condiviso violerebbe la "resource differentiation" del pattern: Torrentio morto
sbarrerebbe Comet vivo.

### 2. Macchina a tre stati

| stato         | comportamento                                                                          |
| ------------- | -------------------------------------------------------------------------------------- |
| **Closed**    | richieste normali; contatore fallimenti a finestra temporale, si azzera da solo        |
| **Open**      | la fonte è saltata **senza chiamata di rete**; timer di attesa attivo                  |
| **Half-Open** | una sola richiesta di prova: successo → Closed e contatore azzerato; fallimento → Open |

### 3. Il trigger è il fallimento ripetuto, non il codice di stato

Apre il breaker una soglia di fallimenti consecutivi **di qualunque classe ritentabile**,
timeout compresi — che è l'unico segnale che questa caduta produce davvero. L'apertura
accelerata su codice resta prevista solo dove il codice arriva davvero entro il timeout
(`503`/`429` con `Retry-After` che eccede la finestra), come caso aggiuntivo e non come
meccanismo principale.

Un timeout pieno vale più di un errore veloce: un tentativo che consuma l'intero `TIMEOUT`
conta come fallimento **e** come costo, quindi la soglia si esprime in tentativi consecutivi
falliti, non in tempo trascorso.

### 4. La prova di Half-Open è l'operazione reale

La prova è una vera query stream verso quell'addon, non un `manifest.json`. Un manifest può
essere servito dalla cache di un CDN mentre l'origine è a terra: riammetterebbe la fonte su
un'evidenza che non riguarda l'operazione che ci serve. Il pattern è esplicito — in Half-Open
si lascia passare un numero limitato di **richieste dell'applicazione**, non un surrogato.

Il costo della prova sbagliata è un `TIMEOUT` singolo, pagato una volta per finestra invece
che a ogni gather di ogni run.

### 5. Timeout dai percentili della stessa operazione

Il timeout per richiesta va derivato dalla latenza osservata **delle query stream**, non dei
manifest: i manifest sani stanno a 0.1s, ma una query stream a Comet misura 1.3–2.2s. Il
valore va scelto su quest'ultima distribuzione — con un margine, non sul massimo osservato.
Un timeout troppo lungo blocca il thread _prima_ che il breaker possa dichiarare il
fallimento; troppo corto trasforma una fonte lenta ma viva in una fonte "morta".

### 6. Lo stato è visibile, e l'override è manuale

Ogni transizione emette un evento a log; `--explain` riporta gli addon in Open e da quando.
Una fonte saltata in silenzio mentirebbe all'utente sul perché un titolo non compare —
[[ws-anti-theater]]. Un flag di reset manuale (l'analogo per addon di `--forget-dead`)
riporta tutti i breaker a Closed.

## Razionale

Il pattern documentato distingue nettamente i due meccanismi: il Retry riprova _aspettandosi_
di riuscire, il Circuit Breaker **impedisce** un'operazione che probabilmente fallirà. Qui
serve il secondo: nessun numero di tentativi risolve un'origine spenta.

Lo stato Half-Open è ciò che distingue questa decisione dallo "skip a TTL" scartato: senza
prova di riammissione, o si riapre troppo presto (e si ripaga l'attesa) o troppo tardi (e si
resta ciechi su una fonte tornata viva).

## Conseguenze

- Con una fonte in Open, la latenza per run torna nell'ordine dei secondi; il caso peggiore
  smette di essere proporzionale al numero di fonti morte.
- Rischio accettato: una fonte lenta ma funzionante può finire in Open e sparire dai
  risultati. Mitigazioni: soglia a fallimenti consecutivi, stato visibile, override manuale.
- Lo stato persistito è una nuova voce in `state/`. Più processi `nstream` possono girare in
  parallelo: la scrittura deve essere atomica (write+rename) e best-effort come ADR 0025 —
  una corsa perde al più un conteggio, non blocca mai la riproduzione.
- Il primo accesso dopo la scadenza del timer paga un `TIMEOUT` se la fonte è ancora giù: è
  il prezzo della prova reale scelto al punto 4, e va documentato nell'output.
- ADR 0025 resta valido e ortogonale: sorgenti morte e addon morti sono due livelli diversi.

## Riferimenti

- Azure Architecture Center — Circuit Breaker pattern:
  `https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker`
  (stati Closed/Open/Half-Open; "resource differentiation"; "inappropriate time-outs on
  external services"; Half-Open come richieste reali dell'applicazione)
- `src/nstream/net.py:26` (`TIMEOUT`), `:154` (`http_get_json`), `src/nstream/util.py:36`
  (`backoff` con jitter), `src/nstream/api.py:46` (`_GATHER_BUDGET`), `:94` (`_gather`)
- ADR 0024, ADR 0025
