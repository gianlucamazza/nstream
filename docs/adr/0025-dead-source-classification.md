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

## Post-scriptum (2026-07-30, stesso giorno del rilascio 1.30.0)

**Il caso che ha motivato l'ADR non era una rimozione.** La release che serviva 2.0 MiB contro
7331.8 MiB annunciati era marcata `[RD download]`: Real-Debrid la stava ancora **scaricando**.
Il file parziale cresce fino a diventare quello vero nel giro di minuti — bandirla per 30 giorni
avrebbe bloccato un titolo che stava per funzionare. Il size check è corretto come segnale, la
sua _interpretazione_ era sbagliata.

**Prima correzione (1.30.1), scartata subito.** `probe_url` aveva preso un parametro `complete`
derivato da `parse_stream().cached`: shortfall su cached → `gone`, su uncached → `unknown`. È
un doppio regime che fa dipendere una struttura **con memoria** dal marker cached — lo stesso
marker che questo ADR dichiara inaffidabile al §1. Marker sbagliato = stesso bug, solo più raro:
debito tecnico, non una soluzione. Rimosso in 1.30.2.

**Correzione definitiva (1.30.2): ogni segnale decide solo ciò che prova davvero.**

| Segnale                                     | Prova                   | Verdetto  | Conseguenza               |
| ------------------------------------------- | ----------------------- | --------- | ------------------------- |
| status 404/410/4xx                          | la risorsa non c'è      | `gone`    | scarta **e** ricorda      |
| size servito << annunciato                  | non è riproducibile ora | `unknown` | scarta per questo run     |
| 403/405/416, 5xx, timeout, errore trasporto | niente di conclusivo    | `unknown` | scarta / benefit of doubt |

Il size shortfall non promuove più a `gone` in nessun caso: **non può** distinguere un
trasferimento in corso da un file svuotato, e quella distinzione è l'intero contenuto
dell'inferenza "rimosso". Resta pienamente utile come segnale di non-riproducibilità, che è il
motivo per cui è stato introdotto — evitare il cast di un file inservibile.

Simmetricamente in `_verify_availability`: **qualunque** verdetto non-usable fa cadere il
candidato per questa esecuzione (drop in-memory + demozione del marker), mentre solo `gone`
scrive nella denylist. E `sources_removed` in headless non si deduce più da "la lista è vuota"
ma da quali chiavi risultano effettivamente in denylist: una lista svuotata da sorgenti
semplicemente non pronte è `no_playable_stream`, che è la verità.

Il principio generale, valido oltre questo caso: **una misura può essere giusta e la sua
interpretazione sbagliata**. Denylistare è un'operazione con memoria, quindi esige una prova più
forte del semplice scartare: l'errore sopravvive alla sessione in cui è stato commesso. Quando
la prova disponibile non regge la conseguenza, si abbassa la conseguenza — non si puntella
l'inferenza con un secondo indizio debole.
