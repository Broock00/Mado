"""Image upload and storage.

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

**Local disk for now.** The storage interface is deliberately narrow - `store`
returns a URL - so moving to object storage later changes this module and nothing
that calls it. Spec 82.01 names S3-compatible storage for production; running that
locally would add a dependency for no development benefit.
"""

from __future__ import annotations

import hashlib
import io
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


@dataclass(slots=True)
class StoredImage:
    url: str
    width: int
    height: int
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
