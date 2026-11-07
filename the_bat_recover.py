#!/usr/bin/env python3
"""Read this legacy TBK's zlib blocks and length-delimited TBB messages.

No original archive data is modified. Uses Python's standard library only.
"""
import argparse
import sys
import tempfile
import webbrowser
import email.policy
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import struct
import zlib
from email.parser import BytesParser
from urllib.parse import quote

TBK_MAGIC = b'\x06\x05\x60\x19'
MAX_BLOCK_BYTES = 100_000_000
MAX_RECOVERED_BYTES = 1_073_741_824


class RecoveryError(ValueError):
    """An unsupported, incomplete, or invalid archive."""


def require(condition, message):
    if not condition:
        raise RecoveryError(message)

def u32(data, pos):
    return struct.unpack_from('<I', data, pos)[0]

def fields(data, key):
    marker = struct.pack('<I', key)
    values = []
    for match in re.finditer(re.escape(marker), data):
        pos = match.start() + 4
        if pos + 4 <= len(data):
            size = u32(data, pos)
            if 0 < size < 1024 and pos + 4 + size <= len(data):
                values.append((match.start(), data[pos+4:pos+4+size].decode('cp1250', errors='replace')))
    return values

def safe(value):
    name = re.sub(r'[^\w.@() -]+', '_', value).strip(' .')[:120] or 'unnamed'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        name = '_' + name
    return name


def unique_name(value, allocated):
    """Avoid traversal, case-insensitive collisions, and long path components."""
    base = name = safe(value)
    number = 1
    while name.casefold() in allocated:
        number += 1
        name = f'{base}_{number}'
    allocated.add(name.casefold())
    return name

class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'tr', 'li', 'hr'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def decoded(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload())
    for charset in (part.get_content_charset(), 'utf-8', 'cp1250', 'iso-8859-2'):
        if charset:
            try:
                return payload.decode(charset)
            except (UnicodeError, LookupError):
                pass
    return payload.decode('utf-8', errors='replace')

CSS = 'body{font:15px system-ui;margin:30px auto;max-width:1200px;padding:0 20px;color:#202b39;background:#f7f9fc}a{color:#1559a6}table{width:100%;border-collapse:collapse;background:white}td,th{text-align:left;padding:10px;border-bottom:1px solid #e1e5eb;vertical-align:top}th{background:#e8eef6}input{padding:12px;width:95%;margin:15px 0;font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px;border:1px solid #dde3eb;font:15px/1.55 system-ui}h1{font-size:26px}dt{font-weight:bold}dd{margin:0 0 12px;overflow-wrap:anywhere}.muted{color:#586779}'

def page(title, body):
    return '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'"><title>'+html.escape(title)+'</title><style>'+CSS+'</style></head><body>'+body+'</body></html>'

