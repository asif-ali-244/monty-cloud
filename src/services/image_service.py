"""Business logic for the image module.

Handlers call into here; nothing in this module knows that API Gateway exists.
"""

import hashlib
import logging
import uuid

from src.common import config, validation
from src.common.errors import NotFoundError
from src.services import metadata_repository as repo
from src.services import object_store

logger = logging.getLogger("images")

# Fields that exist for storage or indexing and are not part of the contract.
_INTERNAL_FIELDS = ("s3Key", "s3Bucket", "filenameLower")


def create_image(user_id, payload):
    """Validate, store bytes, then record metadata.

    Order matters. The object is written before the metadata so that a failure
    in between leaves an unreferenced object - invisible to every API, and
    logged as ``orphaned_object`` for the reconciliation sweep - rather than a
    metadata row pointing at bytes that were never stored, which every later
    read would surface as a 404.
    """
    filename = validation.clean_filename(
        validation.require_string(payload, "filename", validation.MAX_FILENAME_LENGTH)
    )
    content_type = validation.validate_content_type(
        validation.require_string(payload, "contentType")
    )
    data = validation.decode_image(payload.get("imageBase64"), content_type)
    tags = validation.normalise_tags(payload.get("tags"))
    description = validation.require_string(
        payload,
        "description",
        validation.MAX_DESCRIPTION_LENGTH,
        required=False,
    )

    image_id = uuid.uuid4().hex
    extension = config.ALLOWED_CONTENT_TYPES[content_type]
    s3_key = f"images/{user_id}/{image_id}{extension}"

    object_store.put_object(
        s3_key,
        data,
        content_type,
        metadata={"image-id": image_id, "user-id": user_id},
    )

    item = {
        "imageId": image_id,
        "userId": user_id,
        "filename": filename,
        "filenameLower": filename.lower(),
        "contentType": content_type,
        "sizeBytes": len(data),
        "checksumSha256": hashlib.sha256(data).hexdigest(),
        "tags": tags,
        "uploadedAt": validation.utc_now_iso(),
        "s3Key": s3_key,
        "s3Bucket": config.bucket_name(),
    }
    if description:
        item["description"] = description

    try:
        repo.put_new(item)
    except Exception:
        # Compensating delete: do not leave bytes behind for a row that never
        # landed. Best effort, so the caller still sees the original failure.
        try:
            object_store.delete_object(s3_key)
        except Exception:  # noqa: BLE001
            logger.warning("orphaned_object key=%s", s3_key)
        raise

    return to_public(item)


def list_images(filters):
    """List images, newest first, with cursor pagination."""
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
    item = repo.get(image_id)
    if not item:
        raise NotFoundError(f"Image '{image_id}' was not found")
    return to_public(item)


def build_download(image_id, disposition="inline", expires_in=None):
    """Return a short-lived presigned URL for the stored object."""
    item = repo.get(image_id)
    if not item:
        raise NotFoundError(f"Image '{image_id}' was not found")
    url = object_store.presigned_get_url(
        item["s3Key"],
        item["filename"],
        item["contentType"],
        expires_in or config.url_ttl_seconds(),
        disposition,
    )
    return {"url": url, "image": to_public(item)}


def delete_image(image_id):
    """Remove metadata first, then the object.

    The metadata row is the source of truth for whether an image exists, so it
    goes first: once it is gone the image is gone as far as every API is
    concerned. A failure to delete the object afterwards leaves an orphan that
    is logged for the reconciliation sweep, which is strictly better than a live
    row whose bytes have already been removed.
    """
    previous = repo.delete(image_id)
    if previous is None:
        raise NotFoundError(f"Image '{image_id}' was not found")
    try:
        object_store.delete_object(previous["s3Key"])
    except Exception:  # noqa: BLE001
        logger.warning(
            "orphaned_object image_id=%s key=%s", image_id, previous.get("s3Key")
        )
    return to_public(previous)


def to_public(item):
    """Strip storage-internal attributes from anything leaving the service."""
    return {k: v for k, v in item.items() if k not in _INTERNAL_FIELDS}
