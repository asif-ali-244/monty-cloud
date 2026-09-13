"""GET /images - filtering, ordering and pagination."""

import pytest

from src.common import config
from src.handlers import list_images
from tests.conftest import api_event, body_of


@pytest.fixture
def catalogue(aws):
    """A small, deterministic corpus written straight to DynamoDB.

    Five ready images, plus a pending and a rejected one that match every filter
    below and must never be listed. They are written the way the service writes
    them: without uploadedAt, which keeps them out of the sparse user index.
    """
    table = aws.resource("dynamodb", region_name="us-east-1").Table(config.table_name())
    #    id       user     filename     contentType   tags                uploadedAt
    rows = [
        ("img-1", "alice", "beach.png", "image/png", ["beach", "summer"], "2024-01-01"),
        ("img-2", "alice", "dog.jpg", "image/jpeg", ["pets"], "2024-02-01"),
        ("img-3", "alice", "cat.jpg", "image/jpeg", ["pets", "cute"], "2024-03-01"),
        ("img-4", "bob", "beach.jpg", "image/jpeg", ["beach"], "2024-02-15"),
        ("img-5", "bob", "city.png", "image/png", [], "2024-04-01"),
    ]
    for image_id, user, filename, content_type, tags, day in rows:
        uploaded_at = f"{day}T10:00:00.000Z"
        table.put_item(
            Item={
                "imageId": image_id,
                "userId": user,
                "filename": filename,
                "filenameLower": filename.lower(),
                "contentType": content_type,
                "tags": tags,
                "sizeBytes": 100,
                "status": "ready",
                "createdAt": uploaded_at,
                "uploadedAt": uploaded_at,
                "s3Key": f"images/{user}/{image_id}",
                "s3Bucket": config.bucket_name(),
            }
        )
    for image_id, status in (("img-pending", "pending"), ("img-rejected", "rejected")):
        table.put_item(
            Item={
                "imageId": image_id,
                "userId": "alice",
                "filename": "beach-unfinished.png",
                "filenameLower": "beach-unfinished.png",
                "contentType": "image/png",
                "tags": ["beach", "pets"],
                "sizeBytes": 100,
                "status": status,
                "createdAt": "2024-02-10T10:00:00.000Z",
                "expiresAt": 4102444800,
                "s3Key": f"images/alice/{image_id}",
                "s3Bucket": config.bucket_name(),
            }
        )
    return table


def ids(response):
    return [item["imageId"] for item in body_of(response)["items"]]


def test_lists_everything_when_unfiltered(catalogue, context):
    response = list_images.handler(api_event("GET"), context)
    assert response["statusCode"] == 200
    assert sorted(ids(response)) == ["img-1", "img-2", "img-3", "img-4", "img-5"]
    assert body_of(response)["count"] == 5


def test_filters_by_user(catalogue, context):
    response = list_images.handler(api_event("GET", query={"userId": "alice"}), context)
    assert sorted(ids(response)) == ["img-1", "img-2", "img-3"]


def test_user_results_are_newest_first(catalogue, context):
    response = list_images.handler(api_event("GET", query={"userId": "alice"}), context)
    assert ids(response) == ["img-3", "img-2", "img-1"]


def test_unfiltered_results_are_sorted_newest_first(catalogue, context):
    response = list_images.handler(api_event("GET"), context)
    uploaded = [item["uploadedAt"] for item in body_of(response)["items"]]
    assert uploaded == sorted(uploaded, reverse=True)


def test_filters_by_tag(catalogue, context):
    response = list_images.handler(api_event("GET", query={"tag": "pets"}), context)
    assert sorted(ids(response)) == ["img-2", "img-3"]


def test_filters_by_content_type(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"contentType": "image/png"}), context
    )
    assert sorted(ids(response)) == ["img-1", "img-5"]


def test_combines_user_and_tag_filters(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"userId": "alice", "tag": "beach"}), context
    )
    assert ids(response) == ["img-1"]


