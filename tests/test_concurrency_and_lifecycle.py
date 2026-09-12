"""Multi-user behaviour and the full create/read/list/delete round trip.

The brief calls for a service many people use at once, so these cases exercise
the properties that concurrency depends on: unique keys, per-user isolation,
and no cross-request state held in a warm Lambda container.
"""

from concurrent.futures import ThreadPoolExecutor

from src.handlers import delete_image, download_image, get_image, list_images, upload_image
from tests.conftest import api_event, body_of, s3_keys


def test_full_lifecycle(aws, context, upload_payload):
    created = body_of(
        upload_image.handler(api_event("POST", body=upload_payload()), context)
    )
    image_id = created["imageId"]

    fetched = body_of(
        get_image.handler(api_event("GET", path_parameters={"imageId": image_id}), context)
    )
    assert fetched == created

    listed = body_of(
        list_images.handler(api_event("GET", query={"userId": "user-alice"}), context)
    )
    assert [item["imageId"] for item in listed["items"]] == [image_id]

    assert (
        download_image.handler(
            api_event("GET", path_parameters={"imageId": image_id}), context
        )["statusCode"]
        == 302
    )

    assert (
        delete_image.handler(
            api_event("DELETE", path_parameters={"imageId": image_id}), context
        )["statusCode"]
        == 204
    )

    empty = body_of(
        list_images.handler(api_event("GET", query={"userId": "user-alice"}), context)
    )
    assert empty["items"] == []
    assert s3_keys() == []


def test_same_filename_from_different_users_does_not_collide(aws, context, upload_payload):
    alice = body_of(
        upload_image.handler(
            api_event("POST", body=upload_payload(), user_id="alice"), context
        )
    )
    bob = body_of(
        upload_image.handler(api_event("POST", body=upload_payload(), user_id="bob"), context)
    )

    assert alice["imageId"] != bob["imageId"]
    assert sorted(s3_keys()) == sorted(
        [
            "images/alice/{}.png".format(alice["imageId"]),
            "images/bob/{}.png".format(bob["imageId"]),
        ]
    )


def test_same_user_uploading_the_same_file_twice_keeps_both(aws, context, upload_payload):
    first = body_of(upload_image.handler(api_event("POST", body=upload_payload()), context))
    second = body_of(upload_image.handler(api_event("POST", body=upload_payload()), context))

    assert first["imageId"] != second["imageId"]
    assert len(s3_keys()) == 2
    assert first["checksumSha256"] == second["checksumSha256"]


def test_concurrent_uploads_all_persist_with_distinct_ids(aws, context, upload_payload):
    users = [f"user-{i}" for i in range(12)]

    def upload(user):
        return body_of(
            upload_image.handler(
                api_event("POST", body=upload_payload(), user_id=user), context
            )
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(upload, users))

    ids = {item["imageId"] for item in results}
    assert len(ids) == len(users)
    assert len(s3_keys()) == len(users)
    for user, item in zip(users, results):
        assert item["userId"] == user


def test_one_users_listing_never_leaks_another_users_images(aws, context, upload_payload):
    upload_image.handler(api_event("POST", body=upload_payload(), user_id="alice"), context)
    upload_image.handler(api_event("POST", body=upload_payload(), user_id="bob"), context)

    alice = body_of(list_images.handler(api_event("GET", query={"userId": "alice"}), context))
    assert len(alice["items"]) == 1
    assert alice["items"][0]["userId"] == "alice"


def test_warm_container_holds_no_request_state(aws, context, upload_payload):
    """Sequential invocations of the same module must not see each other."""
    first = body_of(
        upload_image.handler(
            api_event("POST", body=upload_payload(filename="one.png"), user_id="alice"),
            context,
        )
    )
    second = body_of(
        upload_image.handler(
            api_event("POST", body=upload_payload(filename="two.png"), user_id="bob"), context
        )
    )
    assert (first["filename"], first["userId"]) == ("one.png", "alice")
    assert (second["filename"], second["userId"]) == ("two.png", "bob")


def test_pagination_is_stable_while_the_table_is_written_to(aws, context, upload_payload):
    for index in range(5):
        upload_image.handler(
            api_event(
                "POST", body=upload_payload(filename=f"f{index}.png"), user_id="alice"
            ),
            context,
        )

    page = body_of(
        list_images.handler(
            api_event("GET", query={"userId": "alice", "limit": "2"}), context
        )
    )
    assert len(page["items"]) == 2 and page["nextToken"]

    upload_image.handler(
        api_event("POST", body=upload_payload(filename="late.png"), user_id="alice"), context
    )

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
