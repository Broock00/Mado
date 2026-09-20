"""Rate limiting and upload handling tests.

Both of these exist because open publishing means untrusted input on a write
path. The tests that matter are the refusals: an upload that is not an image, a
limit that does not actually limit, a file that keeps its GPS coordinates.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest
from PIL import Image

from app.core.errors import ServiceUnavailableError, ValidationError
from app.core.rate_limit import (
    DRAFT_LIMIT,
    LOGIN_LIMIT,
    PUBLISH_LIMIT,
    REGISTER_LIMIT,
    UPLOAD_LIMIT,
    Limit,
    RateLimited,
    _InProcessLimiter,
)
from app.integrations import media_storage


def make_image(width: int = 800, height: int = 600, fmt: str = "JPEG", mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (120, 90, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


# --- rate limiting -----------------------------------------------------------


class TestSlidingWindow:
    """The in-process limiter, which is also the fallback path in production."""

    def test_allows_up_to_the_limit(self):
        limiter = _InProcessLimiter()
        limit = Limit(times=3, seconds=60, scope="test")
        assert all(limiter.check("k", limit, 1000.0 + i) is None for i in range(3))

    def test_refuses_beyond_the_limit(self):
        limiter = _InProcessLimiter()
        limit = Limit(times=3, seconds=60, scope="test")
        for i in range(3):
            limiter.check("k", limit, 1000.0 + i)
        assert limiter.check("k", limit, 1003.0) is not None

    def test_window_slides_rather_than_resetting(self):
        """A fixed window would allow a double burst across its boundary."""
        limiter = _InProcessLimiter()
        limit = Limit(times=2, seconds=60, scope="test")
        limiter.check("k", limit, 1000.0)
        limiter.check("k", limit, 1030.0)
        # Still inside the window of both.
        assert limiter.check("k", limit, 1050.0) is not None
        # The first has now aged out, so one slot is free again.
        assert limiter.check("k", limit, 1061.0) is None

    def test_retry_hint_points_past_the_oldest_entry(self):
        limiter = _InProcessLimiter()
        limit = Limit(times=1, seconds=60, scope="test")
        limiter.check("k", limit, 1000.0)
        retry_after = limiter.check("k", limit, 1010.0)
        assert retry_after == pytest.approx(50, abs=1)

    def test_identities_do_not_share_an_allowance(self):
        limiter = _InProcessLimiter()
        limit = Limit(times=1, seconds=60, scope="test")
        limiter.check("user:a", limit, 1000.0)
        assert limiter.check("user:b", limit, 1000.0) is None

    def test_scopes_do_not_share_an_allowance(self):
        """Publishing and reporting must not consume each other's budget."""
        limiter = _InProcessLimiter()
        publish = Limit(times=1, seconds=60, scope="publish")
        report = Limit(times=1, seconds=60, scope="report")
        limiter.check(f"ratelimit:{publish.scope}:u", publish, 1000.0)
        assert limiter.check(f"ratelimit:{report.scope}:u", report, 1000.0) is None


class TestLimitDefinitions:
    def test_expensive_actions_are_limited_more_tightly_than_cheap_ones(self):
        """Publishing runs two screeners including a model call; a draft does not.

        This is the ordering the limits are actually designed around - by cost of
        the action, not by a single global severity ranking.
        """
        assert PUBLISH_LIMIT.times < DRAFT_LIMIT.times
        assert PUBLISH_LIMIT.seconds == DRAFT_LIMIT.seconds

    def test_every_limit_is_bounded_and_positive(self):
        for limit in (PUBLISH_LIMIT, DRAFT_LIMIT, LOGIN_LIMIT, REGISTER_LIMIT, UPLOAD_LIMIT):
            assert limit.times > 0
            assert limit.seconds > 0
            assert limit.scope

    def test_descriptions_are_human_readable(self):
        assert PUBLISH_LIMIT.description == "20 per hour"
        assert LOGIN_LIMIT.description == "10 per 15 minutes"

    def test_error_carries_a_retry_hint(self):
        error = RateLimited(PUBLISH_LIMIT, retry_after=42)
        assert error.status_code == 429
        assert error.details["retryAfter"] == 42
        # The message says what the limit is, so a developer hitting it in
        # testing does not have to go digging for the number.
        assert "20 per hour" in error.message


