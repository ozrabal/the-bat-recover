# Observed legacy format

These notes describe the 2004 TBK variant used to develop this tool. They are
observations, not an official or complete The Bat! format specification.

## TBK container

- Initial magic: `06 05 60 19`.
- First zlib stream starts at byte 24.
- The 12 bytes immediately before each compressed stream contain three
  little-endian 32-bit values. The first two match the decompressed length
  and compressed length. The third value is not interpreted.
- Continuation streams have a 12-byte intervening block header. Larger gaps
  contain file/folder metadata and begin a new group of decompressed data.
- Metadata includes key/length/value fields, where key and byte length are
  little-endian 32-bit integers. Relevant keys are `1` (type or folder name),
  `5` (filename), `6` (decimal file size), and `11` (account name).
- A folder name field immediately followed by an account field associates
  subsequent mailbox files with that account/folder. Metadata text is decoded
  with Windows-1250, replacing undefined bytes for display.
- Other record headers, padding, indexes, configuration, and trailing metadata
  are not fully interpreted. Block discovery and metadata field discovery are
  heuristic. Declared TBB file counts are cross-checked to detect skipped
  mailboxes.

## TBB mailbox

All byte offsets below are relative to the start of their record.

| Offset | Meaning |
| --- | --- |
| 0 | Mailbox magic `20 06 79 19` |
| 4 | Little-endian 32-bit mailbox header length; observed value 3080 |

After the mailbox header, message records continue until the end of the file:

| Offset | Meaning |
| --- | --- |
| 0 | Message magic `21 09 70 19` |
| 4 | Little-endian 32-bit message header length; observed value 48 |
| 36 | Little-endian 32-bit raw email length |
| Header length | Raw RFC-style message headers and MIME body |

The next record starts at `record_start + header_length + email_length`.
Original EML exports preserve exactly these raw message bytes. HTML is decoded
from MIME text parts; mbox exports add envelope separators and escape `From `
body lines using mboxrd conventions.

Zlib integrity and observed length checks cannot establish whether an original
backup was complete, or validate unsupported container records. Keep the source
archive for future recovery attempts with other software.