def test_filters_by_date_range_within_a_user(catalogue, context):
    """Dates become a key condition on the GSI, not a post-filter."""
    response = list_images.handler(
        api_event(
            "GET",
            query={
                "userId": "alice",
                "uploadedFrom": "2024-01-15T00:00:00Z",
                "uploadedTo": "2024-02-15T00:00:00Z",
            },
        ),
        context,
    )
    assert ids(response) == ["img-2"]


def test_filters_by_date_range_across_users(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"uploadedFrom": "2024-03-01T00:00:00Z"}), context
    )
    assert sorted(ids(response)) == ["img-3", "img-5"]


def test_open_ended_upper_bound(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"userId": "alice", "uploadedTo": "2024-01-31T00:00:00Z"}),
        context,
    )
    assert ids(response) == ["img-1"]


def test_filters_by_partial_filename(catalogue, context):
    response = list_images.handler(api_event("GET", query={"filename": "BEACH"}), context)
    assert sorted(ids(response)) == ["img-1", "img-4"]


def test_tag_filter_is_case_insensitive(catalogue, context):
    response = list_images.handler(api_event("GET", query={"tag": "PETS"}), context)
    assert sorted(ids(response)) == ["img-2", "img-3"]


def test_no_matches_returns_empty_page(catalogue, context):
    response = list_images.handler(api_event("GET", query={"tag": "nonexistent"}), context)
    assert response["statusCode"] == 200
    assert body_of(response) == {"items": [], "count": 0, "nextToken": None}


def test_pagination_walks_every_item_exactly_once(catalogue, context):
    seen, token, pages = [], None, 0
    while True:
        query = {"userId": "alice", "limit": "1"}
        if token:
            query["nextToken"] = token
        body = body_of(list_images.handler(api_event("GET", query=query), context))
        seen.extend(item["imageId"] for item in body["items"])
        token = body["nextToken"]
        pages += 1
        if not token or pages > 10:
            break
    assert seen == ["img-3", "img-2", "img-1"]
    assert pages == 3


def test_limit_is_capped_at_the_maximum(catalogue, context):
    response = list_images.handler(api_event("GET", query={"limit": "9999"}), context)
    assert response["statusCode"] == 200
    assert body_of(response)["count"] <= config.MAX_PAGE_SIZE


@pytest.mark.parametrize("limit", ["0", "-3", "abc"])
def test_rejects_invalid_limit(catalogue, context, limit):
    response = list_images.handler(api_event("GET", query={"limit": limit}), context)
    assert response["statusCode"] == 400


def test_rejects_malformed_pagination_token(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"nextToken": "!!!not-a-token!!!"}), context
    )
    assert response["statusCode"] == 400
    assert "nextToken" in body_of(response)["error"]


@pytest.mark.parametrize("field", ["uploadedFrom", "uploadedTo"])
def test_rejects_malformed_timestamp(catalogue, context, field):
    response = list_images.handler(api_event("GET", query={field: "last tuesday"}), context)
    assert response["statusCode"] == 400
    assert field in body_of(response)["error"]


def test_rejects_unsupported_content_type_filter(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"contentType": "application/zip"}), context
    )
    assert response["statusCode"] == 415


def test_storage_internals_are_not_exposed(catalogue, context):
    body = body_of(list_images.handler(api_event("GET"), context))
    assert all("s3Key" not in item for item in body["items"])


def test_missing_query_string_is_treated_as_no_filters(catalogue, context):
    event = api_event("GET")
    event["queryStringParameters"] = None
    assert list_images.handler(event, context)["statusCode"] == 200


def test_scan_path_with_only_an_upper_bound(catalogue, context):
    response = list_images.handler(
        api_event("GET", query={"uploadedTo": "2024-02-01T00:00:00Z"}), context
    )
    assert sorted(ids(response)) == ["img-1"]