# --- uploads -----------------------------------------------------------------


class TestUploadValidation:
    """Every input here is attacker-controlled."""

    def test_accepts_a_real_photograph(self):
        data, width, height = media_storage.process_image(make_image(1200, 800))
        assert data[:4] == b"RIFF"  # WebP container
        assert (width, height) == (1200, 800)

    def test_rejects_a_file_that_is_not_an_image(self):
        """Content-Type and extension are attacker-supplied; the decoder is not."""
        with pytest.raises(ValidationError):
            media_storage.process_image(b"#!/bin/sh\nrm -rf /\n")

    def test_rejects_an_empty_file(self):
        with pytest.raises(ValidationError):
            media_storage.process_image(b"")

    def test_rejects_an_oversized_file(self):
        with pytest.raises(ValidationError):
            media_storage.process_image(b"\xff" * (media_storage.MAX_UPLOAD_BYTES + 1))

    def test_rejects_a_tracking_pixel(self):
        with pytest.raises(ValidationError):
            media_storage.process_image(make_image(1, 1))

    def test_rejects_an_unsupported_format(self):
        buffer = io.BytesIO()
        Image.new("RGB", (400, 400)).save(buffer, format="BMP")
        with pytest.raises(ValidationError):
            media_storage.process_image(buffer.getvalue())


class TestUploadNormalisation:
    def test_oversized_images_are_scaled_down(self):
        _, width, height = media_storage.process_image(make_image(5000, 3000))
        assert max(width, height) == media_storage.MAX_DIMENSION

    def test_aspect_ratio_is_preserved(self):
        _, width, height = media_storage.process_image(make_image(4000, 2000))
        assert width / height == pytest.approx(2.0, abs=0.01)

    def test_transparency_is_flattened(self):
        """A transparent background renders unpredictably on a themed surface."""
        data, _, _ = media_storage.process_image(make_image(400, 400, fmt="PNG", mode="RGBA"))
        assert Image.open(io.BytesIO(data)).mode == "RGB"

    def test_metadata_is_stripped(self):
        """Phone photos carry GPS. Publishing a venue photo is not consent to
        publish where the photographer was standing."""
        original = Image.new("RGB", (600, 400))
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[0x010F] = "TestCamera"
        original.save(buffer, format="JPEG", exif=exif)

        assert Image.open(io.BytesIO(buffer.getvalue())).getexif()

        processed, _, _ = media_storage.process_image(buffer.getvalue())
        assert not dict(Image.open(io.BytesIO(processed)).getexif())

    def test_output_is_always_one_format(self):
        for fmt, mode in (("PNG", "RGB"), ("JPEG", "RGB"), ("WEBP", "RGB")):
            data, _, _ = media_storage.process_image(make_image(500, 500, fmt=fmt, mode=mode))
            assert Image.open(io.BytesIO(data)).format == "WEBP"


class TestStorage:
    @pytest.fixture(autouse=True)
    def _local_disk(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "media_provider", "local", raising=False)
        media_storage.reset_provider()
        yield
        media_storage.reset_provider()

    def test_identical_images_share_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        owner = uuid.uuid4()
        first = media_storage.store(make_image(400, 400), owner_id=owner)
        second = media_storage.store(make_image(400, 400), owner_id=owner)
        assert first.url == second.url

    def test_stored_name_is_not_chosen_by_the_uploader(self, tmp_path, monkeypatch):
        """Content-addressed, so a filename cannot be used to traverse or collide."""
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        stored = media_storage.store(make_image(400, 400), owner_id=uuid.uuid4())
        assert stored.url.startswith("/media/")
        assert ".." not in stored.url
        assert stored.url.endswith(".webp")


