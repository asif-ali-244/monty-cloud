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
    def test_put_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.put_object("k", b"data", "image/png")

    def test_delete_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.delete_object("k")

    def test_get_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.get_object_bytes("k")

    def test_missing_object_is_a_404_not_a_502(self, aws, broken_s3):
        broken_s3("NoSuchKey")
        with pytest.raises(NotFoundError):
            object_store.get_object_bytes("k")

    def test_presign_failure(self, aws, broken_s3):
        broken_s3()
        with pytest.raises(StorageError):
            object_store.presigned_get_url("k", "f.png", "image/png", 60)

    def test_reads_stored_bytes_when_healthy(self, aws, stored_image):
        item = repo.get(stored_image["imageId"])
        assert object_store.get_object_bytes(item["s3Key"]).startswith(b"\x89PNG")


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


def test_compensating_delete_failure_still_reports_the_original_error(
    aws, context, upload_payload, monkeypatch
):
    """Both the metadata write and the rollback fail; the caller still gets one 500."""
    monkeypatch.setattr(
        repo, "put_new", lambda _item: (_ for _ in ()).throw(RuntimeError("ddb down"))
    )
    monkeypatch.setattr(
        object_store,
        "delete_object",
        lambda _key: (_ for _ in ()).throw(RuntimeError("s3 down")),
    )
    response = upload_image.handler(api_event("POST", body=upload_payload()), context)
    assert response["statusCode"] == 500


def test_service_layer_is_transport_agnostic(aws, upload_payload):
    """image_service knows nothing about API Gateway: it raises, it does not respond."""
    with pytest.raises(NotFoundError):
        image_service.get_image("missing")
    with pytest.raises(NotFoundError):
        image_service.delete_image("missing")
    with pytest.raises(NotFoundError):
        image_service.build_download("missing")
