# Runbook — rotating `USHER_SECRET_KEY`

**Audience:** the operator of a running Usher deployment.
**Written from a drill run on 2026-08-26**, not from the design: every command
below was executed and every block quoted is real output. The drill ran against
a scratch `pgvector/pgvector:pg17` created and removed by the run — never
against the deployment's own database, because re-encrypting a live credential
under a throwaway key and back is a write to production state for no benefit.
Keys, refs and passwords in the examples are drill-synthetic.

Companion: [`restore.md`](restore.md), whose §0 uses this command from the other
direction — restoring an artifact taken under a *different* key.

---

## 0. The order, which is the whole of this document

```
1. generate the new key            (and keep the old one until step 4)
2. run `usher rotate-secret`        WHILE USHER_SECRET_KEY IS STILL THE OLD KEY
3. only then change .env / your secret store
4. restart, then take a fresh backup and discard the old one
```

🔴 **An operator who changes `.env` first has a deployment that cannot read its
own credentials and a rotation command that cannot decrypt anything.** That is
the failure this order exists to prevent, and it is the one mistake that looks
like data loss when it is not.

The mechanism: `usher rotate-secret` needs **both** keys at once, and it takes
the old one from `USHER_SECRET_KEY` — the same setting your deployment runs on.
So once `.env` holds the new key, the command builds *the new cipher on both
sides* and every row still on the previous key opens under neither.

**Measured**, three rows, `.env` changed first:

```
rotated     0
already     0
refused     3
  refused: drill-alpha
  refused: drill-bravo
  refused: drill-charlie
usher rotate-secret: 3 credentials could not be decrypted by either key and must
be re-entered -- re-register those sources with `POST /admin/sources` once the
new key is in place
```

⚠️ **That advice is wrong in this state and you must not follow it.** Those
three credentials are intact. Nothing was written — all three ciphertexts were
byte-identical afterwards. The command cannot tell "the rows are corrupt" from
"you gave me the wrong old key", because both are *"neither cipher opens this"*.

**So read the count, not the sentence:**

| what you see | what it almost always means | what to do |
|---|---|---|
| `refused` == **every** row | the old key you supplied is not the key the rows are on — you changed `.env` first | put `USHER_SECRET_KEY` back to the **old** key and re-run. Nothing was lost. |
| `refused` == **some** rows | those particular rows really are unreadable | the rest rotated; re-enter only the named ones |

Putting the old key back and re-running is the recovery, measured on the same
three rows, same command, only the order corrected:

```
rotated     3
already     0
refused     0
```

---

## 1. Generate the new key — exported, never in `.env`

```fish
set -x USHER_NEW_SECRET_KEY (openssl rand -hex 32)
```

**Export it; do not write it into `.env`.** Two reasons, both measured:

- `Settings` uses `extra="forbid"`, so a `USHER_NEW_SECRET_KEY=` line in `.env`
  makes **every** entry point fail — and a pydantic `ValidationError` renders
  `input_value=`, so the leftover line leaks the new key into the traceback of
  anything that reads settings.
- An exported variable is invisible to `Settings` (its env source reads only the
  fields it declares), which is exactly what you want.

⚠️ **Never put the key on the command line.** `--new-key-env` takes the *name* of
a variable, because a key in `argv` is in your shell history and in `ps` output
for every user on the box. Typing `--new-key <the key>` is refused without the
value being echoed; so is passing the key to `--new-key-env` itself.

---

## 2. Rotate, with `USHER_SECRET_KEY` still the old key

```fish
uv run usher rotate-secret --new-key-env USHER_NEW_SECRET_KEY
```

```
rotated     3
already     0
refused     0
3 stored credentials considered; outstanding playback tickets are invalidated by
any key change and clients recover by asking /play again
set USHER_SECRET_KEY to $USHER_NEW_SECRET_KEY's value and restart, or the next
start cannot read what this run just wrote
```

Exit 0. **This is one table** — `source_credentials`, one row per configured
source — so on most deployments it is a sub-second command. Nothing is
restarted, and the rows are readable under the new key immediately.

### If it is interrupted, re-run it — that is the recovery

It **commits per row**, deliberately (`usher restore` does the opposite and says
why). An interruption therefore leaves a *mixed* table rather than stranding
every credential, and there is no ledger, no resume flag and no cleanup step:
each row is tried with the **new** cipher first, so a second run finishes what a
first one started and a third is a no-op.

Drilled by killing the command on the second of three rows. It exits non-zero
with one line and no traceback:

```
usher rotate-secret: DBAPIError: … <class 'asyncpg.exceptions.RaiseError'>: …
(the stack is one flag away: `usher --traceback rotate-secret`)
```

The table it leaves — row 1 on the new key, rows 2 and 3 still on the old one,
every payload intact:

```
ref              opens under
drill-alpha      NEW
drill-bravo      OLD
drill-charlie    OLD
```

Running **the same command again**, with nothing changed:

```
rotated     2
already     1
refused     0
```

The row a previous run had already moved is *skipped*, not re-encrypted — its
ciphertext was byte-identical before and after the second run.

⚠️ **A half-rotated deployment is diagnosable, not silent.** A source whose row
is on the other key reports on `GET /admin/sources/{id}/status` as unreachable
and unauthenticated with a *re-enter your credentials* detail. If you see that
on some sources and not others, you are looking at an interrupted rotation — run
the command again before re-entering anything.

