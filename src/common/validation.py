"""Input validation and normalisation.

Request validation runs before a single AWS call is made, so a malformed request
costs one Lambda invocation and nothing downstream. Content validation (magic
bytes) runs later, in the upload processor, because the bytes never reach the API.
"""

import datetime as dt
import os
import re

from src.common import config
from src.common.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)

MAX_FILENAME_LENGTH = 255
MAX_TAGS = 20
MAX_TAG_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 1024
MAX_USER_ID_LENGTH = 128
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# The user id becomes an S3 key segment, so it must not be able to introduce a
# '/' (a new prefix) or characters that S3 event notifications URL-encode.
USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
_ISO_SUFFIX = "+00:00"

# Magic bytes, checked so that a caller cannot label an executable as image/png.
_SIGNATURES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],
}
# Enough leading bytes to decide every signature above, including WEBP's
# marker at offset 8.
SIGNATURE_PREFIX_BYTES = 12


def reject_unknown_fields(payload, allowed):
    """Refuse fields the contract does not define instead of silently ignoring them.

    A client that still sends the retired ``imageBase64`` should hear that the
    bytes were not accepted, not get a 201 for an image that will never arrive.
    """
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise ValidationError(
            "Unknown field(s): {}. Allowed: {}".format(
                ", ".join(unknown), ", ".join(sorted(allowed))
            )
        )


def require_string(payload, field, max_length=None, required=True, default=None):
    value = payload.get(field, default)
    if value is None or value == "":
        if required:
            raise ValidationError(f"'{field}' is required")
        return default
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"'{field}' must not be blank")
    if max_length and len(value) > max_length:
        raise ValidationError(
            f"'{field}' must be at most {max_length} characters"
        )
    return value


def clean_filename(raw):
    """Strip any directory component; the client does not get to choose S3 keys."""
    name = os.path.basename(raw.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise ValidationError("'filename' is not a valid file name")
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValidationError(
            f"'filename' must be at most {MAX_FILENAME_LENGTH} characters"
        )
    return name


def validate_content_type(value):
    normalised = value.split(";")[0].strip().lower()
    if normalised not in config.ALLOWED_CONTENT_TYPES:
        raise UnsupportedMediaTypeError(
            "'contentType' must be one of: {}".format(
                ", ".join(sorted(config.ALLOWED_CONTENT_TYPES))
            )
        )
    return normalised


def parse_size_bytes(value):
    """The declared size of the file the client is about to upload.

    It is signed into the upload policy as an exact content-length range, so S3
    itself refuses a body of any other size and the recorded size can be trusted.
    """
    # bool is a subclass of int, and a JSON `true` must not pass as 1 byte.
    if value is None:
        raise ValidationError("'sizeBytes' is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("'sizeBytes' must be an integer")
    if value < 1:
        raise ValidationError("'sizeBytes' must be at least 1")
    limit = config.max_image_bytes()
    if value > limit:
        raise PayloadTooLargeError(f"Image is {value} bytes; the limit is {limit} bytes")
    return value


def verify_signature(head, content_type):
    """Check the file's leading bytes against the declared content type."""
    signatures = _SIGNATURES.get(content_type, [])
    if signatures and not any(head.startswith(sig) for sig in signatures):
        raise ValidationError(
            f"File content does not match the declared contentType '{content_type}'"
        )
    if content_type == "image/webp" and head[8:12] != b"WEBP":
        raise ValidationError(
            "File content does not match the declared contentType 'image/webp'"
        )


def validate_user_id(value):
    if not isinstance(value, str) or not value:
        raise ValidationError("Caller identity missing: supply the X-User-Id header")
    if len(value) > MAX_USER_ID_LENGTH or not USER_ID_RE.match(value):
        raise ValidationError(
            "Caller identity must be 1-128 characters of letters, digits, '.', '_', '@' or '-'"
        )
    return value


def normalise_tags(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raise ValidationError("'tags' must be an array of strings")
    if len(raw) > MAX_TAGS:
        raise ValidationError(f"'tags' must contain at most {MAX_TAGS} items")
    seen = []
    for tag in raw:
        if not isinstance(tag, str):
            raise ValidationError("'tags' must be an array of strings")
        value = tag.strip().lower()
        if not value:
            continue
        if len(value) > MAX_TAG_LENGTH:
            raise ValidationError(
                f"tag '{value}' exceeds {MAX_TAG_LENGTH} characters"
            )
        if not _TAG_RE.match(value):
            raise ValidationError(
                f"tag '{value}' may contain only lowercase letters, digits, '-' and '_'"
            )
        if value not in seen:
            seen.append(value)
    return seen


def parse_timestamp(value, field):
    """Accept an ISO-8601 instant and return the canonical stored form."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be an ISO-8601 timestamp")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + _ISO_SUFFIX
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"'{field}' must be an ISO-8601 timestamp, e.g. 2024-05-01T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return to_iso(parsed.astimezone(dt.timezone.utc))


def parse_limit(value):
    if value is None or value == "":
        return config.DEFAULT_PAGE_SIZE
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("'limit' must be an integer") from exc
    if limit < 1:
        raise ValidationError("'limit' must be at least 1")
    return min(limit, config.MAX_PAGE_SIZE)


def parse_positive_int(value, field, default, maximum):
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"'{field}' must be an integer") from exc
    if parsed < 1:
        raise ValidationError(f"'{field}' must be at least 1")
    return min(parsed, maximum)


def to_iso(moment):
    """Canonical, lexicographically sortable UTC form used as the GSI sort key."""
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        _ISO_SUFFIX, "Z"
    )


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def utc_now_iso():
    return to_iso(utc_now())


def epoch_seconds(moment):
    """DynamoDB TTL wants a Number of epoch seconds, not an ISO string."""
    return int(moment.timestamp())