def _extract(source, root):
    archive = source.read_bytes()
    require(len(archive) >= 24 and archive[:4] == TBK_MAGIC,
            'Unsupported archive: expected the legacy The Bat! TBK signature. '
            'Encrypted or newer backup variants are not supported.')
    groups = []
    pos = last = 0
    blocks = 0
    total_bytes = 0
    while pos < len(archive) - 2:
        if pos >= 12 and archive[pos] == 120 and (archive[pos]*256 + archive[pos+1]) % 31 == 0:
            try:
                dec = zlib.decompressobj()
                data = dec.decompress(archive[pos:], MAX_BLOCK_BYTES + 1)
                require(len(data) <= MAX_BLOCK_BYTES, f'Block at byte {pos} exceeds the 100 MB limit.')
                if dec.eof:
                    length = len(archive)-pos-len(dec.unused_data)
                    require(u32(archive, pos-12) == len(data), f'Block size mismatch at byte {pos}.')
                    require(u32(archive, pos-8) == length, f'Compressed size mismatch at byte {pos}.')
                    total_bytes += len(data)
                    require(total_bytes <= MAX_RECOVERED_BYTES, 'Archive exceeds the 1 GiB decompressed-data limit.')
                    gap = archive[last:pos]
                    if len(gap) > 24 or not groups:
                        groups.append({'gap':gap, 'data':bytearray(), 'offset':pos})
                    groups[-1]['data'].extend(data)
                    pos += length
                    last = pos
                    blocks += 1
                    continue
            except zlib.error:
                pass
        pos += 1

    require(groups and groups[0]['offset'] == 24,
            'Cannot read the initial backup block; the archive may be damaged or unsupported.')
    rows = []
    folders = []
    account = folder = 'Unknown'
    attachments = 0
    account_paths = {}
    used_accounts = set()
    used_folders = {}
    for group in groups:
        gap, data = group['gap'], bytes(group['data'])
        # Folder metadata consists of type/name (key 1), then account (key 11).
        for start, name in fields(gap, 1):
            end = start + 8 + u32(gap, start+4)
            if end+8 <= len(gap) and u32(gap,end) == 11:
                size = u32(gap,end+4)
                require(end+8+size <= len(gap), 'Truncated account metadata.')
                acct = gap[end+8:end+8+size].decode('cp1250', errors='replace')
                if name not in ('ACCOUNT','FOLDERS','ATTACHMENTS'):
                    account, folder = acct, name
        names = fields(gap, 5)
        filename = names[-1][1] if names else ''
        if not filename.upper().endswith('.TBB'):
            if data.startswith(b'dn:'):
                (root/'address-book.ldif').write_bytes(data)
            continue
        sizes = fields(gap, 6)
        require(sizes and sizes[-1][1].isdigit(), f'{filename}: missing mailbox size metadata.')
        require(len(data) == int(sizes[-1][1]), f'{filename}: mailbox size mismatch; a compressed block may be missing or damaged.')
        require(len(data) >= 8 and data[:4] == b'\x20\x06\x79\x19', f'{filename}: unsupported mailbox signature.')
        if account not in account_paths:
            account_paths[account] = unique_name(account, used_accounts)
            used_folders[account] = set()
        account_path = account_paths[account]
        folder_path = unique_name(folder, used_folders[account])
        dest = root/'messages'/account_path/folder_path
        dest.mkdir(parents=True, exist_ok=True)
        raw_dir = root/'original-mailboxes'/account_path/folder_path
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir/safe(filename)).write_bytes(data)
        pos = u32(data,4)
        require(8 <= pos <= len(data), f'{filename}: invalid mailbox header length.')
        count = 0
        mbox = bytearray()
        while pos < len(data):
            require(pos+40 <= len(data) and data[pos:pos+4] == b'!\x09\x70\x19', f'{filename}: invalid or truncated message at byte {pos}.')
            header = u32(data,pos+4)
            length = u32(data,pos+36)
            require(header >= 40 and pos+header+length <= len(data), f'{filename}: invalid message length at byte {pos}.')
            raw = data[pos+header:pos+header+length]
            pos += header+length
            count += 1
            num = f'{count:05}'
            (dest/(num+'.eml')).write_bytes(raw)
            msg = BytesParser(policy=email.policy.default).parsebytes(raw)
            subject = str(msg.get('Subject','(no subject)'))
            sender = str(msg.get('From',''))
            recipient = str(msg.get('To',''))
            date = str(msg.get('Date',''))
            plain, rich, links = [], [], []
            for part in msg.walk():
                if part.is_multipart():
                    continue
                fn = part.get_filename()
                if fn or part.get_content_disposition() == 'attachment':
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        attachment_dir = dest/(num+'_attachments')
                        attachment_dir.mkdir(exist_ok=True)
                        name = f'{len(links)+1:02}_'+safe(fn or 'attachment.bin')
                        (attachment_dir/name).write_bytes(payload)
                        links.append(((attachment_dir/name).relative_to(dest).as_posix(),fn or name))
                        attachments += 1
                elif part.get_content_type() == 'text/plain':
                    plain.append(decoded(part))
                elif part.get_content_type() == 'text/html':
                    parser = TextHTML()
                    parser.feed(decoded(part))
                    rich.append(''.join(parser.parts))
            body = '\n\n'.join(plain or rich) or '(No readable text body; open the original .eml file.)'
            eml_link = quote(num+'.eml')
            back = '../../../index.html'
            details = ''.join('<dt>'+label+'</dt><dd>'+html.escape(value)+'</dd>' for label,value in [('From',sender),('To',recipient),('Date',date),('Folder',account+' / '+folder)])
            attached = '<ul>'+''.join('<li><a href="'+quote(path)+'">'+html.escape(name)+'</a></li>' for path,name in links)+'</ul>' if links else ''
            content = '<a href="'+back+'">← All emails</a><h1>'+html.escape(subject)+'</h1><dl>'+details+'</dl><p><a href="'+eml_link+'">Open original email (.eml)</a></p>'+attached+'<pre>'+html.escape(body)+'</pre>'
            (dest/(num+'.html')).write_text(page(subject,content),encoding='utf-8')
            row = dict(account=account,folder=folder,subject=subject,sender=sender,to=recipient,date=date,path=(dest/(num+'.html')).relative_to(root).as_posix(),attachments=len(links))
            rows.append(row)
            # mboxrd escaping preserves lines beginning with From when imported.
            escaped = re.sub(br'(?m)^(>*From )',br'>\1',raw)
            mbox.extend(b'From recovered@localhost Thu Jan  1 00:00:00 1970\n'+escaped.rstrip(b'\r\n')+b'\n\n')
        require(pos == len(data), f'{filename}: incomplete mailbox extraction.')
        mbox_dest = root/'mbox'/account_path
        mbox_dest.mkdir(parents=True, exist_ok=True)
        (mbox_dest/(folder_path+'.mbox')).write_bytes(mbox)
        folders.append(dict(account=account,folder=folder,messages=count,source=filename,bytes=len(data)))

    require(folders, 'No supported TBB mailboxes found. The backup may be incomplete or unsupported.')
    declared_mailboxes = [name for _, name in fields(archive, 5) if name.upper().endswith('.TBB')]
    require(len(declared_mailboxes) == len(folders), 'Some declared mailboxes could not be recovered; the archive may contain damaged or unsupported blocks.')
    table_rows = ''.join('<tr>'+''.join('<td>'+html.escape(row[key])+'</td>' for key in ('account','folder','date','sender'))+'<td><a href="'+quote(row['path'])+'">'+html.escape(row['subject'])+'</a></td><td>'+str(row['attachments'])+'</td></tr>' for row in rows)
    script = '<script>const q=document.querySelector("input"),rows=[...document.querySelectorAll("tbody tr")];q.addEventListener("input",()=>{let n=0;for(const r of rows){r.hidden=!r.textContent.toLowerCase().includes(q.value.toLowerCase());if(!r.hidden)n++}document.querySelector("#count").textContent=n+" emails shown"})</script>'
    intro = f'<h1>The Bat! · Email archive</h1><p>{len(rows):,} recovered emails · {len(set(r["account"] for r in folders))} accounts</p><p class="muted">Click a subject to read an email. Message text is displayed locally; original formatting and attachments are preserved in the .eml files. Search matches the columns below.</p><input type="search" placeholder="Search subject, sender, date, account or folder" aria-label="Search emails"><p id="count">{len(rows)} emails shown</p>'
    (root/'index.html').write_text(page('The Bat! email archive',intro+'<table><thead><tr><th>Account</th><th>Folder</th><th>Date</th><th>From</th><th>Subject</th><th>Files</th></tr></thead><tbody>'+table_rows+'</tbody></table>'+script),encoding='utf-8')
    report = dict(source=str(source),sha256=hashlib.sha256(archive).hexdigest(),compressed_blocks=blocks,files_with_data=len(groups),emails=len(rows),attachments=attachments,folders=folders)
    (root/'recovery-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'message-index.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'README.txt').write_text(f'''THE BAT! EMAIL RECOVERY

Double-click index.html to browse {len(rows)} recovered emails in your browser.
Use the search field to filter the message list; click a subject to read it.

messages/ contains individual .eml originals, readable .html copies, and decoded attachments.
mbox/ contains mailboxes grouped by account and folder for importing into a mail client.
original-mailboxes/ preserves the decompressed TBB files.
address-book.ldif contains the recovered address book.
recovery-report.json records the archive SHA-256 and extraction counts.
Generated with the-bat-revover (the-bat-recover CLI).

All {blocks} recovered zlib streams passed their checksum and declared size checks.
Every extracted TBB file passed size checks and every message was read by its stored length,
ending exactly at the mailbox boundary. Empty folders have no exported messages.
This verifies extraction consistency, not whether every message from the original mail client
was included in the 2004 backup. The source TBK was read only and remains unchanged.

The browser copies show decoded text. HTML-only emails are converted to text.
The original MIME content, including formatting and attachments, is preserved in .eml files.
''',encoding='utf-8')
    return report


