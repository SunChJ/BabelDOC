# Security policy

Please report vulnerabilities privately through this repository's GitHub
Security Advisory form. Do not include secrets or confidential documents in a
public issue.

Gloss downstream security fixes are made on the latest supported downstream
release. An issue inherited from BabelDOC may also be reported privately to the
upstream maintainers.

## Code-scanning compatibility annotations

The vendored `babeldoc/pdfminer` implementation must decode encrypted PDF
files according to ISO 32000. Legacy revisions require MD5, while revisions 5
and 6 prescribe exact SHA-2 transforms. Those calls are format parsers, not
password-storage functions, and replacing them with Argon2, bcrypt, or PBKDF2
would make valid PDFs unreadable. Narrow, query-specific suppression annotations
on those mandated operations record that reviewed exception without disabling
the query for other code.

Legacy MD5 calls go through a dedicated constructor with Python's
`usedforsecurity=False` flag. The remaining SHA-2 suppressions use CodeQL's
otherwise-empty preceding-line form and apply only to the exact mandated
operations.
Control characters removed by the XML converter are expressed with explicit
raw-string hexadecimal ranges so scanners and reviewers see the intended XML
1.0 character set unambiguously.
