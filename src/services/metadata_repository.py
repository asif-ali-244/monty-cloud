"""DynamoDB access for image metadata.

Table design
------------
``imageId`` (partition key) is the only key on the base table, because every
read of a single image is by id.

One GSI, ``userId-uploadedAt-index`` (partition ``userId``, sort ``uploadedAt``),
serves the dominant access pattern of an Instagram-shaped service: "the images
belonging to this user, newest first", optionally narrowed to a time window.
That pattern is a Query, so it stays O(results) no matter how large the table
grows, and the ISO-8601 sort key makes the date filter a key condition rather
than a post-filter.

The GSI is also sparse, and deliberately so. ``uploadedAt`` is written only
when an upload is verified, so pending and rejected rows are simply absent from
the index: the indexed listing never sees them and needs no status filter. The
Scan fallback has no index to lean on and filters on ``status`` explicitly.

Filters that are not part of a key (tag, contentType) are applied as
FilterExpressions. They are evaluated after the read, so they reduce payload
but not consumed capacity - an accepted trade at this scale, and the reason
``list`` reports whether a scan was used.
"""

import base64
import json

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.common import config
from src.common.errors import ConflictError, StorageError, ValidationError

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_REJECTED = "rejected"


def put_new(item):
    """Insert metadata, refusing to clobber an existing id."""
    try:
        config.dynamodb_table().put_item(
            Item=item, ConditionExpression=Attr("imageId").not_exists()
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConflictError(f"Image '{item['imageId']}' already exists") from exc
        raise StorageError("Could not persist image metadata") from exc
    return item


def get(image_id):
    try:
        result = config.dynamodb_table().get_item(Key={"imageId": image_id})
    except ClientError as exc:
        raise StorageError("Could not read image metadata") from exc
    return result.get("Item")


def delete(image_id):
    """Delete and return the previous item, or None if it was not there."""
    try:
        result = config.dynamodb_table().delete_item(
            Key={"imageId": image_id},
            ConditionExpression=Attr("imageId").exists(),
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise StorageError("Could not delete image metadata") from exc
    return result.get("Attributes")


def mark_ready(image_id, s3_key, size_bytes, checksum, uploaded_at):
    """Promote a pending upload to ready. Returns the new item, or None if it was not pending.

    The condition makes this safe against every race the async flow allows: a
    duplicate S3 event (already ready), an image deleted while its upload was in
    flight (row gone), or an object that does not belong to this row (key
    mismatch). In each case nothing is written and the caller decides what to do.

    Writing ``uploadedAt`` is what adds the row to the sparse user index, and
    removing ``expiresAt`` takes it out of TTL expiry.
    """
    return _transition(
        image_id,
        s3_key,
        "SET #status = :to, sizeBytes = :size, checksumSha256 = :checksum, "
        "uploadedAt = :uploadedAt REMOVE expiresAt",
        {
            ":to": STATUS_READY,
            ":size": size_bytes,
            ":checksum": checksum,
            ":uploadedAt": uploaded_at,
        },
    )


def mark_rejected(image_id, s3_key, reason, expires_at):
    """Record why a pending upload was refused. Returns the new item, or None if not pending.

    The row is kept, with a short TTL, so a client polling GET /images/{id} can
    read the reason instead of watching the image silently vanish.
    """
    return _transition(
        image_id,
        s3_key,
        "SET #status = :to, rejectionReason = :reason, expiresAt = :expiresAt",
        {":to": STATUS_REJECTED, ":reason": reason, ":expiresAt": expires_at},
    )


def _transition(image_id, s3_key, update_expression, values):
    try:
        result = config.dynamodb_table().update_item(
            Key={"imageId": image_id},
            UpdateExpression=update_expression,
            ConditionExpression="#status = :pending AND s3Key = :key",
            # `status` is a DynamoDB reserved word.
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={**values, ":pending": STATUS_PENDING, ":key": s3_key},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise StorageError("Could not update image metadata") from exc
    return result.get("Attributes")


def list_images(
    user_id=None,
    tag=None,
    content_type=None,
    uploaded_from=None,
    uploaded_to=None,
    filename_contains=None,
    limit=None,
    next_token=None,
    newest_first=True,
):
    limit = limit or config.DEFAULT_PAGE_SIZE
    filter_expression = _build_filter(tag, content_type, filename_contains)
    params = {"Limit": limit}
    if filter_expression is not None:
        params["FilterExpression"] = filter_expression

    start_key = decode_token(next_token)
    if start_key:
        params["ExclusiveStartKey"] = start_key

    try:
        if user_id:
            params["IndexName"] = config.user_index_name()
            params["KeyConditionExpression"] = _key_condition(
                user_id, uploaded_from, uploaded_to
            )
            params["ScanIndexForward"] = not newest_first
            result = config.dynamodb_table().query(**params)
            scanned = False
        else:
            # No partition key to anchor on. Bounded Scan with the date range
            # pushed down as a filter; see the module docstring for why this is
            # the deliberate fallback and not the primary path. Unlike the sparse
            # index, the base table holds pending and rejected rows too.
            scan_filter = Attr("status").eq(STATUS_READY)
            for clause in (filter_expression, _date_filter(uploaded_from, uploaded_to)):
                if clause is not None:
                    scan_filter = scan_filter & clause
            params["FilterExpression"] = scan_filter
            result = config.dynamodb_table().scan(**params)
            scanned = True
    except ClientError as exc:
        raise StorageError("Could not list images") from exc

    return {
        "items": result.get("Items", []),
        "nextToken": encode_token(result.get("LastEvaluatedKey")),
        "scanned": scanned,
    }


def _key_condition(user_id, uploaded_from, uploaded_to):
    condition = Key("userId").eq(user_id)
    if uploaded_from and uploaded_to:
        return condition & Key("uploadedAt").between(uploaded_from, uploaded_to)
    if uploaded_from:
        return condition & Key("uploadedAt").gte(uploaded_from)
    if uploaded_to:
        return condition & Key("uploadedAt").lte(uploaded_to)
    return condition


def _date_filter(uploaded_from, uploaded_to):
    if uploaded_from and uploaded_to:
        return Attr("uploadedAt").between(uploaded_from, uploaded_to)
    if uploaded_from:
        return Attr("uploadedAt").gte(uploaded_from)
    if uploaded_to:
        return Attr("uploadedAt").lte(uploaded_to)
    return None


def _build_filter(tag, content_type, filename_contains):
    expression = None
    if tag:
        expression = Attr("tags").contains(tag)
    if content_type:
        clause = Attr("contentType").eq(content_type)
        expression = clause if expression is None else expression & clause
    if filename_contains:
        clause = Attr("filenameLower").contains(filename_contains.lower())
        expression = clause if expression is None else expression & clause
    return expression


def encode_token(last_key):
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key, default=str).encode()).decode()


def decode_token(token):
    if not token:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except Exception as exc:  # noqa: BLE001 - any malformed token is a 400
        raise ValidationError("'nextToken' is not a valid pagination token") from exc
