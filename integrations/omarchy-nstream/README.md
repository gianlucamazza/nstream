# omarchy-nstream

A remote for [nstream](https://github.com/gianlucamazza/nstream) in the
[Omarchy](https://omarchy.org/) bar.

- **On the TV:** play/pause, back/forward 30 s, a seek bar, Cast volume, live subtitle
  timing (±0.5 s) and stop for the cast nstream started.
- **Continue watching:** your nstream history, one click to resume on the TV or in mpv on
  this computer. A finished episode continues with the next one.

The bar icon lights up while a cast is live. Everything else stays in nstream itself; the
**Open nstream** button opens its TUI.

## Requirements

- Omarchy with shell plugin support
- nstream newer than 1.43 (it needs `--json --history`), on `PATH` or in `~/.local/bin`
- `catt` for casting (`uv tool install catt`), `mpv` for local playback

## Install

From the nstream checkout (installs nstream too, and keeps both in step):

```sh
./install.sh
omarchy plugin enable gianlucamazza.nstream
```

Or on its own, from its published repository:

```sh
omarchy plugin add https://github.com/gianlucamazza/omarchy-nstream.git --enable
```

Toggle the panel from a keybinding with
`omarchy-shell gianlucamazza.nstream toggle`.

## Settings

| Key | Default | Meaning |
| --- | ------- | ------- |
| `historyLimit` | `8` | Continue-watching rows shown (1–30) |

The default target (TV or this computer) follows nstream's `prefer_cast`; the toggle in
the panel overrides it until the shell restarts.

## How it works

`Panel.qml` never runs nstream itself. `helper.py` (Python stdlib) validates every
argument, runs `nstream --json …` as an argv list (never through a shell) and forwards only
bounded descriptive fields; nstream never puts stream URLs or tokens in `--json` output.
All text is rendered as plain text. The helper adds `~/.local/bin` to the child
process environment too, so nstream can find tools installed with `uv tool`.

Cast controls use the active session's device; an explicit CLI `--device` still wins.
Receiver and command errors remain visible in the panel. Local playback runs in a separate
process: its notification acknowledges the request, and the helper waits for nstream's
final result (normally when mpv closes) to report failures. Cast controls stay available
while mpv runs. Closing or reloading the widget leaves the player running; a shell reload
ends observation of that local result.

Polling is cheap by design: `--history` is a local read, polled every 30 s (5 s while the
panel is open). The TV is only asked for its state (`--status`) while the panel is open
and a cast is live.

## Development

```sh
python3 -B -m unittest discover -s tests
omarchy plugin validate .
```

Saving a file under `~/.config/omarchy/plugins/gianlucamazza.nstream/` reloads the widget;
from the nstream checkout re-run `./install.sh` to copy changes over.

## Source

Developed in the nstream repository under `integrations/omarchy-nstream/` (so the widget and
the `--json` contract it relies on change together) and mirrored here for
`omarchy plugin add`. Open issues and pull requests on
[nstream](https://github.com/gianlucamazza/nstream).

## License

MIT
