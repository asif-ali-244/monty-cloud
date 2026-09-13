"""Downstream AWS failures must surface as clean HTTP responses, never stack traces."""

import pytest
from botocore.exceptions import ClientError

from src.common import config
from src.common.errors import ConflictError, NotFoundError, StorageError
from src.handlers import delete_image, download_image, get_image, list_images, upload_image
from src.services import image_service, object_store
from src.services import metadata_repository as repo
from tests.conftest import api_event, body_of


def client_error(code, operation="Operation"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class Exploding:
    """Stands in for a boto client or table and fails every call."""

    def __init__(self, error):
        self._error = error

    def __getattr__(self, _name):
        def _raise(*_args, **_kwargs):
            raise self._error

        return _raise


@pytest.fixture
def broken_table(monkeypatch):
    def _install(code="InternalServerError"):
        monkeypatch.setattr(
            config, "dynamodb_table", lambda: Exploding(client_error(code))
        )

    return _install


@pytest.fixture
def broken_s3(monkeypatch):
    def _install(code="InternalError"):
        monkeypatch.setattr(config, "s3_client", lambda: Exploding(client_error(code)))
        monkeypatch.setattr(
            config, "s3_signing_client", lambda: Exploding(client_error(code))
        )

    return _install


class TestRepositoryErrorMapping:
    def test_duplicate_id_is_a_conflict(self, aws, broken_table):
        broken_table("ConditionalCheckFailedException")
        with pytest.raises(ConflictError):
            repo.put_new({"imageId": "dupe"})

    def test_write_failure_becomes_a_storage_error(self, aws, broken_table):
        broken_table()
        with pytest.raises(StorageError):
            repo.put_new({"imageId": "x"})

    def test_read_failure_becomes_a_storage_error(self, aws, broken_table):
        broken_table()
        with pytest.raises(StorageError):
            repo.get("x")

    def test_delete_failure_becomes_a_storage_error(self, aws, broken_table):
        broken_table()
        with pytest.raises(StorageError):
            repo.delete("x")

    def test_missing_row_on_delete_is_reported_as_absent(self, aws, broken_table):
        broken_table("ConditionalCheckFailedException")
        assert repo.delete("x") is None

    def test_list_failure_becomes_a_storage_error(self, aws, broken_table):
        broken_table()
        with pytest.raises(StorageError):
            repo.list_images()


class TestObjectStoreErrorMapping:
    def test_presigned_post_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.presigned_post("k", "image/png", 70, 60)

    def test_delete_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.delete_object("k")

    @pytest.mark.parametrize("operation", ["head_object", "open_object"])
    def test_read_failure(self, aws, broken_s3, operation):
        broken_s3()
        with pytest.raises(StorageError):
            getattr(object_store, operation)("k")

    @pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
    @pytest.mark.parametrize("operation", ["head_object", "open_object"])
    def test_missing_object_is_none_not_an_error(self, aws, broken_s3, operation, code):
        """The processor treats a vanished object as a normal outcome, not a retry."""
        broken_s3(code)
        assert getattr(object_store, operation)("k") is None

    def test_presign_get_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.presigned_get_url("k", "f.png", "image/png", 60)

    def test_streams_stored_bytes_when_healthy(self, aws, stored_image):
        body = object_store.open_object(repo.get(stored_image["imageId"])["s3Key"])
        try:
            assert body.read().startswith(b"\x89PNG")
        finally:
            body.close()


class TestStateTransitionErrorMapping:
    @pytest.mark.parametrize("transition", ["mark_ready", "mark_rejected"])
    def test_condition_failure_means_not_pending(self, aws, broken_table, transition):
        broken_table("ConditionalCheckFailedException")
        args = ("x", "k", 1, "sum", "now") if transition == "mark_ready" else ("x", "k", "why", 0)
        assert getattr(repo, transition)(*args) is None

    @pytest.mark.parametrize("transition", ["mark_ready", "mark_rejected"])
    def test_other_failures_become_storage_errors(self, aws, broken_table, transition):
        broken_table()
        args = ("x", "k", 1, "sum", "now") if transition == "mark_ready" else ("x", "k", "why", 0)
        with pytest.raises(StorageError):
            getattr(repo, transition)(*args)


class TestHandlerResponsesUnderFailure:
    def test_upload_returns_502(self, aws, context, upload_payload, broken_s3):
        broken_s3()
        response = upload_image.handler(api_event("POST", body=upload_payload()), context)
        assert response["statusCode"] == 502
        assert body_of(response)["code"] == "StorageError"

    def test_list_returns_502(self, aws, context, broken_table):
        broken_table()
        assert list_images.handler(api_event("GET"), context)["statusCode"] == 502

    def test_get_returns_502(self, aws, context, broken_table):
        broken_table()
        response = get_image.handler(
            api_event("GET", path_parameters={"imageId": "x"}), context
        )
        assert response["statusCode"] == 502

    def test_download_returns_502(self, aws, context, stored_image, broken_s3):
        broken_s3()
        response = download_image.handler(
            api_event("GET", path_parameters={"imageId": stored_image["imageId"]}), context
        )
        assert response["statusCode"] == 502

    def test_delete_returns_502(self, aws, context, broken_table):
        broken_table()
        response = delete_image.handler(
            api_event("DELETE", path_parameters={"imageId": "x"}), context
        )
        assert response["statusCode"] == 502


def test_failed_rejection_keeps_the_object_so_a_retry_can_finish(
    aws, context, upload_payload, monkeypatch
):
    """Reject marks the row before deleting the object, never the other way round.

    If the row update fails, the object must survive: the Lambda retry needs it
    to reach the same verdict, and deleting first would strand a pending row
    whose bytes are already gone.
    """
    from tests.conftest import PDF_BYTES, client_upload, process, register, s3_keys

    registered = register(context, upload_payload(sizeBytes=len(PDF_BYTES)))
    key = client_upload(registered, PDF_BYTES)

    def unavailable(*_args):
        raise StorageError("Could not update image metadata")

    real_mark_rejected = repo.mark_rejected
    monkeypatch.setattr(repo, "mark_rejected", unavailable)
    with pytest.raises(StorageError):
        process(context, key)
    assert s3_keys() == [key]

    # DynamoDB recovers; the Lambda retry delivers the same event again.
    monkeypatch.setattr(repo, "mark_rejected", real_mark_rejected)
    assert process(context, key)["processed"][0]["outcome"] == "rejected"
    assert s3_keys() == []


def test_service_layer_is_transport_agnostic(aws, upload_payload):
    """image_service knows nothing about API Gateway: it raises, it does not respond."""
    with pytest.raises(NotFoundError):
        image_service.get_image("missing")
    with pytest.raises(NotFoundError):
        image_service.delete_image("missing")
    with pytest.raises(NotFoundError):
        image_service.build_download("missing")
    assert image_service.process_uploaded_object("not/an/upload/key") == "ignored"
