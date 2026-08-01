# ADR 0026 — Metadati stream: i campi strutturati del protocollo prima del testo libero

- Stato: accepted
- Data: 2026-08-01
- Contesto: ADR 0024 (scoperta multi-sorgente), `bp-provider-neutral-adapter-design`,
  [[ws-anti-theater]]

## Contesto

`quality._text()` (`src/nstream/quality.py:66`) costruisce l'unica stringa da cui si estrae
quasi tutto — risoluzione, codec, HDR/DV, dimensione, lingue, sorgente, audio, seeder — come
`name + "\n" + title`. Ogni parser di `_parse_stream_uncached` legge da lì.

Il protocollo Stremio dichiara `title` **deprecato**:

> `title` — "warning: this will soon be deprecated in favor of `stream.description`"
> `description` — "previously `stream.title`"

Comet ha già completato la migrazione: su 35 righe di `tt27722375`, `description` è presente
35/35 e `title` 0/35. nstream legge quindi il campo deprecato e ignora quello corrente.
Misurato sulla riga reale `[RD⚡] Comet 1080p`:

| campo dominio  | valore nel payload                                   | parsato oggi  |
| -------------- | ---------------------------------------------------- | ------------- |
| `release_name` | `Between The Temples (2024) [Bluray 1080p][Esp]…mkv` | `''`          |
| `size_gb`      | `videoSize: 9366675221`                              | `0.0`         |
| `source`       | `⭐ BluRay`                                          | `''`          |
| `languages`    | `🇪🇸` nella description, `[Esp]` nel filename         | `frozenset()` |
| `audio/codec`  | dal filename                                         | `''` / `''`   |

Conseguenza osservata in campo, non ipotetica: con `audio_langs=['ita','eng']` configurato, la
selezione ha scelto un Blu-ray spagnolo perché il filtro lingua non poteva vederne la lingua.
Un filtro che non può fallire rumorosamente ha fallito in silenzio — [[ws-anti-theater]].
In più `_title_matches('', …)` concede il beneficio del dubbio (`quality.py:612`), quindi con
`release_name` vuoto la guardia contro la release del film sbagliato è disattivata.

C'è anche un'incoerenza interna preesistente, indipendente da Comet: **i due dedup usano due
chiavi diverse per la stessa nozione**. `api._dedup_by_release` collassa per
`behaviorHints.filename`, `quality._dedup_by_release` per `release_name.lower()`
(`quality.py:745`), cioè la prima riga di `title`. Sulla stessa release i due possono non
concordare.

## Decisione

### 1. Catena di precedenza per campo: strutturato → testo

Ogni campo dichiara una sola catena, uguale per tutti gli addon:

| campo          | 1ª fonte (strutturata)    | degrado                                  |
| -------------- | ------------------------- | ---------------------------------------- |
| `release_name` | `behaviorHints.filename`  | 1ª riga di `description`, poi di `title` |
| `size_gb`      | `behaviorHints.videoSize` | regex `💾` sul corpus testuale           |
| `container`    | estensione del `filename` | coda del path di `url` (invariato)       |

Gli altri campi — risoluzione, codec, HDR/DV, audio, lingue, sorgente, seeder — restano
euristici: il protocollo non li modella, e la prosa è la loro unica fonte legittima.

### 2. Il corpus testuale è l'unione, non un campo solo

`_text()` diventa `name + description + title + filename`. `title` resta letto benché
deprecato: Torrentio lo popola ancora e il protocollo dice "soon", non "removed". È
un'unione, non una sostituzione — nessun ramo per addon.

### 3. Nessun caso speciale per addon, ma degrado obbligatorio

La catena è una sola per tutte le fonti (`bp-provider-neutral-adapter-design` §1). Poiché lo
stesso principio impone di non assumere che ogni endpoint regga il modello (§2 — e qui è
concreto: `behaviorHints` manca su 1 riga Comet su 35), ogni anello degrada al successivo e
nessuno può presumere il precedente.

### 4. Fuori perimetro (decisioni separate)

La spec espone altri campi oggi inutilizzati — `behaviorHints.videoHash` (hash OpenSubtitles),
`bingeGroup` (selezione automatica dell'episodio), `sources` (tracker e nodi DHT). Sono
opportunità reali ma toccano ADR 0018, l'auto-advance e il fallback P2P: **non sono decisi
qui**. Questo ADR decide solo da dove si leggono i metadati di una release.

## Razionale

- **`videoSize` è esatto, la regex è un'approssimazione.** Il guadagno non riguarda solo gli
  addon `description`: anche su Torrentio si passa da una stringa arrotondata a un intero in
  byte, che è ciò che serve alle guardie di dimensione su remux e cast.
- **`filename` è l'identità canonica della release** — la spec lo raccomanda esplicitamente
  come chiave per identificare il contenuto — mentre la prima riga di `title` è prosa che
  l'addon compone come crede.
- **Il degrado esplicito** impedisce il `StreamInfo` per metà vuoto e per metà plausibile.

## Conseguenze

- **Cambia anche il comportamento su Torrentio**, non solo su Comet: `release_name` passa
  dalla prima riga di `title` al `filename`. Dove i due divergono cambiano le chiavi di dedup
  e l'esito di `_title_matches`. È l'effetto voluto — allinea `quality._dedup_by_release` a
  `api._dedup_by_release`, chiudendo l'incoerenza descritta sopra — ma va verificato con un
  test che confronti le due chiavi sulla stessa release.
- **`videoSize` e la size testuale non misurano la stessa cosa**: il primo è il byte-count del
  **file video**, la seconda la dimensione del torrent, che su un multi-file include gli
  extra. La precedenza rende le guardie più corrette e i numeri diversi da prima.
- `_PARSE_CACHE` (`quality.py:244`) deve includere `description` e i `behaviorHints` letti
  nella chiave, o due righe distinte collassano sullo stesso `StreamInfo`.
- Includere `filename` nel corpus linguistico amplia la superficie di falsi positivi sulle
  lingue; `_LANG_RE` è già match a confine di parola, il rischio resta sui nomi di gruppo.
- `release_name` non più vuoto **riattiva** su Comet la guardia sul titolo e il dedup
  cross-tracker, finora inerti: i conteggi di `--explain` cambiano a parità di catalogo.
- Sopravvive alla deprecazione di `title` senza altri interventi.

## Riferimenti

- Stremio Addon SDK — Stream object:
  `https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/api/responses/stream.md`
- `src/nstream/quality.py:66` (`_text`), `:244` (`parse_stream`), `:262`
  (`_parse_stream_uncached`), `:612` (`_title_matches`), `:745` (dedup per release)
- `src/nstream/api.py:488` (`_release_rank`), `:496` (`_dedup_by_release`)
- ADR 0024 (scoperta multi-sorgente)
