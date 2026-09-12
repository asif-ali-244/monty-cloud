"""S3 access for the image bytes themselves."""

from botocore.exceptions import ClientError

from src.common import config
from src.common.errors import NotFoundError, StorageError


def put_object(key, data, content_type, metadata=None):
    try:
        config.s3_client().put_object(
            Bucket=config.bucket_name(),
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=metadata or {},
        )
    except ClientError as exc:
        raise StorageError("Could not store the image") from exc
    return key


def delete_object(key):
    try:
        config.s3_client().delete_object(Bucket=config.bucket_name(), Key=key)
    except ClientError as exc:
        raise StorageError("Could not delete the stored image") from exc


def get_object_bytes(key):
    try:
        result = config.s3_client().get_object(Bucket=config.bucket_name(), Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise NotFoundError("Stored image is no longer available") from exc
        raise StorageError("Could not read the stored image") from exc
    return result["Body"].read()


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