def test_scan_path_with_a_bounded_window(catalogue, context):
    response = list_images.handler(
        api_event(
            "GET",
            query={
                "uploadedFrom": "2024-02-01T00:00:00Z",
                "uploadedTo": "2024-03-01T23:59:59Z",
            },
        ),
        context,
    )
    assert sorted(ids(response)) == ["img-2", "img-3", "img-4"]


def test_scan_path_combines_a_date_window_with_attribute_filters(catalogue, context):
    response = list_images.handler(
        api_event(
            "GET",
            query={
                "uploadedFrom": "2024-02-01T00:00:00Z",
                "contentType": "image/jpeg",
                "tag": "beach",
            },
        ),
        context,
    )
    assert sorted(ids(response)) == ["img-4"]


def test_three_filters_at_once_within_a_user(catalogue, context):
    response = list_images.handler(
        api_event(
            "GET",
            query={
                "userId": "alice",
                "contentType": "image/jpeg",
                "tag": "pets",
                "filename": "cat",
            },
        ),
        context,
    )
    assert ids(response) == ["img-3"]


def test_user_query_with_only_a_lower_bound(catalogue, context):
    response = list_images.handler(
        api_event(
            "GET", query={"userId": "alice", "uploadedFrom": "2024-02-15T00:00:00Z"}
        ),
        context,
    )
    assert ids(response) == ["img-3"]


def test_a_filtered_page_can_be_empty_while_matches_remain(catalogue, context):
    """`limit` bounds rows READ, not rows RETURNED.

    DynamoDB applies FilterExpression after the read, so a page whose rows all
    fail the filter comes back empty with a non-null nextToken. A client that
    stops at the first empty page silently loses results, which is why the
    documented contract is "follow the cursor until nextToken is null".
    """
    first = body_of(
        list_images.handler(
            api_event("GET", query={"userId": "alice", "tag": "beach", "limit": "2"}), context
        )
    )
    # img-3 and img-2 are read and both filtered out; img-1 is the only match.
    assert first["items"] == []
    assert first["count"] == 0
    assert first["nextToken"] is not None

    second = body_of(
        list_images.handler(
            api_event(
                "GET",
                query={
                    "userId": "alice",
                    "tag": "beach",
                    "limit": "2",
                    "nextToken": first["nextToken"],
                },
            ),
            context,
        )
    )
    assert ids_of(second) == ["img-1"]
    assert second["nextToken"] is None


def ids_of(body):
    return [item["imageId"] for item in body["items"]]


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"userId": "alice"},
        {"tag": "beach"},
        {"userId": "alice", "tag": "pets"},
        {"filename": "unfinished"},
        {"uploadedFrom": "2024-01-01T00:00:00Z"},
    ],
)
def test_pending_and_rejected_images_are_never_listed(catalogue, context, query):
    listed = ids(list_images.handler(api_event("GET", query=query), context))
    assert "img-pending" not in listed
    assert "img-rejected" not in listed


def test_listed_items_are_all_ready(catalogue, context):
    body = body_of(list_images.handler(api_event("GET"), context))
    assert {item["status"] for item in body["items"]} == {"ready"}


def test_an_image_appears_in_listings_only_once_its_upload_is_verified(
    aws, context, upload_payload
):
    """End to end through the real handlers, not hand-written rows."""
    from tests.conftest import PNG_BYTES, client_upload, process, register

    registered = register(context, upload_payload(), user_id="carol")
    image_id = registered["image"]["imageId"]
    by_user = api_event("GET", query={"userId": "carol"})
    everyone = api_event("GET")

    assert ids(list_images.handler(by_user, context)) == []
    assert ids(list_images.handler(everyone, context)) == []

    process(context, client_upload(registered, PNG_BYTES))

    assert ids(list_images.handler(by_user, context)) == [image_id]
    assert ids(list_images.handler(everyone, context)) == [image_id]
