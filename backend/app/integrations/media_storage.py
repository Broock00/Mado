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

**Local disk for now.** The storage interface is deliberately narrow - `store`
returns a URL - so moving to object storage later changes this module and nothing
that calls it. Spec 82.01 names S3-compatible storage for production; running that
locally would add a dependency for no development benefit.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger

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
# fill a development disk. This is the number that moves first when uploads go to
# object storage.
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


def store(data: bytes, *, owner_id: uuid.UUID) -> StoredImage:
    """Process and persist an upload, returning the URL it is served from."""
    processed, width, height = process_image(data)

    # Content-addressed: the same photograph uploaded twice occupies one file, and
    # the name cannot be chosen by the uploader.
    digest = hashlib.sha256(processed).hexdigest()[:32]
    # Sharded two levels deep - a single directory with a hundred thousand files
    # in it is slow to list on every filesystem worth naming.
    relative = Path(digest[:2], digest[2:4], f"{digest}.{OUTPUT_EXTENSION}")

    destination = _storage_root() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(processed)

    logger.info(
        "image_stored",
        owner_id=str(owner_id),
        bytes=len(processed),
        width=width,
        height=height,
    )
    return StoredImage(
        url=f"/media/{relative.as_posix()}",
        width=width,
        height=height,
        bytes_written=len(processed),
    )


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


class VideoUpload:
    """A video arriving in chunks, checked as it goes.

    Streamed rather than read whole. `store` can hold an image in memory because
    an image is megabytes; a hundred-megabyte video read into a string before any
    size check makes the size check decorative, and doing it per concurrent
    request is how the API runs out of memory.

    Identification happens on the first few kilobytes, so a file that is not a
    video is refused before the rest of it is written anywhere.

    Use as a context manager - the partial file is removed on any exit that did
    not `finish`, including an exception mid-upload.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._head = b""
        self._kind: str | None = None
        self._written = 0
        # Beside the media root rather than in the system temp directory:
        # `finish` completes with a rename, and a rename across filesystems is
        # not atomic - it is a copy, which can be interrupted halfway and leave a
        # truncated file under a name that claims to be a content hash. Beside
        # rather than inside, because everything inside is served publicly and a
        # half-written upload is not something to serve.
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

        digest = self._digest.hexdigest()[:32]
        relative = Path(digest[:2], digest[2:4], f"{digest}.{kind}")
        destination = _storage_root() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            # Same bytes already stored. Drop the copy rather than replacing what
            # is there - something may be serving it this second.
            self.discard()
        else:
            self._path.replace(destination)

        logger.info(
            "video_stored", owner_id=str(owner_id), bytes=self._written, container=kind
        )
        return StoredVideo(
            url=f"/media/{relative.as_posix()}",
            content_type=VIDEO_TYPES[kind],
            bytes_written=self._written,
        )

    def discard(self) -> None:
        if not self._file.closed:
            self._file.close()
        self._path.unlink(missing_ok=True)
