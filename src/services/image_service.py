"""Business logic for the image module.

Handlers call into here; nothing in this module knows that API Gateway or S3
event notifications exist.

Upload lifecycle
----------------
1. ``register_upload`` records a ``pending`` row and returns a presigned POST.
2. The client sends the file straight to S3; S3 enforces key, type and the
   size limit.
3. S3 emits ObjectCreated, and ``process_uploaded_object`` verifies the bytes
   and moves the row to ``ready`` - or to ``rejected``, deleting the object.

Only ``ready`` images are listed or downloadable. A ``pending`` row whose upload
never arrives expires through DynamoDB TTL.
"""

import datetime as dt
import hashlib
import logging
import os
import re
import uuid

from src.common import config, validation
from src.common.errors import ImageNotReadyError, NotFoundError, ValidationError
from src.services import metadata_repository as repo
from src.services import object_store

logger = logging.getLogger("images")

# Fields that exist for storage, indexing or expiry and are not part of the contract.
_INTERNAL_FIELDS = ("s3Key", "s3Bucket", "filenameLower", "expiresAt")
_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HASH_CHUNK_BYTES = 1024 * 1024
REGISTER_FIELDS = ("filename", "contentType", "tags", "description")


def register_upload(user_id, payload):
    """Validate the metadata, record a pending row, and sign an upload form.

    The row is written before the form is signed, the reverse of what a
    synchronous upload would need. That is safe here because ``pending`` is a
    real state: a pending row is invisible to listings and refused by downloads,
    so it can never be mistaken for an image whose bytes exist. And ordering it
    this way means an object can only ever land for a row that already exists.
    """
    validation.reject_unknown_fields(payload, REGISTER_FIELDS)
    filename = validation.clean_filename(
        validation.require_string(payload, "filename", validation.MAX_FILENAME_LENGTH)
    )
    content_type = validation.validate_content_type(
        validation.require_string(payload, "contentType")
    )
    tags = validation.normalise_tags(payload.get("tags"))
    description = validation.require_string(
        payload, "description", validation.MAX_DESCRIPTION_LENGTH, required=False
    )

    image_id = uuid.uuid4().hex
    extension = config.ALLOWED_CONTENT_TYPES[content_type]
    s3_key = f"{config.UPLOAD_KEY_PREFIX}{user_id}/{image_id}{extension}"

    now = validation.utc_now()
    upload_ttl = config.upload_url_ttl_seconds()
    upload_expires = now + dt.timedelta(seconds=upload_ttl)
    row_expires = upload_expires + dt.timedelta(seconds=config.PENDING_GRACE_SECONDS)

    item = {
        "imageId": image_id,
        "userId": user_id,
        "filename": filename,
        "filenameLower": filename.lower(),
        "contentType": content_type,
        "tags": tags,
        "status": repo.STATUS_PENDING,
        "createdAt": validation.to_iso(now),
        "expiresAt": validation.epoch_seconds(row_expires),
        "s3Key": s3_key,
        "s3Bucket": config.bucket_name(),
    }
    if description:
        item["description"] = description

    max_bytes = config.max_image_bytes()
    repo.put_new(item)
    form = object_store.presigned_post(s3_key, content_type, max_bytes, upload_ttl)

    return {
        "image": to_public(item),
        "upload": {
            "method": "POST",
            "url": form["url"],
            "fields": form["fields"],
            "fileField": "file",
            "maxSizeBytes": max_bytes,
            "expiresAt": validation.to_iso(upload_expires),
            "expiresInSeconds": upload_ttl,
        },
    }


def process_uploaded_object(key):
    """Verify one uploaded object and settle its row. Returns the outcome.

    Safe to call more than once for the same object: S3 event delivery is
    at-least-once, so every branch is idempotent.
    """
    image_id = image_id_from_key(key)
    if image_id is None:
        logger.warning("upload_ignored reason=unrecognised_key key=%s", key)
        return "ignored"

    item = repo.get(image_id)
    if item is None or item.get("s3Key") != key:
        # The image was deleted, or its pending row expired, before the upload
        # was processed. Nothing references these bytes.
        object_store.delete_object(key)
        logger.info("upload_orphan_deleted image_id=%s key=%s", image_id, key)
        return "orphan_deleted"

    status = item.get("status")
    if status == repo.STATUS_READY:
        return "already_ready"
    if status == repo.STATUS_REJECTED:
        # A retry after the row was rejected but before the delete succeeded.
        object_store.delete_object(key)
        return "already_rejected"

    head = object_store.head_object(key)
    if head is None:
        return "object_missing"

    max_bytes = config.max_image_bytes()
    problem = _check_stored_attributes(item, head, max_bytes)
    checksum = size = None
    if problem is None:
        body = object_store.open_object(key)
        if body is None:
            return "object_missing"
        try:
            checksum, size, problem = _hash_and_verify(body, item["contentType"], max_bytes)
        finally:
            body.close()

    if problem is not None:
        return _reject(image_id, key, problem)

    if repo.mark_ready(image_id, key, size, checksum, validation.utc_now_iso()) is None:
        # The row changed while the object was being hashed.
        if repo.get(image_id) is None:
            object_store.delete_object(key)
            logger.info("upload_orphan_deleted image_id=%s key=%s", image_id, key)
            return "orphan_deleted"
        return "already_ready"

    logger.info("upload_ready image_id=%s size=%s", image_id, size)
    return "ready"


