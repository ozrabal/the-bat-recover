"""Synthetic fixtures only; no user email or backup data."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote
import zlib

from the_bat_recover import RecoveryError, recover

SCRIPT = Path(__file__).resolve().parents[1]/'the_bat_recover.py'


def field(key, value):
    data = value.encode('cp1250')
    return struct.pack('<II', key, len(data)) + data


def compressed(data):
    payload = zlib.compress(data)
    return struct.pack('<III', len(data), len(payload), 0) + payload


def mailbox(messages):
    data = bytearray(b'\x20\x06\x79\x19'+struct.pack('<I', 3080)+bytes(3072))
    for raw in messages:
        header = bytearray(48)
        header[:4] = b'!\x09\x70\x19'
        struct.pack_into('<I', header, 4, 48)
        struct.pack_into('<I', header, 36, len(raw))
        data.extend(header+raw)
    return bytes(data)


def backup(folders, split=False):
    data = b'\x06\x05\x60\x19' + bytes(8) + compressed(b'initial metadata')
    for index, (account, folder, messages) in enumerate(folders):
        tbb = mailbox(messages)
        data += bytes(12) + field(1, folder) + field(11, account)
        data += field(5, f'bat{index}.TBB') + field(6, str(len(tbb)))
        chunks = [tbb[:3200], tbb[3200:]] if split else [tbb]
        data += b''.join(compressed(chunk) for chunk in chunks)
    return data


PLAIN = b'From: Sender <sender@example.test>\r\nTo: reader@example.test\r\nSubject: =?utf-8?b?WmHFvMOzxYLEhw==?=\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello <script>bad()</script>\r\nFrom a body line\r\n'
MULTIPART = b'''From: sender@example.test
Subject: <img src=x onerror=bad()>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=boundary

--boundary
Content-Type: text/html; charset=utf-8

<p>Hello browser</p><script>evil()</script><img src="https://example.test/pixel">
--boundary
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="../../note.bin"
Content-Transfer-Encoding: base64

AAEC
--boundary--
'''


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for key, value in attrs if key == 'href')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'sample backup.tbk'
        self.output = self.root/'output'

    def write_backup(self, folders=None, split=False):
        self.source.write_bytes(backup(folders or [('Test account', 'Inbox', [PLAIN, MULTIPART])], split))

    def test_round_trip_split_blocks_html_attachments_and_links(self):
        self.write_backup(split=True)
        before = self.source.read_bytes()
        report = recover(self.source, self.output)
        self.assertEqual((report['emails'], report['attachments']), (2, 1))
        self.assertEqual(report['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.source.read_bytes(), before)
        folder = self.output/'messages'/'Test account'/'Inbox'
        self.assertEqual((folder/'00001.eml').read_bytes(), PLAIN)
        self.assertEqual((folder/'00002.eml').read_bytes(), MULTIPART)
        self.assertIn('Zażółć', (folder/'00001.html').read_text())
        self.assertIn('&lt;script&gt;bad()', (folder/'00001.html').read_text())
        rendered = (folder/'00002.html').read_text()
        self.assertIn('Hello browser', rendered)
        self.assertNotIn('evil()', rendered)
        self.assertNotIn('<img', rendered)
        self.assertEqual(next((folder/'00002_attachments').iterdir()).read_bytes(), b'\x00\x01\x02')
        self.assertIn(b'\n>From a body line', next(self.output.rglob('*.mbox')).read_bytes())
        for page in self.output.rglob('*.html'):
            parser = LinkParser()
            parser.feed(page.read_text())
            for link in parser.links:
                self.assertTrue((page.parent/unquote(link)).is_file(), link)
        self.assertIn('input', (self.output/'index.html').read_text())

    def test_names_cannot_traverse_or_overwrite_on_collision(self):
        self.write_backup([
            ('../Account', '../A/B', [PLAIN]),
            ('../Account', '../A\\B', [MULTIPART]),
            ('../Account', '../A/B', [PLAIN]),
            ('../account', '../A/B', [PLAIN]),
        ])
        report = recover(self.source, self.output)
        rows = json.loads((self.output/'message-index.json').read_text())
        self.assertEqual(report['emails'], 4)
        self.assertEqual(len({row['path'].casefold() for row in rows}), 4)
        self.assertEqual(len(list(self.output.rglob('*.eml'))), 4)
        self.assertEqual(len(list(self.output.rglob('*.mbox'))), 4)
        for row in rows:
            self.assertTrue((self.output/row['path']).resolve().is_relative_to(self.output.resolve()))

    def test_existing_output_is_preserved(self):
        self.write_backup()
        self.output.mkdir()
        marker = self.output/'keep.txt'
        marker.write_text('keep')
        with self.assertRaisesRegex(RecoveryError, 'already exists'):
            recover(self.source, self.output)
        self.assertEqual(marker.read_text(), 'keep')

    def test_invalid_and_truncated_backups_leave_no_output(self):
        self.write_backup(split=True)
        valid = self.source.read_bytes()
        for bad in (b'not a TBK', valid[:30], valid[:-20]):
            with self.subTest(length=len(bad)):
                self.source.write_bytes(bad)
                with self.assertRaises(RecoveryError):
                    recover(self.source, self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.root.glob('.bat-recover-*')), [])

    def test_invalid_message_length_fails_without_partial_output(self):
        broken = bytearray(mailbox([PLAIN]))
        struct.pack_into('<I', broken, 3080+36, 0xFFFFFFFF)
        self.source.write_bytes(
            b'\x06\x05\x60\x19'+bytes(8)+compressed(b'metadata')+bytes(12)
            +field(1, 'Inbox')+field(11, 'Account')+field(5, 'test.TBB')
            +field(6, str(len(broken)))+compressed(broken))
        with self.assertRaisesRegex(RecoveryError, 'invalid message length'):
            recover(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_entire_mailbox_is_detected(self):
        self.write_backup([('Account', 'Inbox', [PLAIN]), ('Account', 'Sent', [PLAIN])])
        data = self.source.read_bytes()
        # Remove the last stream, leaving its declared mailbox metadata present.
        payload = zlib.compress(mailbox([PLAIN]))
        self.source.write_bytes(data[:-len(payload)])
        with self.assertRaisesRegex(RecoveryError, 'declared mailboxes'):
            recover(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_empty_mailbox_is_supported(self):
        self.write_backup([('Account', 'Empty', [])])
        report = recover(self.source, self.output)
        self.assertEqual(report['emails'], 0)
        self.assertTrue((self.output/'index.html').exists())

    def test_cli_positional_named_source_and_interactive_prompt(self):
        self.write_backup()
        variants = [
            ([str(self.source)], None),
            (['--source', str(self.source)], None),
            ([], str(self.source)+'\n'),
        ]
        for i, (args, stdin) in enumerate(variants):
            out = self.root/f'cli-{i}'
            result = subprocess.run([sys.executable, str(SCRIPT), *args, '-o', str(out)],
                                    input=stdin, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Recovered 2 emails', result.stdout)
            self.assertTrue((out/'index.html').is_file())

    def test_cli_default_output_and_error_exit_codes(self):
        self.write_backup()
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.source)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root/'recovered'/'sample backup'/'index.html').is_file())
        for args, expected in [(['missing.tbk'], 1), (['--unknown'], 2),
                               ([str(self.source), '--source', str(self.source)], 2)]:
            result = subprocess.run([sys.executable, str(SCRIPT), *args],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected)
            self.assertNotIn('Traceback', result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPT)], input='', text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
