"""Image and video upload and storage.

Publishers could previously only attach an image by supplying a URL, which meant
in practice that only the seed had pictures: nobody photographing a venue on their
phone has somewhere to host it first. This closes that gap.

**Uploads are decoded, not trusted.** A file arriving from an open publishing
form is attacker-controlled. Content-Type says whatever the client says it says,
and an extension says even less. Every upload is therefore opened and re-encoded
with Pillow, which means:

- the bytes are proven to be a real image of a format we support, because the
  decoder had to parse them;
- anything smuggled alongside the image data is dropped, since only decoded
  pixels survive re-encoding;
- EXIF is discarded, which matters for privacy - phone photographs routinely
  carry GPS coordinates, and a publisher sharing a venue photo is not consenting
  to publish where they were standing.

**Decompression bombs are rejected before decoding.** A small file can declare
enormous dimensions and exhaust memory when expanded. Pillow's own guard is
enabled, and pixel count is checked against a limit before any full decode.

**Video is checked, not decoded.** There is no Pillow for video: proving a file
is playable would mean shelling out to ffmpeg, and adding a binary dependency to
the API for a check is a bigger decision than this feature. So the guarantee is
weaker and deliberately narrower - the container is identified from its own magic
bytes, only MP4 and WebM are accepted, and the stored extension comes from what
was recognised rather than from what the uploader named the file. That is what
makes the served Content-Type honest, which is the property that matters: a file
served as `video/mp4` cannot execute as HTML however it was crafted.

The narrowness costs something real - a QuickTime `.mov` off an iPhone is refused
rather than stored - and that is the intended trade. Storing it would produce a
video that plays in Safari and shows a broken player everywhere else, which is
worse than a refusal an uploader can act on. Transcoding is the actual fix and
wants ffmpeg.

**Cloudflare R2 is the store; local disk is the fallback.** Spec 82.01 names
S3-compatible object storage for production. R2 speaks that API, and the
interface here is still `store` returning a URL, so another S3 host is an
endpoint setting. Local disk is what runs when R2 is not configured - development
and tests - not a silent write that happens because R2 had a bad day. A put that
fails is an error; falling back would store a URL that 404s on every other
instance, which is the stub-invents-success failure this project refuses.

**HTTP rather than a vendor SDK.** Chapa and Stripe are spoken with httpx too.
boto3 would work, and would also be the one integration that could not swap to
MinIO by changing an endpoint. Signature Version 4 is the S3 protocol; the SDK
is not.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from app.core import tracing
from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError, ValidationError
from app.core.logging import get_logger, redact

logger = get_logger("mado.media")

# Formats accepted on upload. Deliberately short: each one is a decoder with its
# own history of vulnerabilities, and this covers what phones and cameras produce.
ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}

# What everything is re-encoded to. One output format keeps the serving path
# simple, and WebP is materially smaller than JPEG at the same quality.
OUTPUT_FORMAT = "WEBP"
OUTPUT_QUALITY = 82
OUTPUT_EXTENSION = "webp"

# Largest accepted upload. A phone photograph is comfortably under this; anything
# above it is either a mistake or an attempt to fill the disk.
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

# Pixel ceiling, checked before decoding. 50 megapixels is beyond any phone and
# well below what it takes to exhaust memory.
MAX_PIXELS = 50_000_000

# Longest edge after resizing. Cards and detail pages never need more, and
# serving a 6000px original to a phone wastes the explorer's data, not ours.
MAX_DIMENSION = 2000

# Refuse images too small to be useful, which are almost always tracking pixels,
# spacers or accidents rather than photographs of a venue.
MIN_DIMENSION = 200

# --- Video -------------------------------------------------------------------

# Largest accepted video. Generous next to an image because a minute of phone
# footage genuinely is this big, and small enough that a handful of them do not
# fill a development disk.
MAX_VIDEO_BYTES = 100 * 1024 * 1024

# How much of the head is held back for identification. An MP4 declares itself in
# the first twelve bytes; WebM needs the DocType, which sits inside the EBML
# header a little further in. 4 KiB covers both with room to spare.
SNIFF_BYTES = 4096

# MP4 brands accepted, read from bytes 8-12 of an `ftyp` box. The ISO base media
# family and the MP4 brands proper - all of which browsers play as `video/mp4`.
# `qt  ` is absent on purpose: QuickTime is the same box structure and a
# different thing to serve, see the module docstring.
_MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"iso8",
    b"mp41", b"mp42", b"avc1", b"mmp4", b"dash", b"cmfc",
})

VIDEO_TYPES: dict[str, str] = {"mp4": "video/mp4", "webm": "video/webm"}

# Content-addressed objects never change at a given key, so intermediaries may
# keep them for a year. Without this, a CDN refetches the same photograph.
_CACHE_CONTROL = "public, max-age=31536000, immutable"

# SHA-256 of the empty byte string, required for signed HEAD/empty-body requests.
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

IMAGE_PUT_TIMEOUT = 30.0
VIDEO_PUT_TIMEOUT = 120.0
# Health probes are themselves capped at two seconds; this is just the client's.
PING_TIMEOUT = 8.0


@dataclass(slots=True)
class StoredImage:
    url: str
    width: int
    height: int
    bytes_written: int


@dataclass(slots=True)
class StoredVideo:
    url: str
    content_type: str
    bytes_written: int


def _storage_root() -> Path:
    root = Path(get_settings().media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def process_image(data: bytes) -> tuple[bytes, int, int]:
    """Validate, normalise and re-encode an uploaded image.

    Returns ``(webp_bytes, width, height)``. Raises :class:`ValidationError` with
    an explanation an uploader can act on - "that file is not an image we can
    read" is useful, "500" is not.
    """
    if not data:
        raise ValidationError("The file was empty.", code="EMPTY_UPLOAD")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"Images must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            code="UPLOAD_TOO_LARGE",
        )

    # Pillow's own decompression-bomb guard, raised as an error rather than a
    # warning so a hostile file cannot merely log its way through.
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS

    try:
        # open() reads the header only, so dimensions can be checked before
        # committing memory to a full decode.
        probe = Image.open(io.BytesIO(data))
        image_format = (probe.format or "").upper()
        width, height = probe.size
    except UnidentifiedImageError as exc:
        raise ValidationError(
            "That file is not an image we can read. JPEG, PNG or WebP work.",
            code="UNSUPPORTED_IMAGE",
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ValidationError(
            "That image is too large to process.", code="IMAGE_TOO_LARGE"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a malformed file must not 500
        logger.warning("image_probe_failed", error=str(exc))
        raise ValidationError("That image could not be read.", code="UNSUPPORTED_IMAGE") from exc

    if image_format not in ACCEPTED_FORMATS:
        raise ValidationError(
            f"{image_format or 'That format'} is not supported. Use JPEG, PNG or WebP.",
            code="UNSUPPORTED_IMAGE",
        )
    if width * height > MAX_PIXELS:
        raise ValidationError("That image is too large to process.", code="IMAGE_TOO_LARGE")
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise ValidationError(
            f"Images need to be at least {MIN_DIMENSION}px on both sides.",
            code="IMAGE_TOO_SMALL",
        )

    try:
        image = Image.open(io.BytesIO(data))
        # Honour the orientation tag *before* stripping metadata, otherwise a
        # photo taken sideways is stored sideways.
        from PIL import ImageOps

        image = ImageOps.exif_transpose(image)

        # Flatten transparency onto white. WebP supports alpha, but a card with a
        # transparent background renders unpredictably against a themed surface.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(backdrop, image).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

        buffer = io.BytesIO()
        # No exif= argument: re-encoding from decoded pixels is what drops the
        # original metadata, including any GPS coordinates.
        image.save(buffer, format=OUTPUT_FORMAT, quality=OUTPUT_QUALITY, method=4)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_processing_failed", error=str(exc))
        raise ValidationError("That image could not be processed.", code="IMAGE_FAILED") from exc

    return buffer.getvalue(), image.width, image.height


def looks_like_video(head: bytes) -> bool:
    """Whether these opening bytes are a video container at all.

    Deliberately looser than `identify_video`, and answers a different question.
    This one decides *which* verifier an upload goes to; that one decides whether
    the file is kept. So a Matroska file or an exotic MP4 brand is a video here
    and refused there - which is how the uploader gets "browsers cannot play
    that" instead of "that is not an image", an error about the wrong thing
    entirely.
    """
    return head.startswith(b"\x1a\x45\xdf\xa3") or (len(head) >= 8 and head[4:8] == b"ftyp")


def identify_video(head: bytes) -> str:
    """Which container this is, from its own bytes. Raises if it is neither.

    The uploaded filename and Content-Type are ignored entirely - both are
    written by the client, and this is the value the file is then *served* as.
    """
    # WebM and Matroska share the EBML magic and differ only in the DocType
    # string that follows. Matroska is rejected: browsers do not play it, so
    # storing one produces a gallery entry that is permanently a broken player.
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        if b"webm" in head[:64]:
            return "webm"
        raise ValidationError(
            "That looks like a Matroska file. Convert it to WebM or MP4 first.",
            code="UNSUPPORTED_VIDEO",
        )

    # An MP4 opens with a box: a four-byte length, then `ftyp`, then the brand.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        if head[8:12] in _MP4_BRANDS:
            return "mp4"
        raise ValidationError(
            "That video is in a format browsers cannot play. MP4 (H.264) or WebM work.",
            code="UNSUPPORTED_VIDEO",
        )

    raise ValidationError(
        "That file is not a video we can read. MP4 or WebM work.",
        code="UNSUPPORTED_VIDEO",
    )


def _object_key(digest: str, extension: str) -> str:
    # Sharded two levels deep - a single prefix with a hundred thousand files
    # in it is slow to list on every filesystem worth naming, and S3 list
    # performance is the same shape.
    return f"{digest[:2]}/{digest[2:4]}/{digest}.{extension}"


# --- persistence -------------------------------------------------------------


class ObjectStore(Protocol):
    """Where processed bytes go. Callers never see a vendor type."""

    name: str

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> str:
        """Persist bytes, returning the public URL they are served from."""
        ...

    def put_file(
        self, *, key: str, path: Path, content_type: str, payload_hash: str
    ) -> str:
        """Persist a file already on disk, without reading it into memory."""
        ...

    def ping(self) -> None:
        """Reach the store, or raise. Used by readiness, not by uploads."""
        ...


class LocalDiskStore:
    """This machine's disk. The fallback, and what development runs on.

    A file that is already there is left alone rather than replaced: the same
    photograph uploaded twice is one object, and something may be serving the
    first copy this second.
    """

    name = "local"

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> str:
        destination = _storage_root() / Path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(data)
        return f"/media/{key}"

    def put_file(
        self, *, key: str, path: Path, content_type: str, payload_hash: str
    ) -> str:
        destination = _storage_root() / Path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            # Same bytes already stored. Drop the copy rather than replacing
            # what is there.
            path.unlink(missing_ok=True)
        else:
            path.replace(destination)
        return f"/media/{key}"

    def ping(self) -> None:
        return None


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(f"AWS4{secret}".encode(), date_stamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _canonical_uri(path: str) -> str:
    # Each segment encoded; slashes between them stay, which is the S3 rule.
    # Default `quote` keeps `/` as safe, which would leave a segment unencoded.
    return "/".join(quote(part, safe="-._~") for part in path.split("/"))


def _aws_v4_headers(
    *,
    method: str,
    url: str,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    extra_headers: dict[str, str] | None = None,
    when: datetime | None = None,
    service: str = "s3",
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one request.

    The credential travels in `Authorization`, never in the URL. A query-string
    signature is a credential in every proxy log; this project has already
    leaked a key that way once.
    """
    when = when or datetime.now(UTC)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")
    parsed = urlsplit(url)
    headers = {
        "host": parsed.netloc,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if extra_headers:
        headers.update({name.lower(): value.strip() for name, value in extra_headers.items()})

    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = (
        f"{method}\n{_canonical_uri(parsed.path or '/')}\n{parsed.query}\n"
        f"{canonical_headers}\n{signed_names}\n{payload_hash}"
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return headers


def _object_url(endpoint: str, bucket: str, key: str = "") -> str:
    encoded_bucket = quote(bucket, safe="-._~")
    if not key:
        return f"{endpoint}/{encoded_bucket}"
    encoded_key = "/".join(quote(part, safe="-._~") for part in key.split("/"))
    return f"{endpoint}/{encoded_bucket}/{encoded_key}"


class R2Store:
    """Cloudflare R2 via the S3 API. Spec 82.01's production store.

    Path-style addressing (`/{bucket}/{key}` on the account endpoint) rather
    than virtual-hosted: a bucket name with a dot in it breaks TLS on the
    latter, and R2 accepts both.
    """

    name = "r2"

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
        region: str = "auto",
    ) -> None:
        self._endpoint = endpoint_url.rstrip("/")
        self._access_key = access_key_id
        self._secret = secret_access_key
        self._bucket = bucket
        self._public = public_base_url.rstrip("/")
        self._region = region

    def _headers(
        self,
        method: str,
        url: str,
        payload_hash: str,
        content_type: str | None,
    ) -> dict[str, str]:
        extra = {}
        if content_type:
            extra["content-type"] = content_type
            extra["cache-control"] = _CACHE_CONTROL
        return _aws_v4_headers(
            method=method,
            url=url,
            payload_hash=payload_hash,
            access_key=self._access_key,
            secret_key=self._secret,
            region=self._region,
            extra_headers=extra or None,
        )

    def _raise_for(self, response: httpx.Response) -> None:
        if response.status_code in {200, 201, 204}:
            return
        logger.warning(
            "r2_request_failed",
            status=response.status_code,
            detail=redact(response.text[:200]),
        )
        raise ServiceUnavailableError("Media storage is unavailable right now.")

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload_hash: str,
        content_type: str | None = None,
        content: object = None,
        timeout: float,
    ) -> None:
        headers = self._headers(method, url, payload_hash, content_type)
        with tracing.dependency("media"), httpx.Client(timeout=timeout) as client:
            try:
                response = client.request(method, url, content=content, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("r2_request_failed", error=redact(str(exc)))
                raise ServiceUnavailableError(
                    "Media storage is unavailable right now."
                ) from exc
            self._raise_for(response)

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> str:
        url = _object_url(self._endpoint, self._bucket, key)
        self._request(
            "PUT",
            url,
            payload_hash=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            content=data,
            timeout=IMAGE_PUT_TIMEOUT,
        )
        return f"{self._public}/{key}"

    def put_file(
        self, *, key: str, path: Path, content_type: str, payload_hash: str
    ) -> str:
        url = _object_url(self._endpoint, self._bucket, key)
        # Streamed: a hundred-megabyte video read into a string so it can be
        # hashed a second time would make the size cap decorative. The hash was
        # accumulated on the way in.
        with path.open("rb") as body:
            self._request(
                "PUT",
                url,
                payload_hash=payload_hash,
                content_type=content_type,
                content=body,
                timeout=VIDEO_PUT_TIMEOUT,
            )
        return f"{self._public}/{key}"

    def ping(self) -> None:
        url = _object_url(self._endpoint, self._bucket)
        self._request("HEAD", url, payload_hash=_EMPTY_SHA256, timeout=PING_TIMEOUT)


def _r2_endpoint(settings) -> str:
    if settings.r2_endpoint_url:
        return settings.r2_endpoint_url.rstrip("/")
    if settings.r2_account_id:
        return f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
    return ""


def r2_ready(settings) -> bool:
    """Whether R2 can actually serve what it stores.

    The public origin is required, not optional: the S3 endpoint itself is
    signed and a browser cannot fetch from it. Selecting R2 without one would
    store URLs nobody can load, which looks like a successful upload.
    """
    return bool(
        _r2_endpoint(settings)
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
        and settings.r2_bucket
        and settings.media_base_url
    )


@lru_cache
def get_provider() -> ObjectStore:
    """Select the object store.

    ``auto`` prefers R2 when it is fully configured and falls back to local
    disk otherwise, matching how geocoding and places choose. Incomplete R2
    credentials are the same as none: a half-set key that writes nowhere is
    worse than disk.
    """
    settings = get_settings()
    choice = settings.media_provider
    if choice == "auto":
        choice = "r2" if r2_ready(settings) else "local"

    if choice == "r2":
        if r2_ready(settings):
            return R2Store(
                endpoint_url=_r2_endpoint(settings),
                access_key_id=settings.r2_access_key_id,
                secret_access_key=settings.r2_secret_access_key,
                bucket=settings.r2_bucket,
                public_base_url=settings.media_base_url,
                region=settings.r2_region or "auto",
            )
        logger.warning("r2_selected_without_credentials_falling_back")

    if settings.environment in {"production", "staging"}:
        # Disk on one instance is not a store: the next request may land
        # elsewhere and the file is gone. Loud, because this is the
        # misconfiguration, not a mode.
        logger.warning("media_using_local_disk_in_deployment")
    return LocalDiskStore()


def reset_provider() -> None:
    get_provider.cache_clear()


def store(data: bytes, *, owner_id: uuid.UUID) -> StoredImage:
    """Process and persist an upload, returning the URL it is served from."""
    processed, width, height = process_image(data)

    # Content-addressed: the same photograph uploaded twice occupies one object,
    # and the name cannot be chosen by the uploader.
    digest = hashlib.sha256(processed).hexdigest()[:32]
    key = _object_key(digest, OUTPUT_EXTENSION)
    backend = get_provider()
    url = backend.put_bytes(key=key, data=processed, content_type="image/webp")

    logger.info(
        "image_stored",
        owner_id=str(owner_id),
        bytes=len(processed),
        width=width,
        height=height,
        backend=backend.name,
    )
    return StoredImage(
        url=url,
        width=width,
        height=height,
        bytes_written=len(processed),
    )


class VideoUpload:
    """A video arriving in chunks, checked as it goes.

    Streamed rather than read whole. `store` can hold an image in memory because
    an image is megabytes; a hundred-megabyte video read into a string before any
    size check makes the size check decorative, and doing it per concurrent
    request is how the API runs out of memory.

    Identification happens on the first few kilobytes, so a file that is not a
    video is refused before the rest of it is written anywhere.

    Staging is always local, including when R2 is the store. `finish` completes
    with a rename on disk or a streamed put to R2; a rename across filesystems
    is a copy, and a half-written object under a content-hash name is a
    permanently truncated video. Beside the media root rather than inside it,
    because everything inside is served publicly and a half-written upload is
    not something to serve.

    Use as a context manager - the partial file is removed on any exit that did
    not `finish`, including an exception mid-upload.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._head = b""
        self._kind: str | None = None
        self._written = 0
        staging = _storage_root().parent / "incoming"
        staging.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(dir=staging, suffix=".part")
        self._path = Path(path)
        self._file = os.fdopen(handle, "wb")

    def __enter__(self) -> VideoUpload:
        return self

    def __exit__(self, *_: object) -> None:
        self.discard()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return

        self._written += len(chunk)
        if self._written > MAX_VIDEO_BYTES:
            raise ValidationError(
                f"Videos must be under {MAX_VIDEO_BYTES // (1024 * 1024)} MB.",
                code="UPLOAD_TOO_LARGE",
            )

        if self._kind is None:
            self._head += chunk[: SNIFF_BYTES - len(self._head)]
            if len(self._head) >= SNIFF_BYTES:
                self._kind = identify_video(self._head)

        self._digest.update(chunk)
        self._file.write(chunk)

    def finish(self, *, owner_id: uuid.UUID) -> StoredVideo:
        """Identify what has not been identified yet, then commit the file."""
        self._file.close()

        if self._written == 0:
            self.discard()
            raise ValidationError("The file was empty.", code="EMPTY_UPLOAD")
        # A file shorter than the sniff window never triggered the check above.
        kind = self._kind or identify_video(self._head)

        payload_hash = self._digest.hexdigest()
        key = _object_key(payload_hash[:32], kind)
        backend = get_provider()
        url = backend.put_file(
            key=key,
            path=self._path,
            content_type=VIDEO_TYPES[kind],
            payload_hash=payload_hash,
        )

        logger.info(
            "video_stored",
            owner_id=str(owner_id),
            bytes=self._written,
            container=kind,
            backend=backend.name,
        )
        return StoredVideo(
            url=url,
            content_type=VIDEO_TYPES[kind],
            bytes_written=self._written,
        )

    def discard(self) -> None:
        if not self._file.closed:
            self._file.close()
        self._path.unlink(missing_ok=True)