def _reject(image_id, key, reason):
    """Mark the row rejected first, then delete the object.

    In that order a failed delete is retried safely: the retry finds a rejected
    row and deletes again. The reverse order would strand a pending row whose
    object is already gone.
    """
    expires = validation.utc_now() + dt.timedelta(seconds=config.REJECTED_RETENTION_SECONDS)
    repo.mark_rejected(image_id, key, reason, validation.epoch_seconds(expires))
    object_store.delete_object(key)
    logger.warning("upload_rejected image_id=%s reason=%s", image_id, reason)
    return "rejected"


def _check_stored_attributes(item, head, max_bytes):
    """Cheap checks from the object's metadata, before reading any bytes.

    The POST policy already enforces both, so these only fire if an object
    reached the key some other way. Defence in depth for the price of a HEAD.
    """
    problem = _size_problem(int(head.get("ContentLength", 0)), max_bytes)
    if problem:
        return problem
    stored_type = (head.get("ContentType") or "").split(";")[0].strip().lower()
    if stored_type != item["contentType"]:
        return f"Uploaded as '{stored_type}' but '{item['contentType']}' was declared"
    return None


def _hash_and_verify(body, content_type, max_bytes):
    """Stream the object once: check its magic bytes early, and hash all of it.

    Returns (sha256 hex, size, problem). A signature mismatch stops the read at
    the first chunk, and a body that grows past the limit stops it there, rather
    than paying to hash a file that is being rejected. The size measured here -
    not the earlier HEAD - is the one recorded.
    """
    digest = hashlib.sha256()
    size = 0
    head = b""
    for chunk in body.iter_chunks(chunk_size=_HASH_CHUNK_BYTES):
        if len(head) < validation.SIGNATURE_PREFIX_BYTES:
            head += chunk[: validation.SIGNATURE_PREFIX_BYTES - len(head)]
            if len(head) == validation.SIGNATURE_PREFIX_BYTES:
                problem = _signature_problem(head, content_type)
                if problem:
                    return None, size, problem
        digest.update(chunk)
        size += len(chunk)
        if size > max_bytes:
            return None, size, _size_problem(size, max_bytes)
    problem = _size_problem(size, max_bytes) or _signature_problem(head, content_type)
    if problem:
        return None, size, problem
    return digest.hexdigest(), size, None


def _size_problem(size, max_bytes):
    if size < 1:
        return "Uploaded file is empty"
    if size > max_bytes:
        return f"Uploaded {size} bytes; the limit is {max_bytes} bytes"
    return None


def _signature_problem(head, content_type):
    try:
        validation.verify_signature(head, content_type)
    except ValidationError as exc:
        return exc.message
    return None


def image_id_from_key(key):
    """Recover the image id from an upload key, or None if the key is not one of ours."""
    if not key.startswith(config.UPLOAD_KEY_PREFIX):
        return None
    stem, _ = os.path.splitext(os.path.basename(key))
    return stem if _IMAGE_ID_RE.match(stem) else None


def list_images(filters):
    """List ready images, newest first, with cursor pagination."""
    result = repo.list_images(
        user_id=filters.get("userId"),
        tag=filters.get("tag"),
        content_type=filters.get("contentType"),
        uploaded_from=filters.get("uploadedFrom"),
        uploaded_to=filters.get("uploadedTo"),
        filename_contains=filters.get("filename"),
        limit=filters.get("limit"),
        next_token=filters.get("nextToken"),
    )
    items = [to_public(item) for item in result["items"]]
    if result["scanned"]:
        # Without a userId there is no partition to query, so ordering is
        # whatever the scan returned; sort the page for a stable contract.
        items.sort(key=lambda i: i.get("uploadedAt", ""), reverse=True)
    return {
        "items": items,
        "count": len(items),
        "nextToken": result["nextToken"],
    }


def get_image(image_id):
    """Any state - this is how a client polls a pending upload."""
    item = repo.get(image_id)
    if not item:
        raise NotFoundError(f"Image '{image_id}' was not found")
    return to_public(item)


def build_download(image_id, disposition="inline", expires_in=None):
    """Return a short-lived presigned URL for a ready image."""
    item = repo.get(image_id)
    if not item:
        raise NotFoundError(f"Image '{image_id}' was not found")
    status = item.get("status")
    if status == repo.STATUS_REJECTED:
        raise ImageNotReadyError(
            f"Image '{image_id}' was rejected: {item.get('rejectionReason', 'unknown reason')}"
        )
    if status != repo.STATUS_READY:
        raise ImageNotReadyError(f"Image '{image_id}' has not finished uploading")
    url = object_store.presigned_get_url(
        item["s3Key"],
        item["filename"],
        item["contentType"],
        expires_in or config.url_ttl_seconds(),
        disposition,
    )
    return {"url": url, "image": to_public(item)}


def delete_image(image_id):
    """Remove metadata first, then the object - in any state.

    The metadata row is the source of truth for whether an image exists, so it
    goes first: once it is gone the image is gone as far as every API is
    concerned. A pending image may have no object yet; deleting a missing key
    succeeds. If the upload lands after this, the processor finds no row and
    deletes the object. A failure to delete here leaves an orphan that is logged.
    """
    previous = repo.delete(image_id)
    if previous is None:
        raise NotFoundError(f"Image '{image_id}' was not found")
    try:
        object_store.delete_object(previous["s3Key"])
    except Exception:  # noqa: BLE001
        logger.warning("orphaned_object image_id=%s key=%s", image_id, previous.get("s3Key"))
    return to_public(previous)


def to_public(item):
    """Strip storage-internal attributes from anything leaving the service."""
    return {k: v for k, v in item.items() if k not in _INTERNAL_FIELDS}