# --- video -------------------------------------------------------------------


def mp4_bytes(brand: bytes = b"isom", size: int = 4096) -> bytes:
    """An MP4 header, padded.

    Only the container is asserted anywhere - nothing here decodes video - so a
    real clip would test nothing extra and would put a binary in the repository.
    The tests below are careful to claim only what the header proves.
    """
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * (size - 12)


def webm_bytes(doctype: bytes = b"webm", size: int = 4096) -> bytes:
    return b"\x1a\x45\xdf\xa3" + b"\x42\x82" + doctype + b"\x00" * (size - 10)


class TestVideoIdentification:
    """The container is read from the file, never from what it was called."""

    def test_reads_mp4_from_its_own_bytes(self):
        assert media_storage.identify_video(mp4_bytes()) == "mp4"

    def test_reads_webm_from_its_own_bytes(self):
        assert media_storage.identify_video(webm_bytes()) == "webm"

    def test_refuses_matroska(self):
        """Same magic bytes as WebM, and browsers will not play it - so storing
        one produces a gallery entry that is permanently a broken player."""
        with pytest.raises(ValidationError) as caught:
            media_storage.identify_video(webm_bytes(doctype=b"matroska"))
        assert caught.value.code == "UNSUPPORTED_VIDEO"

    def test_refuses_quicktime(self):
        """Plays in Safari and nowhere else. A video that works for some
        visitors is worse than a refusal the uploader can act on."""
        with pytest.raises(ValidationError):
            media_storage.identify_video(mp4_bytes(brand=b"qt  "))

    def test_refuses_an_image_renamed_to_mp4(self):
        with pytest.raises(ValidationError):
            media_storage.identify_video(make_image(400, 400))

    def test_an_unrecognised_head_is_not_routed_to_the_video_path(self):
        """`looks_like_video` picks the verifier, so it must not claim a
        photograph - or a JPEG would be refused for not being a video."""
        assert media_storage.looks_like_video(mp4_bytes(brand=b"qt  ")) is True
        assert media_storage.looks_like_video(webm_bytes(doctype=b"matroska")) is True
        assert media_storage.looks_like_video(make_image(400, 400)) is False


class TestVideoStorage:
    @pytest.fixture(autouse=True)
    def _local_disk(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "media_provider", "local", raising=False)
        media_storage.reset_provider()
        yield
        media_storage.reset_provider()

    def test_extension_comes_from_the_bytes(self, tmp_path, monkeypatch):
        """The one property that makes the served Content-Type honest."""
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        with media_storage.VideoUpload() as upload:
            upload.feed(webm_bytes())
            stored = upload.finish(owner_id=uuid.uuid4())
        assert stored.url.endswith(".webm")
        assert stored.content_type == "video/webm"

    def test_refuses_a_file_over_the_cap_without_writing_it_all(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        with pytest.raises(ValidationError) as caught, media_storage.VideoUpload() as upload:
            upload.feed(mp4_bytes())
            # One chunk past the ceiling, rather than a hundred megabytes:
            # the check is on the running total, so this is the same test
            # and does not spend a minute of disk write to make it.
            upload.feed(b"\x00" * (media_storage.MAX_VIDEO_BYTES + 1))
        assert caught.value.code == "UPLOAD_TOO_LARGE"

    def test_refuses_before_the_whole_file_arrives(self, tmp_path, monkeypatch):
        """Identification happens on the head, so a file that is not a video is
        turned away rather than written and then deleted."""
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        with pytest.raises(ValidationError), media_storage.VideoUpload() as upload:
            upload.feed(b"\x00" * media_storage.SNIFF_BYTES)

    def test_a_short_file_is_still_identified(self, tmp_path, monkeypatch):
        """Shorter than the sniff window, so the check never fired during feed
        and has to happen at the end. Without it a 30-byte file of anything at
        all would be stored as a video."""
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        with pytest.raises(ValidationError), media_storage.VideoUpload() as upload:
            upload.feed(b"not a video")
            upload.finish(owner_id=uuid.uuid4())

    def test_an_abandoned_upload_leaves_nothing_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path / "media")
        with pytest.raises(ValidationError), media_storage.VideoUpload() as upload:
            upload.feed(b"\x00" * media_storage.SNIFF_BYTES)
        leftovers = list((tmp_path / "incoming").glob("*"))
        assert leftovers == [], f"partial upload left on disk: {leftovers}"

    def test_identical_videos_share_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        urls = []
        for _ in range(2):
            with media_storage.VideoUpload() as upload:
                upload.feed(mp4_bytes())
                urls.append(upload.finish(owner_id=uuid.uuid4()).url)
        assert urls[0] == urls[1]


