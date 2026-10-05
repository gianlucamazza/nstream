# Architecture Decision Records

Short, immutable notes recording an architectural decision: the context that forced it,
the choice made, and the consequences accepted. They explain _why_ the code is shaped the
way it is — the companion to `docs/selection.md` (_how_) and `docs/architecture.md` (_where_).

Format: MADR-light (Context · Decision · Rationale · Consequences · References), kept terse
and code-anchored by **symbol** (see [`0000-template.md`](0000-template.md)).

An ADR is append-only: once **Accepted** it is not edited to reflect a later change of mind.
A new ADR supersedes it instead, and the old one is marked `Superseded by NNNN`.

**Proposed** ADRs are open work — also listed in [docs/roadmap.md](../roadmap.md). Empirical
fixtures may live under `NNNN-phase*/` next to the ADR (pattern: ADR 0020).

## By theme

| Theme                         | ADRs                                  |
| ----------------------------- | ------------------------------------- |
| Debrid / backends             | 0001–0004                             |
| Cast delivery & discovery     | 0005–0013, 0015–0017, 0022–0023, 0031, 0035, 0039, 0044–0045 |
| Selection / sources / privacy | 0014, 0021, 0024–0030, 0032, 0047     |
| Subtitles                     | 0012, 0018–0020                       |
| Series / UX structure         | 0009, 0029, 0046–0048                 |

## Index

