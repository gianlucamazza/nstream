# Architecture Decision Records

Short, immutable notes recording an architectural decision: the context that forced it,
the choice made, and the consequences accepted. They explain _why_ the code is shaped the
way it is — the companion to `docs/selection.md`, which explains _how_ one subsystem behaves.

Format: MADR-light (Context · Decision · Rationale · Consequences · References), kept terse
and code-anchored (cite `src/nstream/<mod>.py:line`) in the project's documentation tone.

An ADR is append-only: once **Accepted** it is not edited to reflect a later change of mind.
A new ADR supersedes it instead, and the old one is marked `Superseded by NNNN`.

| #                                                    | Title                                                                              | Status   |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------- | -------- |
| [0001](0001-native-debrid-resolver-adapter-layer.md) | Native debrid resolver adapter layer (alongside Torrentio)                         | Accepted |
| [0002](0002-realdebrid-native-integration.md)        | RealDebrid: stay on Torrentio, no native cache path                                | Accepted |
| [0003](0003-torbox-native-integration.md)            | TorBox native resolver                                                             | Accepted |
| [0004](0004-premiumize-native-integration.md)        | Premiumize native resolver                                                         | Accepted |
| [0005](0005-cast-delivery-tier2-remux.md)            | Cast delivery: Tier-2 on-host audio remux to a complete file                       | Accepted |
| [0006](0006-cast-mirror-realtime-1080p.md)           | Cast realtime via headless mirror (1080p H.264)                                    | Accepted |
| [0007](0007-cast-metadata-via-castbridge.md)         | Native cast delivery: castbridge sender + nstream Range server (metadata + events) | Accepted |
| [0008](0008-castbridge-socket-activation.md)         | castbridge daemon: systemd socket activation (retire spawn code)                   | Proposed |
| [0009](0009-movies-series-separation.md)             | Movies / TV-series separation: typed home sections, typed flows                    | Accepted |
| [0010](0010-background-cast-discovery.md)            | Background Chromecast discovery with a verified disk cache                         | Accepted |
| [0011](0011-unify-cast-delivery-state-machine.md)    | Unify the castbridge→catt cast-delivery state machine                              | Accepted |
| [0012](0012-cast-subtitles-webvtt-text-track.md)     | Cast subtitles as a WebVTT text track on the castbridge path                       | Accepted |
| [0013](0013-custom-cast-receiver.md)                 | Custom Cast (CAF) receiver with runtime codec capability probing                   | Proposed |
| [0014](0014-verified-cache-preranking.md)            | Verify cached availability before committing to a pick                             | Accepted |
| [0015](0015-mirror-over-4k-remux.md)                 | Prefer the realtime mirror over a 4K remux for Dolby-only releases                 | Accepted |
| [0016](0016-receiver-track-observability.md)         | Surface the receiver's real track + error state                                    | Accepted |
| [0017](0017-cast-video-codec-vetting.md)             | Vet the REAL video codec before casting                                            | Accepted |
| [0018](0018-subtitle-hash-match-and-retime.md)       | Subtitle sync: exact-file hash match + manual retime                               | Accepted |
| [0019](0019-audio-anchored-subtitle-sync.md)         | Audio-anchored subtitle sync via alass                                             | Superseded by 0020 |
| [0020](0020-native-subtitle-alignment.md)            | Native sparse-evidence subtitle alignment and audio arbitration                    | Accepted |
| [0021](0021-per-invocation-constraint-parity.md)     | Per-invocation constraints hold across every selection path                        | Accepted |

New ADR: copy [`0000-template.md`](0000-template.md), take the next number, add a row above.
