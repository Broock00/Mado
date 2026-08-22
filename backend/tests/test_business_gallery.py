"""A business's own photographs and videos.

The gallery answers what a post history cannot: what the place is actually like.
So the tests here are mostly about the two things that make it trustworthy -
that the *file* decides what is stored rather than what the uploader called it,
and that the one authorization gate covers the new routes as well as the old.

`test_hardening.py` covers the container checks as units. These drive the whole
route, because a validator that is correct and never reached is the failure this
project has already had once.
"""

from __future__ import annotations

import io
import os
import uuid

import httpx
import pytest
from PIL import Image

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"


@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=60) as session:
        yield session


def account(client, who="owner") -> dict:
    auth, _ = account_with_email(client, who)
    return auth


def account_with_email(client, who="owner") -> tuple[dict, str]:
    """Register an explorer. Returns `(auth headers, email)`.

    The address is returned from here rather than read back off `/me`, which
    does not carry it - an invitation is addressed by email, so a test that
    invites somebody has to have kept it.
    """
    email = f"{who}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": who.title()},
    )
    assert response.status_code == 201, response.text
    token = response.json()["data"]["tokens"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}, email


def make_business(client, auth) -> dict:
    response = client.post(
        "/api/v1/me/account-type/business",
        headers=auth,
        json={
            "name": f"Test Gallery Hotel {uuid.uuid4().hex[:6]}",
            "businessType": "hotel",
            "description": "A place invented by the test suite.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["business"]


def photo(width: int = 800, height: int = 600) -> bytes:
    buffer = io.BytesIO()
    # Noise rather than a flat colour: two flat images of the same size are
    # byte-identical after re-encoding, and content-addressed storage would give
    # them one URL - so a test uploading "two photos" would silently have one.
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(
        buffer, format="JPEG"
    )
    return buffer.getvalue()


def clip(brand: bytes = b"isom") -> bytes:
    """An MP4 header, padded past the sniff window, with a unique tail."""
    return b"\x00\x00\x00\x18ftyp" + brand + os.urandom(8192)


def webm_clip() -> bytes:
    return b"\x1a\x45\xdf\xa3\x42\x82webm" + os.urandom(8192)


def upload(client, auth, business_id, name, data, content_type, caption=None):
    files = {"file": (name, data, content_type)}
    return client.post(
        f"/api/v1/businesses/{business_id}/gallery/upload",
        headers=auth,
        files=files,
        data={"caption": caption} if caption else None,
    )


# --- uploading ----------------------------------------------------------------


class TestUploading:
    def test_a_business_can_upload_a_photo(self, client):
        auth = account(client)
        business = make_business(client, auth)

        response = upload(
            client, auth, business["id"], "lobby.jpg", photo(), "image/jpeg", "The lobby"
        )
        assert response.status_code == 201, response.text
        item = response.json()["data"]

        assert item["kind"] == "image"
        assert item["caption"] == "The lobby"
        assert item["url"].endswith(".webp"), "everything is re-encoded to one format"
        # Known because Pillow decoded it. The client reserves space from these.
        assert item["width"] == 800
        assert item["height"] == 600

    def test_a_business_can_upload_a_video(self, client):
        auth = account(client)
        business = make_business(client, auth)

        response = upload(client, auth, business["id"], "tour.mp4", clip(), "video/mp4")
        assert response.status_code == 201, response.text
        item = response.json()["data"]

        assert item["kind"] == "video"
        assert item["contentType"] == "video/mp4"
        assert item["url"].endswith(".mp4")
        # Null, not zero and not a guess: nothing decodes video server-side, and
        # a made-up aspect ratio would make every vertical clip jump on load.
        assert item["width"] is None
        assert item["height"] is None

    def test_webm_is_accepted_too(self, client):
        auth = account(client)
        business = make_business(client, auth)
        response = upload(client, auth, business["id"], "tour.webm", webm_clip(), "video/webm")
        assert response.status_code == 201, response.text
        assert response.json()["data"]["contentType"] == "video/webm"

    def test_the_stored_kind_comes_from_the_bytes_not_the_name(self, client):
        """A photograph renamed `.mp4` and declared `video/mp4`.

        Both of those are written by the client. If either were believed, the
        file would be stored under an extension it is not - and served with a
        Content-Type that is a lie, which is exactly what content sniffing turns
        into stored XSS.
        """
        auth = account(client)
        business = make_business(client, auth)

        response = upload(client, auth, business["id"], "sneaky.mp4", photo(), "video/mp4")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "UNSUPPORTED_VIDEO"

    def test_a_video_declared_as_an_image_is_still_read_as_a_video(self, client):
        """The mirror of the case above, and the reason the bytes are sniffed
        before the declared type is consulted at all."""
        auth = account(client)
        business = make_business(client, auth)

        response = upload(client, auth, business["id"], "tour.jpg", clip(), "image/jpeg")
        assert response.status_code == 201, response.text
        assert response.json()["data"]["kind"] == "video"

    def test_quicktime_is_refused_with_a_reason(self, client):
        """Refused rather than stored: it plays in Safari and nowhere else."""
        auth = account(client)
        business = make_business(client, auth)

        response = upload(
            client, auth, business["id"], "iphone.mov", clip(brand=b"qt  "), "video/quicktime"
        )
        assert response.status_code == 422, response.text
        body = response.json()["error"]
        assert body["code"] == "UNSUPPORTED_VIDEO"
        # The error names the formats that work. "Upload failed" would leave the
        # uploader with a large file and nothing to do about it.
        assert "MP4" in body["message"] or "WebM" in body["message"]

    def test_a_file_that_is_neither_is_refused(self, client):
        auth = account(client)
        business = make_business(client, auth)
        response = upload(
            client, auth, business["id"], "run.sh", b"#!/bin/sh\nrm -rf /\n", "text/plain"
        )
        assert response.status_code == 422, response.text


# --- what the profile shows ----------------------------------------------------


class TestOnTheProfile:
    def test_uploads_reach_the_public_page(self, client):
        """Nobody has to be signed in, and no second request is needed."""
        auth = account(client)
        business = make_business(client, auth)
        upload(client, auth, business["id"], "room.jpg", photo(), "image/jpeg", "A room")

        page = client.get(f"/api/v1/businesses/by-slug/{business['slug']}")
        assert page.status_code == 200, page.text
        gallery = page.json()["data"]["gallery"]

        assert len(gallery) == 1
        assert gallery[0]["caption"] == "A room"

    def test_a_gallery_is_independent_of_what_is_on(self, client):
        """The point of the tab. A business with nothing published still has
        somewhere to show what the place is like."""
        auth = account(client)
        business = make_business(client, auth)
        upload(client, auth, business["id"], "bar.jpg", photo(), "image/jpeg")

        body = client.get(f"/api/v1/businesses/by-slug/{business['slug']}").json()["data"]
        assert body["listings"] == []
        assert len(body["gallery"]) == 1

    def test_order_is_the_order_they_were_added(self, client):
        auth = account(client)
        business = make_business(client, auth)
        for index in range(3):
            response = upload(
                client, auth, business["id"], f"{index}.jpg", photo(), "image/jpeg", f"#{index}"
            )
            assert response.status_code == 201, response.text

        gallery = client.get(
            f"/api/v1/businesses/by-slug/{business['slug']}"
        ).json()["data"]["gallery"]
        assert [item["caption"] for item in gallery] == ["#0", "#1", "#2"]

    def test_removing_takes_it_off_the_profile(self, client):
        auth = account(client)
        business = make_business(client, auth)
        item = upload(
            client, auth, business["id"], "gone.jpg", photo(), "image/jpeg"
        ).json()["data"]

        deleted = client.delete(
            f"/api/v1/businesses/{business['id']}/gallery/{item['id']}", headers=auth
        )
        assert deleted.status_code == 204, deleted.text

        page = client.get(f"/api/v1/businesses/by-slug/{business['slug']}").json()["data"]
        assert page["gallery"] == []


# --- who may touch it ----------------------------------------------------------


class TestPermission:
    def test_a_stranger_cannot_upload(self, client):
        """404 rather than 403 - confirming the business exists to somebody with
        no relationship to it is itself a disclosure."""
        owner = account(client, "owner")
        business = make_business(client, owner)
        stranger = account(client, "stranger")

        response = upload(client, stranger, business["id"], "x.jpg", photo(), "image/jpeg")
        assert response.status_code == 404, response.text

    def test_a_stranger_cannot_delete(self, client):
        owner = account(client, "owner")
        business = make_business(client, owner)
        item = upload(
            client, owner, business["id"], "keep.jpg", photo(), "image/jpeg"
        ).json()["data"]

        stranger = account(client, "stranger")
        response = client.delete(
            f"/api/v1/businesses/{business['id']}/gallery/{item['id']}", headers=stranger
        )
        assert response.status_code == 404, response.text

    def test_an_editor_cannot_change_the_gallery(self, client):
        """Gated on `profile:edit`, which an editor does not hold. The gallery
        *is* the profile - an editor writes posts, an administrator decides how
        the business presents itself."""
        owner = account(client, "owner")
        business = make_business(client, owner)

        editor, email = account_with_email(client, "editor")
        invitation = client.post(
            f"/api/v1/businesses/{business['id']}/members",
            headers=owner,
            json={"email": email, "role": "editor"},
        )
        assert invitation.status_code == 201, invitation.text
        accepted = client.post(
            f"/api/v1/me/business-invitations/{invitation.json()['data']['id']}/accept",
            headers=editor,
        )
        assert accepted.status_code == 200, accepted.text

        # They can see it - `profile:view` - which is what makes 403 the right
        # answer here rather than the 404 a stranger gets.
        assert (
            client.get(
                f"/api/v1/businesses/{business['id']}/gallery", headers=editor
            ).status_code
            == 200
        )
        response = upload(client, editor, business["id"], "x.jpg", photo(), "image/jpeg")
        assert response.status_code == 403, response.text

    def test_one_business_cannot_delete_another_s_media(self, client):
        """The item id alone is not authority. Without the ownership check on the
        row, a caller who manages *any* business could delete from every one."""
        first_owner = account(client, "first")
        first = make_business(client, first_owner)
        item = upload(
            client, first_owner, first["id"], "theirs.jpg", photo(), "image/jpeg"
        ).json()["data"]

        second_owner = account(client, "second")
        second = make_business(client, second_owner)

        response = client.delete(
            f"/api/v1/businesses/{second['id']}/gallery/{item['id']}", headers=second_owner
        )
        assert response.status_code == 404, response.text

        # Still there.
        page = client.get(f"/api/v1/businesses/by-slug/{first['slug']}").json()["data"]
        assert len(page["gallery"]) == 1

    def test_a_personal_account_has_no_gallery(self, client):
        """An individual has no public profile page, so items added to one would
        be write-only. Refused rather than stored where nothing renders them."""
        auth = account(client)
        personal = client.get("/api/v1/posts/me", headers=auth).json()["data"]

        response = upload(client, auth, personal["id"], "x.jpg", photo(), "image/jpeg")
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "NOT_A_BUSINESS"


# --- serving -------------------------------------------------------------------


class TestServing:
    def test_an_uploaded_file_is_served_as_what_it_is(self, client):
        """The stored extension came from our own verifier, so the Content-Type
        is honest - and `nosniff` stops a browser second-guessing it, which is
        what turns a file crafted to read as both video and HTML into stored XSS
        on our own origin."""
        auth = account(client)
        business = make_business(client, auth)
        item = upload(
            client, auth, business["id"], "tour.mp4", clip(), "video/mp4"
        ).json()["data"]

        served = client.get(item["url"])
        assert served.status_code == 200, served.text
        assert served.headers["content-type"].startswith("video/mp4")
        assert served.headers.get("x-content-type-options") == "nosniff"
