# Security policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting** — the *"Report a
vulnerability"* button on this repository's
[Security tab](https://github.com/anirudhlath/usher/security). It opens a
private advisory thread visible only to you and the maintainer.

It is enabled, so if that button is missing, something has changed and a
public issue asking about it is the right next step. No email address is
published, deliberately: a mailto in this file with nothing behind it is worse
than no channel at all.

**This is a single-maintainer project.** There is no rota and no response-time
commitment.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

**Only the most recent release is supported, and there are no backports.**
Because Usher is `0.x`
([README's Versioning section](README.md#versioning) says why), a fix may
arrive in a minor version rather than a patch — upgrade to the newest release
rather than waiting for a backport to an older line.

That is a promise one person keeps by doing nothing unusual, which is why it is
the promise made.

## Known and accepted — please do not report these as findings

Each is documented, deliberate, and already stated in the project's own
records. Reporting one costs you time and tells the maintainer nothing new.

- **No route requires authentication, including every `/admin` route.** There
  is no auth module and no `current_user` anywhere in the tree; it is an
  unbuilt feature, not a deferral with a date. Usher is designed to sit behind
  something that authenticates, on a network you control. See the README's
  Security section.
- **A playback ticket redeems to a URL carrying the source's session token.**
  Neither a `<video>` element nor a deep link can send Emby a header, so the
  token rides in the URL. The short-lived ticket keeps that URL out of what a
  client stores or renders, but the player that follows the ticket's `302`
  holds it. That URL is a secret: Usher never logs it and never renders it.
- **`USHER_SECRET_KEY` is the whole of the credential encryption.** Rotating
  it invalidates every stored source credential until the next write, and
  every outstanding playback ticket at once — which is the coarse revocation
  that does exist. `usher rotate-secret` makes that a procedure rather than an
  outage; the steps are in
  [`docs/runbooks/rotation.md`](docs/runbooks/rotation.md).

**What is worth reporting:** anything that lets a caller reach data or an
action the posture above does not already grant them — a credential or a
playback URL reaching a log, an error body or the console; a way past
`USHER_SECRET_KEY`; a path that serves one household's data to another.
