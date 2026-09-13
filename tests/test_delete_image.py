"""DELETE /images/{imageId}"""

from src.common import config
from src.handlers import delete_image, get_image
from tests.conftest import api_event, body_of, s3_keys


def path(image_id):
    return {"imageId": image_id}


def test_deletes_metadata_and_object(aws, context, stored_image):
    response = delete_image.handler(
        api_event("DELETE", path_parameters=path(stored_image["imageId"])), context
    )
    assert response["statusCode"] == 204
    assert response["body"] == ""
    assert s3_keys() == []
    assert (
        get_image.handler(
            api_event("GET", path_parameters=path(stored_image["imageId"])), context
        )["statusCode"]
        == 404
    )


def test_deleting_a_missing_image_returns_404(aws, context):
    response = delete_image.handler(
        api_event("DELETE", path_parameters=path("does-not-exist")), context
    )
    assert response["statusCode"] == 404
    assert body_of(response)["code"] == "NotFound"


def test_delete_is_not_silently_idempotent(aws, context, stored_image):
    """The second delete must report 404 rather than pretend it did work."""
    first = delete_image.handler(
        api_event("DELETE", path_parameters=path(stored_image["imageId"])), context
    )
    second = delete_image.handler(
        api_event("DELETE", path_parameters=path(stored_image["imageId"])), context
    )
    assert (first["statusCode"], second["statusCode"]) == (204, 404)


def test_missing_path_parameter_returns_400(aws, context):
    assert delete_image.handler(api_event("DELETE"), context)["statusCode"] == 400


def test_orphaned_object_does_not_fail_the_delete(aws, context, stored_image, monkeypatch):
    """S3 cleanup is best effort: the row is the source of truth and it is gone."""
    from src.services import object_store

    def explode(_key):
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(object_store, "delete_object", explode)
    response = delete_image.handler(
        api_event("DELETE", path_parameters=path(stored_image["imageId"])), context
    )

    assert response["statusCode"] == 204
    assert s3_keys() == ["images/user-alice/{}.png".format(stored_image["imageId"])]
    table = aws.resource("dynamodb", region_name="us-east-1").Table(config.table_name())
    assert "Item" not in table.get_item(Key={"imageId": stored_image["imageId"]})


def test_deleting_one_image_leaves_the_others(aws, context, upload_payload, stored_image):
    from tests.conftest import PNG_BYTES, complete_upload

    other = complete_upload(context, upload_payload(filename="other.png"), PNG_BYTES)
    delete_image.handler(
        api_event("DELETE", path_parameters=path(stored_image["imageId"])), context
    )
    assert s3_keys() == [f"images/user-alice/{other['imageId']}.png"]
    assert (
        get_image.handler(api_event("GET", path_parameters=path(other["imageId"])), context)[
            "statusCode"
        ]
        == 200
    )


def test_deletes_a_pending_image_that_has_no_object_yet(aws, context, pending_image):
    image_id = pending_image["image"]["imageId"]
    response = delete_image.handler(api_event("DELETE", path_parameters=path(image_id)), context)

    assert response["statusCode"] == 204
    assert get_image.handler(api_event("GET", path_parameters=path(image_id)), context)[
        "statusCode"
    ] == 404


def test_deletes_a_rejected_image(aws, context, upload_payload):
    from tests.conftest import PDF_BYTES, client_upload, process, register

    registered = register(context, upload_payload())
    process(context, client_upload(registered, PDF_BYTES))
    image_id = registered["image"]["imageId"]

    response = delete_image.handler(api_event("DELETE", path_parameters=path(image_id)), context)
    assert response["statusCode"] == 204


def test_upload_arriving_after_delete_is_cleaned_up(aws, context, pending_image):
    """Delete wins: a late upload for a deleted image does not resurrect it."""
    from tests.conftest import PNG_BYTES, client_upload, process

    image_id = pending_image["image"]["imageId"]
    delete_image.handler(api_event("DELETE", path_parameters=path(image_id)), context)
    key = client_upload(pending_image, PNG_BYTES)
    process(context, key)

    assert s3_keys() == []
    assert get_image.handler(api_event("GET", path_parameters=path(image_id)), context)[
        "statusCode"
    ] == 404
