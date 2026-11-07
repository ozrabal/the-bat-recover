# the-bat-revover

Recover old **The Bat! `.tbk` email backups** into a searchable HTML email list,
individual `.eml` messages, attachments, and importable `.mbox` mailboxes.
Everything runs locally using Python's standard library. The original archive
is read only.

The repository name is `the-bat-revover`; the installed command is
`the-bat-recover`.

## Quick start

Requires **Python 3.10 or newer**. No installation or dependencies are needed:

```sh
python3 the_bat_recover.py "/path/to/backup.tbk"
```

With no source argument, the script asks for the file path:

```sh
python3 the_bat_recover.py
# Source .tbk file: /path/to/backup.tbk
```

Type the path without surrounding quotes at the prompt. Use shell quotes around
paths containing spaces when passing command-line arguments.

You can also supply the source as a named option, choose a new output directory,
and ask to open the email list:

```sh
python3 the_bat_recover.py --source "/path/to/backup.tbk" \
  --output "/path/to/recovered-emails" --open
python3 the_bat_recover.py --help
```

Output defaults to `recovered/<archive-stem>/` under your current directory.
The output directory must **not already exist**. Choose another directory for
each run. Failed recovery removes its temporary output; successful recovery
prints the path to `index.html`. The `--open` option uses your system's default
browser; if automatic opening fails, open `index.html` manually.

On Windows, replace `python3` with `py -3` in these examples.

## Optional CLI installation

From this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
the-bat-recover --source "/path/to/backup.tbk" --open
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
Installing the package may download the setuptools build dependency; running
the standalone script needs no package downloads.

## Read the emails

Open the generated `index.html` in a browser. Filter the list by subject,
sender, date, account, or folder, then click a subject to read the message.
Each message page links to the original `.eml` and any decoded attachments.
The search filters the displayed list columns, not the email bodies.

```text
recovered/<archive-stem>/
  index.html                  Searchable email list
  messages/<account>/<folder>/
    00001.html                Readable message text
    00001.eml                 Original MIME message bytes
    00001_attachments/        Decoded attachment files, when present
  mbox/<account>/*.mbox        Mailboxes for importing into a mail client
  original-mailboxes/         Decompressed original TBB files
  address-book.ldif           Address book, if recognized
  message-index.json          Message headers and relative HTML paths
  recovery-report.json        Source SHA-256, counts, folder inventory
  README.txt                  Instructions for this recovered archive
```

Account and folder names remain in the message index. Filesystem names are
sanitized; colliding names receive numeric suffixes. Multiple mailboxes with
the same folder name are exported separately.

HTML-only emails are converted to text for browsing. Scripts, remote images,
and original HTML markup are not loaded in these browser copies. Original
formatting and MIME content remain in `.eml` files. Named non-multipart
attachments are also decoded into separate files. Attached messages and other
unusual multipart structures remain preserved in the `.eml` even when no
separate attachment file is created.

## Compatibility and limitations

This is a parser for the **observed legacy TBK format**, developed from a 2004
backup. That archive yielded 978 emails from 9 accounts and 163 decoded
attachments. This is not a guarantee of support for all The Bat! versions.
Encrypted/password-protected backups and other TBK variants are unsupported.
The application The Bat! is not required.

The parser recognizes the legacy signature, discovers zlib blocks, groups
continuation blocks, reads account/folder metadata, and splits TBB records by
their stored lengths. It checks recovered zlib checksums, block sizes, mailbox
sizes, message boundaries, and declared mailbox counts. It fails on detected
inconsistencies rather than presenting them as a successful recovery. It does
not implement the entire proprietary container format or prove that every
original item was included in the backup. Non-mail configuration files and
separately stored attachments outside MIME messages are not restored.

The input and decompressed groups are held in memory. Limits are 100 MB per
decompressed block and 1 GiB of total decompressed block data; actual peak
memory can be higher. This tool is intended for small legacy archives, not
multi-gigabyte backups. Cancellation and errors leave no published output
directory, although a force-killed process can leave `.bat-recover-*` temporary
directories beside the intended output.

Recovered files contain private correspondence. The default `recovered/`
directory and common email/backup extensions are gitignored. If you choose a
different output location inside this repository, exclude that directory
before committing.

## Python API

```python
from the_bat_recover import recover

report = recover("/path/to/backup.tbk", "/path/to/new-output-directory")
print(report["emails"])
```

`recover()` returns the same dictionary saved in `recovery-report.json`.
Unsupported or detected malformed archives raise `RecoveryError`; filesystem
problems raise `OSError`. CLI exit codes are `0` for success, `1` for recovery
or filesystem errors, `2` for invalid arguments, and `130` for cancellation.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests create synthetic mail backups in temporary directories. No private
archive, email, contact, or generated recovery output is included in this
repository. The parser's observed binary layout is documented in
[FORMAT.md](FORMAT.md).