| #                                                     | Title                                                                       | Status             | Date       |
| ----------------------------------------------------- | --------------------------------------------------------------------------- | ------------------ | ---------- |
| [0001](0001-native-debrid-resolver-adapter-layer.md)  | Native debrid resolver adapter layer (alongside Torrentio)                  | Accepted           | —          |
| [0002](0002-realdebrid-native-integration.md)         | RealDebrid: stay on Torrentio, no native cache path                         | Accepted           | —          |
| [0003](0003-torbox-native-integration.md)             | TorBox native resolver                                                      | Accepted           | —          |
| [0004](0004-premiumize-native-integration.md)         | Premiumize native resolver                                                  | Accepted           | —          |
| [0005](0005-cast-delivery-tier2-remux.md)             | Cast delivery: Tier-2 on-host audio remux to a complete file                | Accepted           | —          |
| [0006](0006-cast-mirror-realtime-1080p.md)            | Cast realtime via headless mirror (1080p H.264)                             | Accepted           | —          |
| [0007](0007-cast-metadata-via-castbridge.md)          | Native cast delivery: castbridge sender + nstream Range server              | Accepted           | —          |
| [0008](0008-castbridge-socket-activation.md)          | castbridge daemon: systemd socket activation (retire spawn code)            | **Proposed**       | 2026-06-06 |
| [0009](0009-movies-series-separation.md)              | Movies / TV-series separation: typed home sections, typed flows             | Accepted           | —          |
| [0010](0010-background-cast-discovery.md)             | Background Chromecast discovery with a verified disk cache                  | Accepted           | —          |
| [0011](0011-unify-cast-delivery-state-machine.md)     | Unify the castbridge→catt cast-delivery state machine                       | Accepted           | —          |
| [0012](0012-cast-subtitles-webvtt-text-track.md)      | Cast subtitles as a WebVTT text track on the castbridge path                | Accepted           | —          |
| [0013](0013-custom-cast-receiver.md)                  | Custom Cast (CAF) receiver with runtime codec capability probing            | **Proposed**       | 2026-07-14 |
| [0014](0014-verified-cache-preranking.md)             | Verify cached availability before committing to a pick                      | Accepted           | —          |
| [0015](0015-mirror-over-4k-remux.md)                  | Prefer the realtime mirror over a 4K remux for Dolby-only releases          | Accepted           | —          |
| [0016](0016-receiver-track-observability.md)          | Surface the receiver's real track + error state                             | Accepted           | —          |
| [0017](0017-cast-video-codec-vetting.md)              | Vet the REAL video codec before casting                                     | Accepted           | —          |
| [0018](0018-subtitle-hash-match-and-retime.md)        | Subtitle sync: exact-file hash match + manual retime                        | Accepted           | —          |
| [0019](0019-audio-anchored-subtitle-sync.md)          | Audio-anchored subtitle sync via alass                                      | Superseded by 0020 | —          |
| [0020](0020-native-subtitle-alignment.md)             | Native sparse-evidence subtitle alignment and audio arbitration             | Accepted           | —          |
| [0021](0021-per-invocation-constraint-parity.md)      | Per-invocation constraints hold across every selection path                 | Accepted           | —          |
| [0022](0022-cast-container-vetting.md)                | Vet the cast CONTAINER (mkv → MP4 rewrap) before casting                    | Accepted           | —          |
| [0023](0023-mirror-fidelity-and-explicit-force.md)    | Mirror fidelity (HDR→SDR) and an explicit `--mirror` that forces            | Accepted           | —          |
| [0024](0024-multi-source-stream-discovery.md)         | Multi-source stream discovery beyond Torrentio                              | Accepted           | —          |
| [0025](0025-dead-source-classification.md)            | Classify removed sources (size-aware probe) and remember them               | Accepted           | —          |
| [0026](0026-structured-stream-metadata-precedence.md) | Stream metadata: protocol structured fields before free text                | Accepted           | 2026-08-01 |
| [0027](0027-per-addon-circuit-breaker.md)             | Per-addon circuit breaker on stream sources                                 | Accepted           | 2026-08-01 |
| [0028](0028-content-duration-vetting.md)              | Vet the REAL duration against the expected runtime                          | Accepted           | —          |
| [0029](0029-one-continuation-policy.md)               | One continuation policy; advance decided once, never in a delivery backend  | Accepted           | —          |
| [0030](0030-explicit-year-hard-constraint.md)         | An explicit `--year` is a hard constraint on headless title selection       | Accepted           | —          |
| [0031](0031-delivery-backend-reports-started.md)      | A delivery backend reports whether the cast started; `ok: true` requires it | Accepted           | —          |
| [0032](0032-p2p-privacy-gate-at-the-swarm-join.md)    | The P2P privacy gate lives at the swarm join, not at one call site          | Accepted           | —          |
| [0033](0033-exhaustion-raises-none-means-esc.md)      | Exhaustion raises; `None` means the user backed out                         | Accepted           | 2026-08-10 |
| [0034](0034-bounded-effects-and-playback-evidence.md) | Bounded effects, local playback evidence, and reproducible verification | Accepted | 2026-09-09 |
| [0035](0035-direct-cast-before-full-remux.md)         | A soft language preference yields to a direct cast                       | Accepted | 2026-09-25 |
| [0036](0036-remux-feasibility-before-prepare.md)      | Remux feasibility is decided before the prepare; never a mute cast        | Accepted | 2026-10-01 |
| [0037](0037-explicit-interactivity-and-injected-prompts.md) | Interactivity is an explicit mode; the domain never opens fzf | Accepted | 2026-10-01 |
| [0038](0038-dead-source-key-by-what-failed.md) | A dead source is keyed by what failed, never by a shared display name | Accepted | 2026-10-01 |
| [0039](0039-disk-free-tier2-via-live-hls-ts.md) | Tier-2 audio conversion streams live HLS-TS instead of a complete file | Accepted | 2026-10-01 |
| [0040](0040-subtitles-off-the-start-path.md) | Subtitles stay off the cast start path: embedded first, alignment after start | Proposed | 2026-10-01 |
| [0041](0041-live-tier-before-another-dub.md) | With the live tier, a soft language preference no longer yields to another dub | Accepted | 2026-10-01 |
| [0042](0042-embedded-subtitles-on-the-live-path.md) | Embedded text subtitles first, delivered as an HLS rendition on the live path | Accepted (point 4 revised by 0043) | 2026-10-02 |
| [0043](0043-live-subtitle-timing-without-reload.md) | Live subtitle timing changes never reload the media | Accepted | 2026-10-02 |
| [0044](0044-live-started-is-observed-state.md) | Live `started` is an observed player state; dual-layer DV is not live-HLS-native | Accepted | 2026-10-02 |
| [0045](0045-philips-cast-volume-and-hevc-delivery.md) | Philips Chromecast: MASTER volume is 0–1; HEVC stutter is delivery, not codec | **Proposed** | 2026-10-05 |
| [0046](0046-board-catalogs-from-unlocked-manifests.md) | Board catalogs from unlocked addon manifests (not a marketplace) | Accepted | 2026-10-05 |
| [0047](0047-catalog-id-to-imdb.md) | Translate catalog ids (`tmdb:`, `kitsu:`, …) to IMDb before stream discovery | **Proposed** | 2026-10-05 |
| [0048](0048-addon-catalog-genre-skip.md) | Genre and skip extras for unlocked addon catalogs | **Proposed** | 2026-10-05 |

New ADR: copy [`0000-template.md`](0000-template.md), take the next number, add a row above,
and update [docs/roadmap.md](../roadmap.md) if Status is Proposed.
