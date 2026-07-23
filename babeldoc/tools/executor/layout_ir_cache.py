from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pickle  # noqa: S403 - restricted to private, validated IL cache files
import stat
import tempfile
from pathlib import Path
from typing import Any

from babeldoc import __version__ as babeldoc_version
from babeldoc.format.pdf.document_il import il_version_1

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
CACHE_PROFILE = "gloss-layout-ir-v3"
MAX_CACHE_FILE_BYTES = 256 * 1024 * 1024
MAX_CACHE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_CACHE_ENTRIES = 8
_IL_MODULE_NAME = "babeldoc.format.pdf.document_il.il_version_1"


class _LimitedWriter:
    def __init__(self, handle, limit: int):
        self._handle = handle
        self._limit = limit
        self._written = 0

    def write(self, data: bytes) -> int:
        next_size = self._written + len(data)
        if next_size > self._limit:
            raise ValueError("layout IR cache entry exceeds its size limit")
        written = self._handle.write(data)
        self._written += written
        return written


class _RestrictedILUnpickler(pickle.Unpickler):
    _allowed_types = {
        name: value
        for name, value in vars(il_version_1).items()
        if isinstance(value, type)
        and dataclasses.is_dataclass(value)
        and value.__module__ == _IL_MODULE_NAME
    }

    def find_class(self, module: str, name: str):
        if module == _IL_MODULE_NAME and name in self._allowed_types:
            return self._allowed_types[name]
        raise pickle.UnpicklingError(f"forbidden layout cache type: {module}.{name}")


