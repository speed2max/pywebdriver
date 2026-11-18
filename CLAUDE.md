# pywebdriver: Speed2Max fork

This is Speed2Max's fork of [pywebdriver](https://github.com/pywebdriver/pywebdriver),
the hardware proxy between the Odoo POS and the till's devices (ESC/POS ticket printer,
cash drawer, display). Upstream is **archived** since v3.0.22 (June 2025); this fork
carries our fixes so that a till can be reinstalled without losing them.

First user: the Clermont till (`pdv-tactile`, Epson TM-T20II on USB), see Odoo master
task #490.

## Branches

| Branch                  | Role                                                                                            | Who writes there                                    |
| ----------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `master`                | Exact copy of upstream `master`. Base to take upstream releases if the repo is ever unarchived. | Nobody. Only a fast-forward from `upstream/master`. |
| `dev`                   | Our main branch (the GitHub default). Started from `v3.0.22`.                                   | Only through merged PRs.                            |
| `<type>/<task>-<topic>` | One change, branched from `dev`, e.g. `fix/490-usb-lock`, `docs/claude-md`.                     | The author.                                         |

## Rules

- **Never commit to `master`.** Never commit to `dev` directly: every change goes
  through a PR into `dev`.
- **Branch from `dev`, merge back into `dev`.** One topic per branch and per commit, so
  a fix can later be offered upstream on its own.
- **Tag every merge into `dev`:** `s2m-<upstream version>-<N>`, e.g. `s2m-3.0.22-1`,
  then `s2m-3.0.22-2`. `N` goes up by one at each merge; it restarts at 1 when the
  upstream base changes. Tag the PR's own merge commit, annotated, with the PR number
  and a one-line summary. Check first that `dev`'s head is that PR's merge commit:
  someone else may have merged in between, and a tag cannot be fixed afterwards.
  ```bash
  C=$(gh pr view <PR> --json state,mergeCommit --jq 'select(.state=="MERGED").mergeCommit.oid')
  git switch dev && git pull --ff-only
  git log --oneline s2m-3.0.22-<N-1>..dev   # only this PR's commit, nothing else
  git tag -a s2m-3.0.22-<N> -m "#<PR>: summary" "$C"
  git push origin s2m-3.0.22-<N>
  ```
- **Tags never move and are never deleted.** A till runs a tag; it must always mean the
  same code. Known slip: `s2m-3.0.22-1` is #1's merge commit, but its message reads "#2:
  ci…".
- **Tills install from a tag, never from a branch.**
- **No rebase, no force-push on `dev` or `master`.** Their history is shared and tagged.
  Rebasing your own change branch before its PR is fine.
- **Never push to `upstream`.** It is archived; it is only fetched.
- **Commit messages:** `type(scope): summary`, then a body explaining why, and the Odoo
  task (`Task: #490`). Types: `fix`, `feat`, `test`, `docs`, `chore`, `ci`.

## Merging into `dev` (GitHub ruleset "dev: PR with Dev team review")

- **Squash merge only.** One commit per PR on `dev`, titled after the PR with `(#N)`;
  give it the PR's commit message as body.
- **Required checks:** `pre-commit` and `tests` (workflow `S2M checks`) must pass.
  Prettier runs in pre-commit and rewraps Markdown at 88 columns: run it before pushing
  a `.md` change.
- **Only Speed2Max's Dev team merges.** `CODEOWNERS` makes the Dev team the reviewer of
  every change. The Dev team may bypass the review, **only when merging a PR**
  (`gh pr merge <PR> --squash --admin`); a direct push to `dev` is refused to everyone.
- **Outside contributors** may open PRs. Their CI only runs once a member approves it,
  and a Dev member reviews and merges them.
- `master` can only be updated by the Dev team and org owners; `s2m-*` tags can only be
  created by them and can never be moved or deleted, by anyone.

## Licence (AGPL-3.0)

The fork must stay public. Keep `LICENSE` and the existing copyright headers. Each file
we change carries a notice of the change: a comment naming the task, e.g.
`# Speed2Max #490: <why>`, next to the changed code. The dated commits complete it.

## Upstream releases (if the repo is ever unarchived)

```bash
git fetch upstream --tags
git switch master && git merge --ff-only upstream/master && git push origin master
git switch -c chore/upstream-vX.Y.Z dev && git merge vX.Y.Z    # then PR into dev
```

Merge, do not rebase. After that merge, tags restart from `s2m-X.Y.Z-1`.

Squashing that PR would drop upstream's history from `dev` and make the next upstream
merge conflict. The `dev` ruleset only allows squash: that PR needs merge commits
allowed for its merge, which is the Dev team's decision at the time.

## Develop and test

- Local install and run: `DEVELOP.md`.
- Code style: `pre-commit run --all-files` (black, isort, flake8, pylint, prettier).
  Upstream CI (`.github/workflows/main.yml`) only runs on `master` for now.
- The ESC/POS layer comes from `py-xml-escpos` (akretion, branch `py3`), on top of
  `python-escpos`. The Clermont till runs python-escpos **3.0a8**. Its `Usb.open()`
  resets the USB device on every open: that is why our ESC/POS driver keeps the USB
  connection open between operations (#490). Check this behaviour before changing that
  version.
- A fix ships with a test that reproduces the failure on a simulated printer, fails on
  the old code and passes on the new one.

## Before saying a change works

1. Its tests pass, and pre-commit is clean.
2. For a printer change, on a real till: print a ticket; unplug the printer's USB, try a
   ticket (fails), plug it back, print again (comes out without restarting anything);
   `journalctl -u pywebdriver` and `journalctl -k` show no `Errno 19` and no repeated
   `reset full-speed USB device`.

The till's install outside this repo (CUPS, power agent, `pywebdriver.service`) is not
kept here.
