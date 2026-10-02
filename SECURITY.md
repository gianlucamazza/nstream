# Security

## Secrets

Debrid tokens, addon manifests that embed keys, and stream URLs must never appear in
issues, logs, or pull requests. nstream stores the token only in
`$XDG_CONFIG_HOME/nstream/config.json` (`chmod 600`) and redacts it from logs.

If a token leaked (paste, screenshot, CI log):

1. Rotate it at the debrid provider immediately.
2. Open a [GitHub private vulnerability report](https://github.com/gianlucamazza/nstream/security/advisories/new)
   if the leak is in this repository or in a release artifact.

## Reports

Use GitHub private advisories for anything that could expose tokens, LAN addresses, or
unauthenticated access to a running remux HTTP server. Do not file a public issue for
those.

The maintainer: Gianluca Mazza <info@gianlucamazza.it>.