# --- object store ------------------------------------------------------------


def _configure_r2(monkeypatch, **overrides):
    from app.core.config import get_settings

    settings = get_settings()
    values = {
        "media_provider": "auto",
        "environment": "development",
        "r2_account_id": "acct",
        "r2_access_key_id": "AKIAEXAMPLE",
        "r2_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "r2_bucket": "mado-media",
        "media_base_url": "https://cdn.example",
        "r2_endpoint_url": "",
        "r2_region": "auto",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    media_storage.reset_provider()


class RecordingClient:
    """Stands in for httpx.Client. Captures signed requests, never opens a socket."""

    calls: list[dict] = []
    status_code = 200

    def __init__(self, timeout=None):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, url, content=None, headers=None):
        body = content.read() if hasattr(content, "read") else content
        RecordingClient.calls.append(
            {"method": method, "url": url, "body": body, "headers": dict(headers or {})}
        )
        import httpx

        return httpx.Response(
            self.status_code, text="AccessDenied" if self.status_code >= 400 else ""
        )


class TestChoosingABackend:
    def teardown_method(self):
        media_storage.reset_provider()

    def test_local_disk_is_the_keyless_default(self, monkeypatch):
        _configure_r2(
            monkeypatch,
            r2_access_key_id="",
            r2_secret_access_key="",
            r2_bucket="",
            media_base_url="",
            r2_account_id="",
        )
        assert media_storage.get_provider().name == "local"

    def test_r2_is_used_when_fully_configured(self, monkeypatch):
        _configure_r2(monkeypatch)
        assert media_storage.get_provider().name == "r2"

    def test_a_public_origin_is_required_for_r2(self, monkeypatch):
        """The S3 endpoint is signed. Storing a URL nobody can fetch looks like
        a successful upload and is the stub-invents-success failure."""
        _configure_r2(monkeypatch, media_base_url="")
        assert media_storage.get_provider().name == "local"

    def test_explicit_local_wins_over_credentials(self, monkeypatch):
        _configure_r2(monkeypatch, media_provider="local")
        assert media_storage.get_provider().name == "local"

    def test_incomplete_r2_selection_falls_back(self, monkeypatch):
        _configure_r2(monkeypatch, media_provider="r2", r2_secret_access_key="")
        assert media_storage.get_provider().name == "local"


