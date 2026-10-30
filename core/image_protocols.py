"""Shared image protocol selection for manual and ecommerce generation."""
from __future__ import annotations

import re

from core.model_catalog import normalize_model_id


_NANO_BANANA_RE = re.compile(r"nano[\s_-]*banana", re.IGNORECASE)
_OPENAI_IMAGE_RE = re.compile(
    r"(?:gpt[\s_-]*image|grok[\s_-]*imagine[\s_-]*image)", re.IGNORECASE)


def _protocols(values):
    if isinstance(values, str):
        values = [values]
    result = set()
    for value in values or ():
        text = str(value or "").strip().casefold().replace("_", "-")
        if text in {
            "gemini", "gemini-native", "google", "google-generative-language",
            "generative-language", "generatecontent",
        } or "gemini" in text:
            result.add("gemini")
        elif text in {
            "openai", "openai-image", "openai-images", "images", "image",
            "images/generations", "dall-e",
        } or "openai-image" in text:
            result.add("openai")
    return result


def preferred_image_protocol(model, metadata=None):
    """Return ``gemini`` or ``openai`` for one image model.

    Nano Banana is a Gemini-native family even when an upstream catalog record
    advertises multiple endpoint types.  For other models explicit protocol
    metadata wins, then a single supported endpoint, then the model family.
    """
    model_id = normalize_model_id(model)
    text = str(model_id or "")
    metadata = metadata if isinstance(metadata, dict) else {}

    if _NANO_BANANA_RE.search(text):
        return "gemini"
    if _OPENAI_IMAGE_RE.search(text):
        return "openai"
    if re.search(r"gemini|imagen", text, re.IGNORECASE):
        return "gemini"

    explicit = metadata.get("protocol") or metadata.get("image_protocol")
    if isinstance(explicit, (list, tuple, set)):
        explicit = next(iter(explicit), "")
    explicit = str(explicit or "").casefold()
    requested = ""
    if "gemini" in explicit or "google" in explicit or "imagen" in explicit:
        requested = "gemini"
    elif "openai" in explicit or "dall" in explicit or "azure" in explicit:
        requested = "openai"

    supported = _protocols(metadata.get("supported_endpoint_types", metadata.get("endpoint_types", ())))
    if requested and (not supported or requested in supported):
        return requested
    if len(supported) == 1:
        return next(iter(supported))
    if "gemini" in supported and "google" in str(metadata.get("hints", "")).casefold():
        return "gemini"
    if "openai" in supported:
        return "openai"
    if "gemini" in supported:
        return "gemini"
    return "openai"


__all__ = ["preferred_image_protocol"]
