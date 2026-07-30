# ADR 0025 — Classificazione delle sorgenti morte e negative cache

- Stato: accepted
- Data: 2026-07-30
- Contesto: ADR 0014 (verifica pre-commit dei cached), `bp-torrent-streaming-client` §2

## Contesto

Il marker `[RD+]` di Torrentio è una stima crowdsourced, non una verità: Real-Debrid non
espone più `instantAvailability` (ADR 0002). ADR 0014 ha introdotto una verifica pre-commit
che sonda i primi N candidati _cached_ e demota quelli che non rispondono.

Restano tre buchi, emersi su un cast reale (`Spider-Man: Brand New Day`, release uncached
7.16 GB E-AC-3): il cast è partito, il receiver non ha mai riprodotto nulla.

1. **`net.url_playable` è un booleano che confonde tre stati diversi.** Un link revocato dal
   debrid (DMCA/eviction), un errore di rete transitorio e un 403 di hotlink-protection
   producono tutti `False`, e un `Range: bytes=0-0` che risponde `200` produce `True` _anche
   quando il corpo servito non è il film_ — alcuni addon/provider servono un placeholder di
   pochi KB al posto del file rimosso. Il probe conferma la raggiungibilità dell'URL, non
   l'esistenza del contenuto.
2. **La demozione è in-memory.** Una release morta viene ri-sondata a ogni esecuzione: lo
   stesso torrent rimosso costa una latenza di probe a ogni ricerca dello stesso titolo, e
   torna in cima al ranking appena il processo termina.
3. **La verifica copre solo i cached.** Il razionale ("gli uncached sono già filtrati dal
   seeder count") non regge: il seeder count descrive la salute dello _swarm_, che è una
   domanda diversa dalla disponibilità _lato debrid_. Un torrent con 40 seeder può essere
   perfettamente vivo in P2P e rimosso da RD.

Conseguenza per l'utente: nessun errore, un cast muto o nero, e il fallimento scoperto solo
con `--status`. Violazione di [[ws-anti-theater]]: il comando riporta successo senza esso.

## Decisione

### 1. Probe classificato a tre stati

`net.url_playable` → `net.probe_url(url, expected_bytes=…) -> Probe`, con
`state ∈ {live, gone, unknown}`:

| Segnale                                                          | Stato     |
| ---------------------------------------------------------------- | --------- |
| `404` / `410` / altri `4xx` non ambigui                          | `gone`    |
| `2xx` con dimensione totale servita << dimensione annunciata     | `gone`    |
| `2xx` con dimensione plausibile (o non dichiarata)               | `live`    |
| `403` / `405` / `416` (metodo o Range rifiutati, risorsa esiste) | `unknown` |
| `5xx`, timeout, errore di connessione                            | `unknown` |

Il **size check** è il discriminante nuovo e quello che cattura il caso reale: si confronta
il totale da `Content-Range`/`Content-Length` con la dimensione annunciata dallo stream
(`quality.parse_stream().size_gb`). Uno scarto di ordini di grandezza — soglia: sotto
`max(8 MiB, 2%)` dell'atteso — è la firma di un placeholder o di un file svuotato. La
soglia è deliberatamente larghissima: nessun film vero ci finisce dentro.

Due proprietà distinte, mai confuse:

- `Probe.usable` — si può tentare _adesso_: vero per `live` e per l'`unknown` da rifiuto di
  metodo (403/405/416, benefit of the doubt come oggi), falso per `gone` e per l'`unknown`
  da errore di trasporto (che continua a innescare il fallback, come oggi).
- `Probe.dead` — vero **solo** per `gone`: è l'unico stato che merita memoria persistente.
  Un guasto transitorio non deve mai bandire una sorgente.

### 2. Negative cache persistente

`gone` è permanente: va ricordato. Nuovo stato in
`XDG_STATE_HOME/nstream/dead-sources.json`, mappa `chiave → {ts, reason}`, con:

- chiave = `infoHash` minuscolo, oppure `behaviorHints.filename`, oppure il nome della
  release (in quest'ordine): stabile fra addon diversi che servono lo stesso torrent
- TTL 30 giorni (un file può tornare disponibile: la lista non è eterna)
- cap 500 voci, prune LRU sulla scrittura
- best-effort: qualunque errore di I/O degrada a "nessuna denylist", mai a un crash

Applicata come **filtro pre-ranking** (`prune_dead`), non come demozione: una sorgente
rimossa non deve competere né comparire nel picker manuale.

### 3. Verifica estesa agli uncached

`_verify_cached_availability` → `_verify_availability`: sonda i primi N candidati con URL
pronto **a prescindere dal marker cached**, su ogni backend che non sia `local`. Stesso cap,
stessa memoizzazione per-run. I cached morti restano demoti (il marker mente sul ranking);
i `gone` — cached o no — finiscono in denylist e spariscono.

### 4. Errore onesto verso l'alto: `sources_removed`

Quando il filtro svuota il set (o lo svuota la verifica appena eseguita), headless
restituisce `error: "sources_removed"` invece di `no_playable_stream`, con il conteggio
delle sorgenti scartate. Il chiamante (skill, agente) può così dire "il titolo esiste ma le
fonti sono state rimosse dal debrid" invece di suggerire un retry inutile.

## Conseguenze

- Il costo di rete non cresce: stesso cap di probe, memoizzati per-run, ora con un beneficio
  che sopravvive al processo.
- Un falso `gone` è il rischio: mitigato dalla soglia larghissima sul size, dal TTL di 30
  giorni e dal fatto che `unknown` non entra mai in denylist.
- `net.url_playable` resta come wrapper booleano (`probe.usable`) per non rompere i
  chiamanti esistenti.
- La denylist è ispezionabile e cancellabile: è un JSON, `--forget-dead` la svuota.
