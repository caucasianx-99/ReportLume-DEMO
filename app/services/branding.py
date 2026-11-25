"""Private agency branding and normalized, metadata-free PNG logos."""

import base64
import io

from PIL import Image, UnidentifiedImageError

from app.core.firestore_rest import FirestoreConflict
from app.core.config import settings
from app.services.workspace_access import plan_entitlements

from app.services.client_workspace import (
    WorkspaceReadOnly,
    make_update,
    require_write_access,
)


MAX_UPLOAD_BYTES = 200_000
MAX_IMAGE_PIXELS = 4_000_000
MAX_SAVED_BYTES = 42_000

THEMES = {
    "teal",
    "blue",
    "slate",
}

PLACEHOLDER_NAMES = {
    "your agency",
    "agency name",
}


class BrandValidation(ValueError):
    pass


def normalize_logo(contents: bytes) -> str:
    """Validate image bytes and re-encode without source metadata."""

    if (
        not isinstance(contents, bytes)
        or not 0 < len(contents) <= MAX_UPLOAD_BYTES
    ):
        raise BrandValidation(
            "Logo must be a PNG, JPEG or WebP under 200 KB."
        )

    try:
        with Image.open(io.BytesIO(contents)) as source:
            if source.format not in ("PNG", "JPEG", "WEBP"):
                raise BrandValidation(
                    "Only PNG, JPEG and WebP are accepted."
                )

            if (
                source.width <= 0
                or source.height <= 0
                or source.width * source.height > MAX_IMAGE_PIXELS
            ):
                raise BrandValidation(
                    "Image dimensions are too large."
                )

            source.load()

            image = source.convert("RGBA")
            image.thumbnail(
                (320, 120),
                Image.Resampling.LANCZOS,
            )

            output = io.BytesIO()

            image.save(
                output,
                format="PNG",
                optimize=True,
            )

            raw = output.getvalue()

            if len(raw) > MAX_SAVED_BYTES:
                raise BrandValidation(
                    "Logo has too much detail. "
                    "Use a simpler image."
                )

            return base64.b64encode(raw).decode("ascii")

    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        raise BrandValidation(
            "The image could not be verified."
        ) from exc


def validate_brand(name, theme):
    """Validate a real agency identity and supported palette."""

    if not isinstance(name, str):
        raise BrandValidation(
            "Enter an agency or professional name."
        )

    clean_name = name.strip()

    if not 1 <= len(clean_name) <= 80:
        raise BrandValidation(
            "Agency name must contain 1–80 characters."
        )

    if any(ord(character) < 32 for character in clean_name):
        raise BrandValidation(
            "Agency name contains unsupported control characters."
        )

    if clean_name.casefold() in PLACEHOLDER_NAMES:
        raise BrandValidation(
            "Replace the example agency name with your own."
        )

    if theme not in THEMES:
        raise BrandValidation(
            "Select a supported report color palette."
        )

    return clean_name, theme


def save_brand(
    store,
    name,
    theme=None,
    logo=None,
    remove_logo=False,
):
    """Persist name on all plans; customize only when verified rights allow.

    Solo retains stored colors and logos without editing or applying them.
    A Professional or Agency trial may customize within its own trial limits.
    The plan selection alone never grants paid capacity.
    """
    for _ in range(3):
        profile, version = store.get_user_with_version()
        require_write_access(profile)

        rights = plan_entitlements(profile, settings)
        custom_allowed = bool(
            rights.get("active")
            and rights.get("allow_custom_branding")
        )
        original_theme = profile.get("brand_theme", "teal")

        # Historical branding records may have an unsupported palette.
        # A Solo owner must still be able to update their name safely.
        if custom_allowed:
            chosen_theme = (
                theme if theme not in (None, "") else original_theme
            )
        else:
            chosen_theme = (
                "teal"
                if original_theme not in THEMES
                else original_theme
            )

        clean_name, chosen_theme = validate_brand(
            name,
            chosen_theme,
        )

        if logo is not None and remove_logo:
            raise BrandValidation(
                "Choose either a new logo or removal, not both."
            )

        changes = {"brand_name": clean_name}

        if custom_allowed:
            changes["brand_theme"] = chosen_theme

            if remove_logo:
                changes["brand_logo_b64"] = ""
            elif logo is not None:
                changes["brand_logo_b64"] = normalize_logo(logo)

        elif (
            (theme not in (None, "") and theme != original_theme)
            or logo is not None
            or remove_logo
        ):
            raise BrandValidation(
                "Logo and report colors are available only on Professional or Agency."
            )

        try:
            store.commit([
                make_update(
                    store.user_name,
                    changes,
                    version,
                )
            ])
            return
        except FirestoreConflict:
            continue

    raise WorkspaceReadOnly(
        "Brand settings changed concurrently. Refresh and retry."
    )
