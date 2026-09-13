"""Multi-user behaviour and the full register/upload/process/read/delete round trip.

The brief calls for a service many people use at once, so these cases exercise
the properties that concurrency depends on: unique keys, per-user isolation,
and no cross-request state held in a warm Lambda container.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor

from src.handlers import delete_image, download_image, get_image, list_images
from tests.conftest import (
    PNG_BYTES,
    api_event,
    body_of,
    client_upload,
    complete_upload,
    process,
    register,
    s3_keys,
)


def test_full_lifecycle(aws, context, upload_payload):
    registered = register(context, upload_payload())
    image_id = registered["image"]["imageId"]
    path = {"imageId": image_id}

    # 1. Registered: visible by id as pending, but not listed or downloadable.
    pending = body_of(get_image.handler(api_event("GET", path_parameters=path), context))
    assert pending["status"] == "pending"
    assert body_of(
        list_images.handler(api_event("GET", query={"userId": "user-alice"}), context)
    )["items"] == []
    assert (
        download_image.handler(api_event("GET", path_parameters=path), context)["statusCode"]
        == 409
    )

    # 2. The client uploads straight to S3, and S3 notifies the processor.
    key = client_upload(registered, PNG_BYTES)
    process(context, key)

    # 3. Ready: listed, downloadable, with verified size and checksum.
    ready = body_of(get_image.handler(api_event("GET", path_parameters=path), context))
    assert ready["status"] == "ready"
    assert ready["checksumSha256"] == hashlib.sha256(PNG_BYTES).hexdigest()

    listed = body_of(
        list_images.handler(api_event("GET", query={"userId": "user-alice"}), context)
    )
    assert [item["imageId"] for item in listed["items"]] == [image_id]
    assert (
        download_image.handler(api_event("GET", path_parameters=path), context)["statusCode"]
        == 302
    )

    # 4. Deleted: gone from every endpoint and from S3.
    assert (
        delete_image.handler(api_event("DELETE", path_parameters=path), context)["statusCode"]
        == 204
    )
    assert body_of(
        list_images.handler(api_event("GET", query={"userId": "user-alice"}), context)
    )["items"] == []
    assert s3_keys() == []


def test_same_filename_from_different_users_does_not_collide(aws, context, upload_payload):
    alice = complete_upload(context, upload_payload(), PNG_BYTES, user_id="alice")
    bob = complete_upload(context, upload_payload(), PNG_BYTES, user_id="bob")

    assert alice["imageId"] != bob["imageId"]
    assert sorted(s3_keys()) == sorted(
        [f"images/alice/{alice['imageId']}.png", f"images/bob/{bob['imageId']}.png"]
    )


def test_same_user_uploading_the_same_file_twice_keeps_both(aws, context, upload_payload):
    first = complete_upload(context, upload_payload(), PNG_BYTES)
    second = complete_upload(context, upload_payload(), PNG_BYTES)

    assert first["imageId"] != second["imageId"]
    assert len(s3_keys()) == 2
    assert first["checksumSha256"] == second["checksumSha256"]


def test_concurrent_registrations_get_distinct_ids_and_keys(aws, context, upload_payload):
    users = [f"user-{i}" for i in range(12)]

    def register_as(user):
        return register(context, upload_payload(), user_id=user)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(register_as, users))

    assert len({r["image"]["imageId"] for r in results}) == len(users)
    assert len({r["upload"]["fields"]["key"] for r in results}) == len(users)
    for user, result in zip(users, results):
        assert result["image"]["userId"] == user
        assert result["upload"]["fields"]["key"].startswith(f"images/{user}/")


def test_concurrent_processing_of_many_uploads(aws, context, upload_payload):
    registrations = [
        register(context, upload_payload(filename=f"f{i}.png"), user_id=f"user-{i}")
        for i in range(10)
    ]
    keys = [client_upload(r, PNG_BYTES) for r in registrations]

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda key: process(context, key), keys))

    assert [o["processed"][0]["outcome"] for o in outcomes] == ["ready"] * 10
    listed = body_of(list_images.handler(api_event("GET", query={"limit": "100"}), context))
    assert listed["count"] == 10


def test_one_users_listing_never_leaks_another_users_images(aws, context, upload_payload):
    complete_upload(context, upload_payload(), PNG_BYTES, user_id="alice")
    complete_upload(context, upload_payload(), PNG_BYTES, user_id="bob")

    alice = body_of(list_images.handler(api_event("GET", query={"userId": "alice"}), context))
    assert len(alice["items"]) == 1
    assert alice["items"][0]["userId"] == "alice"


def test_warm_container_holds_no_request_state(aws, context, upload_payload):
    """Sequential invocations of the same module must not see each other."""
    first = register(context, upload_payload(filename="one.png"), user_id="alice")["image"]
    second = register(context, upload_payload(filename="two.png"), user_id="bob")["image"]
    assert (first["filename"], first["userId"]) == ("one.png", "alice")
    assert (second["filename"], second["userId"]) == ("two.png", "bob")


def test_pagination_is_stable_while_the_table_is_written_to(aws, context, upload_payload):
    for index in range(5):
        complete_upload(context, upload_payload(filename=f"f{index}.png"), PNG_BYTES, "alice")

    page = body_of(
        list_images.handler(api_event("GET", query={"userId": "alice", "limit": "2"}), context)
    )
    assert len(page["items"]) == 2 and page["nextToken"]

    complete_upload(context, upload_payload(filename="late.png"), PNG_BYTES, "alice")

    rest = body_of(
        list_images.handler(
            api_event(
                "GET",
                query={"userId": "alice", "limit": "10", "nextToken": page["nextToken"]},
            ),
            context,
        )
    )
    seen = [item["imageId"] for item in page["items"] + rest["items"]]
    assert len(seen) == len(set(seen))