def recover(source, output):
    """Recover into a new directory; return a JSON-compatible report.

    Failed runs leave no partial output. The input and existing output paths
    are never overwritten. This parser supports the observed legacy TBK variant.
    """
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().absolute()
    if not source.is_file():
        raise RecoveryError(f'Source file does not exist or is not a regular file: {source}')
    if output.exists() or output.is_symlink():
        raise RecoveryError(f'Output already exists: {output}. Choose a new directory with --output.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bat-recover-', dir=output.parent) as staging:
        root = Path(staging)/'result'
        root.mkdir()
        report = _extract(source, root)
        require(not output.exists() and not output.is_symlink(), 'Output directory appeared during recovery; choose a different path.')
        root.rename(output)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Recover legacy The Bat! .tbk backups into a searchable local HTML email list, EML files, attachments and mbox mailboxes.',
        epilog='Without a source argument, you will be prompted. Output must be a new directory. No third-party dependencies.')
    parser.add_argument('archive', nargs='?', help='source .tbk archive')
    parser.add_argument('-s', '--source', help='source .tbk archive (alternative to the positional argument)')
    parser.add_argument('-o', '--output', type=Path, help='new output directory (default: ./recovered/<archive-stem>)')
    parser.add_argument('--open', action='store_true', help='open the generated email list in the default browser')
    args = parser.parse_args(argv)
    if args.archive and args.source:
        parser.error('Provide the source either positionally or with --source, not both.')
    try:
        source_text = args.source or args.archive
        if not source_text:
            source_text = input('Source .tbk file: ').strip()
        if not source_text:
            raise RecoveryError('A source .tbk file is required.')
        source = Path(source_text).expanduser()
        output = (args.output or Path.cwd()/'recovered'/safe(source.stem)).expanduser().absolute()
        report = recover(source, output)
    except (RecoveryError, OSError, EOFError) as exc:
        print(f'Error: {exc or "No source supplied. Pass a path as an argument."}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nRecovery cancelled.', file=sys.stderr)
        return 130
    index = output.absolute()/'index.html'
    print(f'Recovered {report["emails"]:,} emails and {report["attachments"]:,} attachments from {len({f["account"] for f in report["folders"]})} accounts.')
    print(f'Open email list: {index}')
    if args.open:
        try:
            opened = webbrowser.open(index.as_uri())
        except webbrowser.Error:
            opened = False
        if not opened:
            print('Browser could not be opened automatically. Open index.html manually.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
