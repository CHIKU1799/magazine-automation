"""AI image generation (OpenAI-compatible /images/generations).

Generated images are always labelled as AI-generated on the asset (tag + credit). The prompt framing keeps them to
editorial illustration: no text baked into the picture (cover lines and captions stay live text) and no real,
identifiable people (speakers must be shown with real photographs).
"""

from __future__ import annotations

import base64
import io

import httpx
from PIL import Image

from app.config import get_settings

ASPECT_SIZES = {"portrait": "1024x1536", "landscape": "1536x1024", "square": "1024x1024"}
AI_TAG = "ai-generated"
FRAMING = ("Editorial image for a printed news magazine. Do not include any text, lettering, logos, watermarks or "
           "captions. Do not depict real, identifiable people; any people shown must be anonymous. ")


class ImageGenError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _endpoint() -> tuple[str, str]:
    s = get_settings()
    return (s.image_api_key or s.openai_api_key), (s.image_base_url or s.openai_base_url).rstrip("/")


def configured() -> tuple[bool, str]:
    s = get_settings()
    if s.image_provider == "none":
        return False, "IMAGE_PROVIDER=none: AI image generation is switched off."
    key, base = _endpoint()
    if not key:
        return False, "AI images need IMAGE_API_KEY or OPENAI_API_KEY (OpenAI-compatible /images/generations)."
    return True, f"{s.image_model} via {base}"


def _error(r: httpx.Response) -> ImageGenError:
    try:
        msg = r.json().get("error", {}).get("message") or r.text[:300]
    except ValueError:
        msg = r.text[:300]
    # 400 is usually the provider's content policy or a bad parameter: the editor can rephrase.
    return ImageGenError(f"The image service refused the request: {msg}", status=422 if r.status_code == 400 else 502)


def _image_from(r: httpx.Response) -> bytes:
    item = (r.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        return httpx.get(item["url"], timeout=get_settings().image_timeout_seconds).content
    raise ImageGenError("The image service returned no image.")


def generate(prompt: str, aspect: str) -> tuple[bytes, int, int]:
    """Returns (png_bytes, width, height). Raises ImageGenError with a message fit for the editor."""
    s = get_settings()
    ok, detail = configured()
    if not ok:
        raise ImageGenError(detail, status=503)
    size = ASPECT_SIZES.get(aspect)
    if size is None:
        raise ImageGenError(f"aspect must be one of {', '.join(ASPECT_SIZES)}", status=422)
    key, base = _endpoint()
    try:
        r = httpx.post(
            f"{base}/images/generations",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": s.image_model, "prompt": FRAMING + prompt.strip(), "size": size, "quality": s.image_quality,
                  "n": 1},
            timeout=s.image_timeout_seconds,
        )
    except httpx.HTTPError as e:
        raise ImageGenError(f"Couldn't reach the image service: {e}") from e
    if r.status_code != 200:
        raise _error(r)
    raw = _image_from(r)
    with Image.open(io.BytesIO(raw)) as im:
        w, h = im.size
        if im.format != "PNG":
            buf = io.BytesIO()
            im.save(buf, "PNG")
            raw = buf.getvalue()
    return raw, w, h