class LayoutIRCache:
    """Private, bounded cache for the pre-translation BabelDOC document IL.

    The cache belongs to one authenticated executor workroot. Cache keys are
    derived by the runtime from the stable input bytes and all relevant parser
    options; the client never supplies either a cache path or a cache key.
    """

    def __init__(self, root: Path, *, max_pages_per_part: int):
        self.root = root
        self.max_pages_per_part = max_pages_per_part
        self.status = "disabled"
        self._cache_path: Path | None = None
        self._input_path: Path | None = None
        self._input_identity: tuple[int, int, int, int, int] | None = None
        self._expected_page_count = 0
        self._expected_page_numbers: list[int] = []

    def load(self, config, mupdf_document) -> il_version_1.Document | None:
        if not self._is_eligible(config, mupdf_document):
            self.status = "ineligible"
            return None
        if not self._root_is_private():
            self.status = "unsafe_directory"
            return None

        try:
            self._prepare_key(config, mupdf_document)
            self._cleanup_temporary_files()
        except (OSError, ValueError):
            logger.exception("failed to prepare layout IR cache")
            self.status = "read_error"
            return None

        assert self._cache_path is not None
        try:
            payload = self._read_payload(self._cache_path)
        except FileNotFoundError:
            self.status = "miss"
            return None
        except Exception:
            logger.warning(
                "invalidating unreadable layout IR cache entry", exc_info=True
            )
            self._unlink_cache_path()
            self.status = "invalidated"
            return None

        try:
            document = self._validate_payload(payload)
        except (TypeError, ValueError):
            logger.warning(
                "invalidating malformed layout IR cache entry", exc_info=True
            )
            self._unlink_cache_path()
            self.status = "invalidated"
            return None

        shared_context = config.shared_context_cross_split_part
        shared_context.valid_char_count_total = payload["valid_character_count"]
        shared_context.total_valid_text_token_count = payload["valid_token_count"]
        try:
            os.utime(self._cache_path, None, follow_symlinks=False)
        except OSError:
            pass
        self.status = "hit"
        return document

    def store(self, document: il_version_1.Document, config) -> None:
        if self._cache_path is None or self.status not in {"miss", "invalidated"}:
            return
        if not self._input_is_unchanged():
            self.status = "input_changed"
            return

        descriptor: int | None = None
        temporary_path: Path | None = None
        try:
            page_numbers = self._validated_page_numbers(document)
            shared_context = config.shared_context_cross_split_part
            valid_character_count = _non_negative_int(
                getattr(shared_context, "valid_char_count_total", 0)
            )
            valid_token_count = _non_negative_int(
                getattr(shared_context, "total_valid_text_token_count", 0)
            )
            self._prune(
                maximum_bytes=MAX_CACHE_TOTAL_BYTES - MAX_CACHE_FILE_BYTES,
                maximum_entries=MAX_CACHE_ENTRIES - 1,
            )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".layout-ir-{os.getpid()}-",
                suffix=".tmp",
                dir=self.root,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                writer = _LimitedWriter(handle, MAX_CACHE_FILE_BYTES)
                pickle.dump(  # noqa: S301 - loaded only by _RestrictedILUnpickler
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "profile": CACHE_PROFILE,
                        "key": self._cache_path.stem,
                        "page_count": self._expected_page_count,
                        "page_numbers": page_numbers,
                        "valid_character_count": valid_character_count,
                        "valid_token_count": valid_token_count,
                        "document": document,
                    },
                    writer,
                    protocol=5,
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self._cache_path)
            temporary_path = None
            self._cache_path.chmod(0o600)
            self._prune(
                maximum_bytes=MAX_CACHE_TOTAL_BYTES,
                maximum_entries=MAX_CACHE_ENTRIES,
                protected_path=self._cache_path,
            )
            self.status = "stored"
        except Exception:
            logger.warning("failed to store layout IR cache entry", exc_info=True)
            self.status = "write_error"
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _is_eligible(self, config, mupdf_document) -> bool:
        page_count = getattr(mupdf_document, "page_count", 0)
        return (
            isinstance(page_count, int)
            and not isinstance(page_count, bool)
            and 1 <= page_count <= self.max_pages_per_part
            and config.skip_scanned_detection
            and not config.debug
            and config.pages is None
            and not config.page_ranges
            and not config.only_parse_generate_pdf
            and config.table_model is None
        )

    def _root_is_private(self) -> bool:
        try:
            info = os.lstat(self.root)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0
        )

    def _prepare_key(self, config, mupdf_document) -> None:
        input_path = Path(config.input_file)
        input_digest, input_identity = _stable_file_fingerprint(input_path)
        page_count = int(mupdf_document.page_count)
        options: dict[str, Any] = {
            "debug": bool(config.debug),
            "enable_graphic_element_process": bool(
                config.enable_graphic_element_process
            ),
            "lang_in": str(config.lang_in).lower(),
            "lang_out": str(config.lang_out).lower(),
            "merge_alternating_line_numbers": bool(
                config.merge_alternating_line_numbers
            ),
            "ocr_workaround": bool(config.ocr_workaround),
            "primary_font_family": config.primary_font_family,
            "remove_non_formula_lines": bool(config.remove_non_formula_lines),
            "skip_curve_render": bool(config.skip_curve_render),
            "skip_form_render": bool(config.skip_form_render),
            "skip_formula_offset_calculation": bool(
                config.skip_formula_offset_calculation
            ),
        }
        identity = json.dumps(
            {
                "babeldoc_version": babeldoc_version,
                "input_sha256": input_digest,
                "options": options,
                "page_count": page_count,
                "profile": CACHE_PROFILE,
                "schema_version": CACHE_SCHEMA_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        cache_key = hashlib.sha256(identity).hexdigest()
        self._cache_path = self.root / f"{cache_key}.pickle"
        self._input_path = input_path
        self._input_identity = input_identity
        self._expected_page_count = page_count
        self._expected_page_numbers = list(range(page_count))

    def _read_payload(self, path: Path) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or not 0 < info.st_size <= MAX_CACHE_FILE_BYTES
            ):
                raise ValueError("layout IR cache file metadata is unsafe")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = _RestrictedILUnpickler(handle).load()  # noqa: S301
                if handle.read(1):
                    raise ValueError("layout IR cache contains trailing data")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(payload, dict):
            raise TypeError("layout IR cache payload must be an object")
        return payload

    def _validate_payload(self, payload: dict[str, Any]) -> il_version_1.Document:
        assert self._cache_path is not None
        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise ValueError("layout IR cache schema does not match")
        if payload.get("profile") != CACHE_PROFILE:
            raise ValueError("layout IR cache profile does not match")
        if payload.get("key") != self._cache_path.stem:
            raise ValueError("layout IR cache key does not match")
        if payload.get("page_count") != self._expected_page_count:
            raise ValueError("layout IR cache page count does not match")
        document = payload.get("document")
        page_numbers = self._validated_page_numbers(document)
        if payload.get("page_numbers") != page_numbers:
            raise ValueError("layout IR cache page numbers do not match")
        for field_name in ("valid_character_count", "valid_token_count"):
            value = payload.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"layout IR cache {field_name} is invalid")
        return document

    def _validated_page_numbers(
        self,
        document: object,
    ) -> list[int]:
        if not isinstance(document, il_version_1.Document):
            raise TypeError("layout IR cache document type is invalid")
        pages = document.page
        if not isinstance(pages, list) or len(pages) != self._expected_page_count:
            raise ValueError("layout IR cache document page count is invalid")
        page_numbers = [page.page_number for page in pages]
        if page_numbers != self._expected_page_numbers:
            raise ValueError("layout IR cache document page numbers are invalid")
        if document.total_pages not in (None, self._expected_page_count):
            raise ValueError("layout IR cache document total_pages is invalid")
        return page_numbers

    def _input_is_unchanged(self) -> bool:
        if self._input_path is None or self._input_identity is None:
            return False
        try:
            info = self._input_path.stat()
        except OSError:
            return False
        return _file_identity(info) == self._input_identity

    def _unlink_cache_path(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.unlink()
        except OSError:
            pass

    def _cache_entries(self) -> list[tuple[Path, os.stat_result]]:
        entries: list[tuple[Path, os.stat_result]] = []
        try:
            candidates = list(self.root.iterdir())
        except OSError:
            return entries
        for candidate in candidates:
            if (
                candidate.suffix != ".pickle"
                or len(candidate.stem) != 64
                or any(
                    character not in "0123456789abcdef" for character in candidate.stem
                )
            ):
                continue
            try:
                info = os.lstat(candidate)
            except OSError:
                continue
            if (
                stat.S_ISREG(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0
            ):
                entries.append((candidate, info))
        return entries

    def _prune(
        self,
        *,
        maximum_bytes: int,
        maximum_entries: int,
        protected_path: Path | None = None,
    ) -> None:
        total_bytes = 0
        kept_entries = 0
        entries = sorted(
            self._cache_entries(),
            key=lambda entry: (
                entry[0] == protected_path,
                entry[1].st_mtime_ns,
            ),
            reverse=True,
        )
        for candidate, info in entries:
            if (
                kept_entries < maximum_entries
                and total_bytes + info.st_size <= maximum_bytes
            ):
                kept_entries += 1
                total_bytes += info.st_size
                continue
            try:
                candidate.unlink()
            except OSError:
                pass

    def _cleanup_temporary_files(self) -> None:
        try:
            candidates = list(self.root.glob(".layout-ir-*.tmp"))
        except OSError:
            return
        for candidate in candidates:
            try:
                info = os.lstat(candidate)
            except OSError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                continue
            try:
                candidate.unlink()
            except OSError:
                pass


def _stable_file_fingerprint(
    path: Path,
) -> tuple[str, tuple[int, int, int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("layout IR cache input is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    before_identity = _file_identity(before)
    if before_identity != _file_identity(after):
        raise ValueError("layout IR cache input changed while hashing")
    return digest.hexdigest(), before_identity


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _non_negative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value
