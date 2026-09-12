"""Input validation and normalisation.

All of it runs before a single AWS call is made, so a malformed request costs
one Lambda invocation and nothing downstream.
"""

import base64
import binascii
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
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ISO_SUFFIX = "+00:00"

# Magic bytes, checked so that a caller cannot label an executable as image/png.
_SIGNATURES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],
}


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


def decode_image(raw, content_type):
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("'imageBase64' is required")
    payload = raw.strip()
    if payload.startswith("data:"):
        # Accept the data-URL form browsers produce: data:image/png;base64,AAA
        _, _, payload = payload.partition(",")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("'imageBase64' is not valid base64") from exc
    if not data:
        raise ValidationError("'imageBase64' decoded to an empty file")
    limit = config.max_image_bytes()
    if len(data) > limit:
        raise PayloadTooLargeError(
            f"Image is {len(data)} bytes; the limit is {limit} bytes"
        )
    _verify_signature(data, content_type)
    return data


def _verify_signature(data, content_type):
    signatures = _SIGNATURES.get(content_type, [])
    if signatures and not any(data.startswith(sig) for sig in signatures):
        raise ValidationError(
            f"File content does not match the declared contentType '{content_type}'"
        )
    if content_type == "image/webp" and data[8:12] != b"WEBP":
        raise ValidationError(
            "File content does not match the declared contentType 'image/webp'"
        )


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


def utc_now_iso():
    return to_iso(dt.datetime.now(dt.timezone.utc))
