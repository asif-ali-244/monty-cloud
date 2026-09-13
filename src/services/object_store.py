"""S3 access for the image bytes themselves.

The service never receives image bytes over the API. Clients upload straight to
S3 with a presigned POST, and the upload processor reads the object back from S3
to validate it.
"""

from botocore.exceptions import ClientError

from src.common import config
from src.common.errors import StorageError

_MISSING = ("NoSuchKey", "404", "NotFound")


def presigned_post(key, content_type, size_bytes, expires_in):
    """Sign a browser-compatible POST that can create exactly one object.

    A POST policy is used instead of a presigned PUT because only a policy can
    constrain the body. S3 itself enforces every condition below, before a byte
    is stored: the exact key, the exact Content-Type, and a content-length range
    pinned to the declared size. Any mismatch is refused with 400/403.
    """
    try:
        return config.s3_signing_client().generate_presigned_post(
            Bucket=config.bucket_name(),
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", size_bytes, size_bytes],
            ],
            ExpiresIn=expires_in,
        )
    except ClientError as exc:
        raise StorageError("Could not create an upload URL") from exc


def head_object(key):
    """Return the object's metadata, or None if it does not exist."""
    try:
        return config.s3_client().head_object(Bucket=config.bucket_name(), Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in _MISSING:
            return None
        raise StorageError("Could not inspect the uploaded image") from exc


def open_object(key):
    """Open the object as a stream, or return None if it does not exist.

    Returned unread so the caller can hash it chunk by chunk: memory stays flat
    however large the image is.
    """
    try:
        return config.s3_client().get_object(Bucket=config.bucket_name(), Key=key)["Body"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in _MISSING:
            return None
        raise StorageError("Could not read the uploaded image") from exc


def delete_object(key):
    """Delete the object. Deleting a key that does not exist succeeds."""
    try:
        config.s3_client().delete_object(Bucket=config.bucket_name(), Key=key)
    except ClientError as exc:
        raise StorageError("Could not delete the stored image") from exc


def presigned_get_url(key, filename, content_type, expires_in, disposition="inline"):
    """Sign a direct-to-S3 GET.

    The response headers are signed in too, so the browser gets the original
    file name and either renders the image or saves it, without the bytes ever
    touching Lambda.
    """
    disposition_header = '{}; filename="{}"'.format(
        "attachment" if disposition == "attachment" else "inline",
        filename.replace('"', ""),
    )
    try:
        return config.s3_signing_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": config.bucket_name(),
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": disposition_header,
            },
            ExpiresIn=expires_in,
        )
    except ClientError as exc:
        raise StorageError("Could not create a download URL") from exc