class TestAwsV4Signing:
    """The credential is in Authorization, never the URL."""

    when = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)

    def _headers(self, secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
        return media_storage._aws_v4_headers(
            method="PUT",
            url="https://acct.r2.cloudflarestorage.com/mado-media/ab/cd/file.webp",
            payload_hash=media_storage._EMPTY_SHA256,
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=secret,
            region="auto",
            extra_headers={"content-type": "image/webp"},
            when=self.when,
        )

    def test_the_signature_is_stable(self):
        first = self._headers()
        second = self._headers()
        assert first["authorization"] == second["authorization"]
        assert first["authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        assert "20150830/auto/s3/aws4_request" in first["authorization"]
        assert first["x-amz-date"] == "20150830T123600Z"

    def test_a_different_secret_produces_a_different_signature(self):
        signed = self._headers()["authorization"]
        other = self._headers("other-secret")["authorization"]
        assert signed != other

    def test_the_secret_is_not_in_the_authorization_header(self):
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert secret not in self._headers(secret)["authorization"]


class TestR2Store:
    @pytest.fixture(autouse=True)
    def _record(self, monkeypatch):
        RecordingClient.calls = []
        RecordingClient.status_code = 200
        monkeypatch.setattr(media_storage.httpx, "Client", RecordingClient)
        yield
        media_storage.reset_provider()

    def _store(self):
        return media_storage.R2Store(
            endpoint_url="https://acct.r2.cloudflarestorage.com",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            bucket="mado-media",
            public_base_url="https://cdn.example",
        )

    def test_put_uses_the_public_origin_not_the_s3_endpoint(self):
        url = self._store().put_bytes(
            key="ab/cd/file.webp", data=b"webp-bytes", content_type="image/webp"
        )
        assert url == "https://cdn.example/ab/cd/file.webp"
        call = RecordingClient.calls[0]
        assert call["method"] == "PUT"
        assert call["url"] == (
            "https://acct.r2.cloudflarestorage.com/mado-media/ab/cd/file.webp"
        )
        assert call["body"] == b"webp-bytes"
        assert call["headers"]["content-type"] == "image/webp"
        assert call["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 ")

    def test_store_writes_to_r2_not_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        _configure_r2(monkeypatch)
        stored = media_storage.store(make_image(400, 400), owner_id=uuid.uuid4())
        assert stored.url.startswith("https://cdn.example/")
        assert stored.url.endswith(".webp")
        assert list(tmp_path.rglob("*.webp")) == []
        assert RecordingClient.calls, "R2 was never contacted"
        assert RecordingClient.calls[0]["headers"]["content-type"] == "image/webp"

    def test_a_failed_put_does_not_fall_back_to_disk(self, tmp_path, monkeypatch):
        """The file would exist on this instance and 404 on every other one."""
        RecordingClient.status_code = 500
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path)
        _configure_r2(monkeypatch)
        with pytest.raises(ServiceUnavailableError):
            media_storage.store(make_image(400, 400), owner_id=uuid.uuid4())
        assert list(tmp_path.rglob("*.webp")) == []

    def test_a_video_is_streamed_not_held(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path / "media")
        _configure_r2(monkeypatch)
        clip = mp4_bytes()
        with media_storage.VideoUpload() as upload:
            upload.feed(clip)
            stored = upload.finish(owner_id=uuid.uuid4())
        assert stored.url.endswith(".mp4")
        assert stored.url.startswith("https://cdn.example/")
        assert stored.content_type == "video/mp4"
        call = RecordingClient.calls[0]
        assert call["method"] == "PUT"
        assert call["body"] == clip
        leftovers = list((tmp_path / "incoming").glob("*"))
        assert leftovers == [], f"staging file left behind: {leftovers}"
        assert list((tmp_path / "media").rglob("*")) == []

    def test_a_failed_video_put_leaves_nothing_on_disk(self, tmp_path, monkeypatch):
        RecordingClient.status_code = 503
        monkeypatch.setattr(media_storage, "_storage_root", lambda: tmp_path / "media")
        _configure_r2(monkeypatch)
        with pytest.raises(ServiceUnavailableError), media_storage.VideoUpload() as upload:
            upload.feed(mp4_bytes())
            upload.finish(owner_id=uuid.uuid4())
        assert list((tmp_path / "incoming").glob("*")) == []
        assert list((tmp_path / "media").rglob("*")) == []


class TestMediaHealth:
    def test_media_is_optional(self):
        from app.core import health

        check = next(c for c in health.CHECKS if c.name == "media")
        assert check.required is False

    async def test_local_disk_is_disabled_not_down(self, monkeypatch):
        from app.core import health
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "media_provider", "local", raising=False)
        media_storage.reset_provider()
        result = await health._run(next(c for c in health.CHECKS if c.name == "media"))
        assert result.status == health.STATUS_DISABLED
        media_storage.reset_provider()