### If a row is refused

```
rotated     2
already     0
refused     1
  refused: drill-bravo
```

Exit 1, and **the other rows still rotated** — a rotation that aborted on one
bad row would leave you worse off than you started. The refused row is left
exactly as it was (measured byte-identical), because writing onto it would
destroy the one copy a recovered old key could still have read.

If it is refused because *every* row is, see §0 — you have the wrong old key. If
it is genuinely just this row, re-register that source with
`POST /admin/sources` once the new key is in place.

---

## 3. Only now change `.env`, and restart

Set `USHER_SECRET_KEY` to the value you rotated **to**, then restart the
deployment. The command's own last line says the same thing:

```
set USHER_SECRET_KEY to $USHER_NEW_SECRET_KEY's value and restart, or the next
start cannot read what this run just wrote
```

---

## 4. Two things rotation does not cover

### Outstanding playback tickets are invalidated, not migrated

`USHER_SECRET_KEY` has exactly **two** derivations over it, and they are
different subkeys of the same key — verified in the drill: a ticket cipher
handed a credential token answers `InvalidToken`.

| derivation | protects | persisted? |
|---|---|---|
| `build_cipher` (`usher.source-credentials.v1`) | `source_credentials.ciphertext` | **yes** — this is what rotates |
| `build_ticket_cipher` (`usher.playback-ticket.v1`) | a playback target URL inside a ticket | **never** |

So there is nothing to migrate. Every outstanding ticket stops working the
moment the new key is in force; a client meets that as `404 ticket_invalid` and
answers by asking `/play` again. **A ticket lives 300 seconds**
(`api.routers.playback.TICKET_TTL_SECONDS`, re-read 2026-08-26), so the window is
one interval of that at worst. Nothing to do — but if you are watching a
dashboard for the minute after a restart, that is what the blip is.

Keyset cursors are outside this entirely: `Settings.secret_key` is deliberately
not what signs one.

### 🔴 Backup artifacts do not rotate, and an old one becomes unrestorable

`usher backup` carries `source_credentials` as **the ciphertext it is stored
as** — it never calls `build_cipher` and holds no key. So an artifact taken
before a rotation holds old-key ciphertext forever, and restoring it into the
deployment you just rotated restores credentials nothing can decrypt.

Measured in the drill — a backup taken under the old key, then a rotation:

| ref | live before | in the artifact | live after |
|---|---|---|---|
| drill-alpha | `0f4f9d11e5ade82b` | `0f4f9d11e5ade82b` | `20e68bbcc0fc4482` |
| drill-bravo | `0e7949840e9c10ca` | `0e7949840e9c10ca` | `e79e8ebd3ccd73ba` |
| drill-charlie | `17faa9e117c10c7e` | `17faa9e117c10c7e` | `c6a3453da6a064d6` |

The artifact is byte-identical to the pre-rotation rows and **does not move when
the database does**.

**So the sequence is: rotate → take a fresh backup → discard the old one.**

```fish
uv run usher backup --output /path/to/usher-backup-post-rotation.jsonl.gz
```

✅ **If you have already restored an old artifact, you are not stuck** — provided
you still hold the key it was taken under. Restore it, then rotate *from* that
key *to* the one this deployment runs, which is [`restore.md`](restore.md) §0's
recipe. If you no longer have the old key, there is no path from that ciphertext
to plaintext and every source has to be re-entered.

---

## 5. 🔴 Before you type anything: `.env` points at your real database

Every checkout of this project carries a `.env` whose `USHER_DATABASE_URL` names
the deployment's own database, and `alembic/env.py` reads `get_settings()`. A
command typed in a worktree targets production-shared state **by default**, with
no flag and no prompt — that has taken this deployment down for ~3.5 hours once
(`.claude/rules/db-and-sql.md`, 2026-08-19).

This matters here because rotation is a **write** to the one table nothing else
can reconstruct. If you are rehearsing rather than rotating, point at a scratch
database and prove where you are pointed *before* you run anything:

```fish
set -x USHER_DATABASE_URL "postgresql+asyncpg://usher:usher@127.0.0.1:<scratch port>/usher"
uv run python -c "
from sqlalchemy.engine import make_url
from usher.config import get_settings
print(make_url(get_settings().database_url.get_secret_value()).render_as_string())
"
```

Read the host and port off that output, not off the variable you just set — the
variable being set is not the same claim as it having won. In the drill the same
check with the override removed resolved to the live host on port 5432, which is
how you know the check is doing something.

---

## 6. What this drill did not establish

Stated because this project has been wrong about the boundary of a run before.

- **One process.** Every arm was a single `usher rotate-secret`. Nothing here
  says what two concurrent rotations, or a rotation racing an admin
  `POST /admin/sources`, would do.
- **Three rows.** The deployment this project runs has **one**
  (`source_credentials`, measured 2026-08-25). Nothing here is a statement about
  a deployment with hundreds of sources, though the command's cost is linear and
  the table is one row per source.
- **No real credential was decrypted.** The drill's canaries are synthetic and
  the live database was read with `count(*)` and `md5()` only.
- ⚠️ **A database error during rotation prints the row's ciphertext.**
  SQLAlchemy renders a failing statement's bound parameters, and for this command
  one of them is the credential blob. It is ciphertext, not plaintext — opening
  it still needs the key — but treat the output of a *failed* rotation as
  sensitive and do not paste it into an issue. Known, not fixed.
