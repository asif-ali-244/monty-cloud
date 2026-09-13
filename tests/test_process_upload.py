"""S3 ObjectCreated -> process_upload: verify bytes, settle the row.

S3 event delivery is asynchronous and at-least-once, and the client controls
the bytes. So beyond the happy path this suite is mostly about races, duplicate
events and hostile content.
"""

import datetime as dt
import hashlib

import pytest

from src.common import config
from src.handlers import process_upload
from tests.conftest import (
    GIF_BYTES,
    JPEG_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    WEBP_BYTES,
    client_upload,
    fetch_image,
    process,
    register,
    s3_event,
    s3_keys,
    table_item,
)


def outcome(result):
    return [entry["outcome"] for entry in result["processed"]]


class TestHappyPath:
    def test_marks_a_valid_upload_ready(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        assert outcome(process(context, key)) == ["ready"]

        image = fetch_image(context, pending_image["image"]["imageId"])
        assert image["status"] == "ready"
        assert image["sizeBytes"] == len(PNG_BYTES)
        assert image["checksumSha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
        assert image["uploadedAt"].endswith("Z")
        assert image["createdAt"] <= image["uploadedAt"]

    def test_ready_rows_leave_ttl_expiry(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        process(context, key)
        assert "expiresAt" not in table_item(pending_image["image"]["imageId"])

    def test_keeps_the_object(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        process(context, key)
        assert s3_keys() == [key]

    @pytest.mark.parametrize(
        "content_type,data",
        [
            ("image/jpeg", JPEG_BYTES),
            ("image/png", PNG_BYTES),
            ("image/gif", GIF_BYTES),
            ("image/webp", WEBP_BYTES),
        ],
    )
    def test_accepts_every_supported_type(
        self, aws, context, upload_payload, content_type, data
    ):
        registered = register(
            context,
            upload_payload(contentType=content_type, filename="a.bin"),
        )
        key = client_upload(registered, data)
        assert outcome(process(context, key)) == ["ready"]

    def test_hashes_files_larger_than_one_read_chunk(self, aws, context, upload_payload):
        """The object is streamed in 1 MiB chunks; the digest must cover all of them."""
        data = PNG_BYTES + b"\x00" * (3 * 1024 * 1024 + 17)
        registered = register(context, upload_payload())
        key = client_upload(registered, data)
        process(context, key)

        image = fetch_image(context, registered["image"]["imageId"])
        assert image["status"] == "ready"
        assert image["checksumSha256"] == hashlib.sha256(data).hexdigest()
        assert image["sizeBytes"] == len(data)

    def test_processes_every_record_in_one_event(self, aws, context, upload_payload):
        keys = []
        for index in range(3):
            registered = register(context, upload_payload(filename=f"f{index}.png"))
            keys.append(client_upload(registered, PNG_BYTES))
        assert outcome(process(context, *keys)) == ["ready", "ready", "ready"]


class TestRejections:
    def test_content_that_is_not_the_declared_type(self, aws, context, upload_payload):
        """A PDF uploaded as image/png is caught by its magic bytes."""
        registered = register(context, upload_payload())
        key = client_upload(registered, PDF_BYTES)

        assert outcome(process(context, key)) == ["rejected"]
        image = fetch_image(context, registered["image"]["imageId"])
        assert image["status"] == "rejected"
        assert "does not match the declared contentType" in image["rejectionReason"]
        assert s3_keys() == []

    def test_webp_container_without_the_webp_marker(self, aws, context, upload_payload):
        riff = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 16
        registered = register(
            context, upload_payload(contentType="image/webp")
        )
        key = client_upload(registered, riff)
        assert outcome(process(context, key)) == ["rejected"]

    def test_object_over_the_size_limit(self, aws, context, pending_image, monkeypatch):
        """The policy prevents this; the processor checks anyway."""
        monkeypatch.setenv("MAX_IMAGE_BYTES", str(len(PNG_BYTES) - 1))
        key = client_upload(pending_image, PNG_BYTES)

        assert outcome(process(context, key)) == ["rejected"]
        reason = fetch_image(context, pending_image["image"]["imageId"])["rejectionReason"]
        assert reason == f"Uploaded {len(PNG_BYTES)} bytes; the limit is {len(PNG_BYTES) - 1} bytes"
        assert s3_keys() == []

    def test_empty_object(self, aws, context, pending_image):
        """The policy's 1-byte minimum prevents this; the processor checks anyway."""
        key = client_upload(pending_image, b"")
        assert outcome(process(context, key)) == ["rejected"]
        image = fetch_image(context, pending_image["image"]["imageId"])
        assert image["rejectionReason"] == "Uploaded file is empty"

    def test_size_is_measured_from_the_stored_object(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        process(context, key)
        assert fetch_image(context, pending_image["image"]["imageId"])["sizeBytes"] == len(
            PNG_BYTES
        )

    def test_stored_content_type_different_from_the_declared_one(
        self, aws, context, pending_image
    ):
        key = client_upload(pending_image, PNG_BYTES, content_type="text/html")
        assert outcome(process(context, key)) == ["rejected"]
        assert "text/html" in fetch_image(context, pending_image["image"]["imageId"])[
            "rejectionReason"
        ]

    def test_file_shorter_than_the_signature(self, aws, context, upload_payload):
        tiny = b"\x89P"
        registered = register(context, upload_payload())
        key = client_upload(registered, tiny)
        assert outcome(process(context, key)) == ["rejected"]

    def test_rejected_rows_expire_after_the_retention_window(self, aws, context, upload_payload):
        registered = register(context, upload_payload())
        key = client_upload(registered, PDF_BYTES)
        before = int(dt.datetime.now(dt.timezone.utc).timestamp())
        process(context, key)

        expires = int(table_item(registered["image"]["imageId"])["expiresAt"])
        expected = before + config.REJECTED_RETENTION_SECONDS
        assert expected <= expires <= expected + 5

    def test_rejected_rows_never_reach_the_user_index(self, aws, context, upload_payload):
        registered = register(context, upload_payload())
        process(context, client_upload(registered, PDF_BYTES))
        assert "uploadedAt" not in table_item(registered["image"]["imageId"])

    def test_signature_mismatch_stops_reading_early(
        self, aws, context, upload_payload, monkeypatch
    ):
        """A hostile multi-megabyte file must not be hashed to the end."""
        from src.services import image_service

        monkeypatch.setattr(image_service, "_HASH_CHUNK_BYTES", 64)
        data = PDF_BYTES + b"\x00" * 10_000
        registered = register(context, upload_payload())
        key = client_upload(registered, data)

        chunks_read = []
        real_open = image_service.object_store.open_object

        def counting_open(object_key):
            body = real_open(object_key)
            original = body.iter_chunks

            def iter_chunks(chunk_size):
                for chunk in original(chunk_size=chunk_size):
                    chunks_read.append(len(chunk))
                    yield chunk

            body.iter_chunks = iter_chunks
            return body

        monkeypatch.setattr(image_service.object_store, "open_object", counting_open)
        assert outcome(process(context, key)) == ["rejected"]
        assert len(chunks_read) == 1


class TestRacesAndRedelivery:
    def test_duplicate_event_is_a_no_op(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        process(context, key)
        first = table_item(pending_image["image"]["imageId"])

        assert outcome(process(context, key)) == ["already_ready"]
        assert table_item(pending_image["image"]["imageId"]) == first

    def test_image_deleted_before_its_upload_was_processed(self, aws, context, pending_image):
        from src.handlers import delete_image
        from tests.conftest import api_event

        key = pending_image["upload"]["fields"]["key"]
        delete_image.handler(
            api_event("DELETE", path_parameters={"imageId": pending_image["image"]["imageId"]}),
            context,
        )
        # The client's upload lands after the delete.
        client_upload(pending_image, PNG_BYTES)

        assert outcome(process(context, key)) == ["orphan_deleted"]
        assert s3_keys() == []

    def test_row_deleted_while_the_object_was_being_hashed(
        self, aws, context, pending_image, monkeypatch
    ):
        from src.services import image_service
        from src.services import metadata_repository as repo

        key = client_upload(pending_image, PNG_BYTES)
        real_mark_ready = repo.mark_ready

        def delete_then_mark(image_id, *args):
            repo.delete(image_id)
            return real_mark_ready(image_id, *args)

        monkeypatch.setattr(image_service.repo, "mark_ready", delete_then_mark)
        assert outcome(process(context, key)) == ["orphan_deleted"]
        assert s3_keys() == []

    def test_concurrent_duplicate_that_won_the_race(self, aws, context, pending_image, monkeypatch):
        from src.services import image_service
        from src.services import metadata_repository as repo

        key = client_upload(pending_image, PNG_BYTES)
        real_mark_ready = repo.mark_ready

        def other_invocation_wins(image_id, *args):
            real_mark_ready(image_id, *args)
            return real_mark_ready(image_id, *args)

        monkeypatch.setattr(image_service.repo, "mark_ready", other_invocation_wins)
        assert outcome(process(context, key)) == ["already_ready"]
        assert s3_keys() == [key]

    def test_retry_after_a_rejection_whose_delete_failed(self, aws, context, upload_payload):
        from src.services import metadata_repository as repo

        registered = register(context, upload_payload())
        key = client_upload(registered, PDF_BYTES)
        repo.mark_rejected(registered["image"]["imageId"], key, "earlier attempt", 0)

        assert outcome(process(context, key)) == ["already_rejected"]
        assert s3_keys() == []

    def test_object_already_gone(self, aws, context, pending_image):
        key = pending_image["upload"]["fields"]["key"]
        assert outcome(process(context, key)) == ["object_missing"]
        assert fetch_image(context, pending_image["image"]["imageId"])["status"] == "pending"

    def test_object_disappears_between_head_and_read(
        self, aws, context, pending_image, monkeypatch
    ):
        from src.services import image_service

        key = client_upload(pending_image, PNG_BYTES)
        monkeypatch.setattr(image_service.object_store, "open_object", lambda _key: None)
        assert outcome(process(context, key)) == ["object_missing"]


class TestForeignObjects:
    def test_object_at_a_key_that_no_row_owns(self, aws, context):
        import boto3

        key = "images/user-alice/0123456789abcdef0123456789abcdef.png"
        boto3.client("s3", region_name="us-east-1").put_object(
            Bucket="test-images-bucket", Key=key, Body=PNG_BYTES, ContentType="image/png"
        )
        assert outcome(process(context, key)) == ["orphan_deleted"]
        assert s3_keys() == []

    def test_object_whose_key_does_not_match_the_row(self, aws, context, pending_image):
        """Same image id, different path: not the object this row signed for."""
        import boto3

        image_id = pending_image["image"]["imageId"]
        forged = f"images/someone-else/{image_id}.png"
        boto3.client("s3", region_name="us-east-1").put_object(
            Bucket="test-images-bucket", Key=forged, Body=PNG_BYTES, ContentType="image/png"
        )
        assert outcome(process(context, forged)) == ["orphan_deleted"]
        assert fetch_image(context, image_id)["status"] == "pending"

    @pytest.mark.parametrize(
        "key", ["somewhere/else.png", "images/user-alice/not-an-id.png", "images/"]
    )
    def test_keys_outside_the_upload_scheme_are_ignored(self, aws, context, key):
        assert outcome(process(context, key)) == ["ignored"]

    def test_non_create_events_are_skipped(self, aws, context, pending_image):
        key = client_upload(pending_image, PNG_BYTES)
        result = process_upload.handler(
            s3_event(key, event_name="ObjectRemoved:Delete"), context
        )
        assert result == {"processed": []}
        assert fetch_image(context, pending_image["image"]["imageId"])["status"] == "pending"

    def test_empty_event(self, aws, context):
        assert process_upload.handler({}, context) == {"processed": []}

    def test_url_encoded_keys_are_decoded(self, aws, context, upload_payload):
        """S3 encodes keys in notifications; '@' in a user id arrives as %40."""
        registered = register(context, upload_payload(), user_id="a.b@example.com")
        key = client_upload(registered, PNG_BYTES)
        event = s3_event(key)
        assert "%40" in event["Records"][0]["s3"]["object"]["key"]

        assert outcome(process_upload.handler(event, context)) == ["ready"]


class TestTransientFailures:
    def test_storage_errors_propagate_so_lambda_retries(
        self, aws, context, pending_image, monkeypatch
    ):
        from src.common.errors import StorageError
        from src.services import image_service

        key = client_upload(pending_image, PNG_BYTES)

        def unavailable(_key):
            raise StorageError("Could not inspect the uploaded image")

        monkeypatch.setattr(image_service.object_store, "head_object", unavailable)
        with pytest.raises(StorageError):
            process(context, key)
        assert fetch_image(context, pending_image["image"]["imageId"])["status"] == "pending"

    def test_a_retry_after_a_transient_failure_succeeds(
        self, aws, context, pending_image, monkeypatch
    ):
        from src.common.errors import StorageError
        from src.services import image_service

        key = client_upload(pending_image, PNG_BYTES)
        real_head = image_service.object_store.head_object
        calls = {"n": 0}

        def flaky(object_key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise StorageError("blip")
            return real_head(object_key)

        monkeypatch.setattr(image_service.object_store, "head_object", flaky)
        with pytest.raises(StorageError):
            process(context, key)
        assert outcome(process(context, key)) == ["ready"]


def test_object_replaced_between_head_and_read(aws, context, pending_image, monkeypatch):
    """HEAD said the object was within the limit, but the bytes streamed are not.

    Only reachable if the object is overwritten mid-processing; the size measured
    while hashing is the one trusted, not the earlier HEAD.
    """
    from src.services import image_service

    data = PNG_BYTES + b"overwritten"
    monkeypatch.setenv("MAX_IMAGE_BYTES", str(len(PNG_BYTES)))
    key = client_upload(pending_image, data)
    real_head = image_service.object_store.head_object

    def stale_head(object_key):
        head = real_head(object_key)
        return {**head, "ContentLength": len(PNG_BYTES)}

    monkeypatch.setattr(image_service.object_store, "head_object", stale_head)
    assert outcome(process(context, key)) == ["rejected"]
    reason = fetch_image(context, pending_image["image"]["imageId"])["rejectionReason"]
    assert reason == f"Uploaded {len(data)} bytes; the limit is {len(PNG_BYTES)} bytes"


def test_hashing_stops_as_soon_as_the_stream_passes_the_limit(
    aws, context, pending_image, monkeypatch
):
    """An object that grows past the limit mid-read is not hashed to the end."""
    from src.services import image_service

    monkeypatch.setattr(image_service, "_HASH_CHUNK_BYTES", 64)
    data = PNG_BYTES + b"\x00" * 10_000
    monkeypatch.setenv("MAX_IMAGE_BYTES", "100")
    key = client_upload(pending_image, data)
    real_head = image_service.object_store.head_object
    monkeypatch.setattr(
        image_service.object_store,
        "head_object",
        lambda object_key: {**real_head(object_key), "ContentLength": 50},
    )

    chunks_read = []
    real_open = image_service.object_store.open_object

    def counting_open(object_key):
        body = real_open(object_key)
        original = body.iter_chunks

        def iter_chunks(chunk_size):
            for chunk in original(chunk_size=chunk_size):
                chunks_read.append(len(chunk))
                yield chunk

        body.iter_chunks = iter_chunks
        return body

    monkeypatch.setattr(image_service.object_store, "open_object", counting_open)
    assert outcome(process(context, key)) == ["rejected"]
    assert len(chunks_read) == 2
