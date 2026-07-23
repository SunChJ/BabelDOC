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
would make valid PDFs unreadable. The narrow
`codeql[py/weak-sensitive-data-hashing]` comments on those mandated operations
record that reviewed exception without disabling the query for other code.

Each suppression is an otherwise-empty comment line immediately before the
mandated hash operation, which is the source-level form recognized by CodeQL.
Control characters removed by the XML converter are expressed with explicit
raw-string hexadecimal ranges so scanners and reviewers see the intended XML
1.0 character set unambiguously.
