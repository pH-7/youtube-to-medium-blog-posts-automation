"""
Multi-platform publishing layer.

This module provides a clean, extensible abstraction for publishing a single
generated article to several blogging platforms (Medium, Dev.to, Hashnode, ...).

Design goals:
- Each platform is a small, self-contained ``BasePublisher`` subclass.
- Adding a new platform = add one class + register it in ``_PUBLISHER_REGISTRY``.
- Platform-specific *formatting* is respected:
    * Medium consumes HTML (captions, kicker, subtitle render reliably).
    * Dev.to (Forem) and Hashnode consume Markdown directly.
- The main script stays agnostic: it builds the publishers once and calls
  ``publish_to_all(...)`` with plain Markdown.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class PublishResult:
    """Outcome of a single publish attempt to one platform."""
    platform: str
    success: bool
    url: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _is_publish_status(config: Dict[str, Any]) -> bool:
    """Return True when articles should be published live (not as drafts)."""
    status = str(config.get("PUBLISH_STATUS", "draft")).strip().lower()
    return status in ("publish", "public", "published", "live", "true")


def _first_image_url(markdown_content: str) -> Optional[str]:
    """Extract the first Markdown image URL (used as a cover image when supported)."""
    match = re.search(r"!\[[^\]]*\]\(([^)\s]+)", markdown_content)
    return match.group(1) if match else None


def _slugify_tag(tag: str) -> str:
    """Normalize a tag into a lowercase, alphanumeric slug (platform-safe)."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "", tag.lower())
    return slug


# ---------------------------------------------------------------------------
# Base publisher
# ---------------------------------------------------------------------------
class BasePublisher(ABC):
    """Abstract base class every platform publisher must implement."""

    #: Short, lowercase platform identifier (e.g. "medium", "devto").
    name: str = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True when this publisher has the credentials it needs."""

    @abstractmethod
    def publish(
        self,
        *,
        title: str,
        content: str,
        tags: List[str],
        output_language: str,
        niche: str,
    ) -> PublishResult:
        """Publish the article (Markdown ``content``) and return a result."""


# ---------------------------------------------------------------------------
# Medium
# ---------------------------------------------------------------------------
class MediumPublisher(BasePublisher):
    """
    Publish to Medium using HTML ``contentFormat``.

    Medium's API accepts HTML or Markdown, but HTML mode renders image captions,
    the kicker and the subtitle reliably. The Markdown -> HTML conversion is
    injected (``html_converter``) so all Medium-specific formatting stays in one
    place in the main script.
    """

    name = "medium"

    def __init__(self, config: Dict[str, Any], html_converter: Callable[[str, str], str]):
        super().__init__(config)
        self._html_converter = html_converter

    def is_configured(self) -> bool:
        return bool(self.config.get("MEDIUM_ACCESS_TOKEN"))

    def _select_publication_id(self, output_language: str, niche: str) -> Optional[str]:
        if niche == "tech":
            return self.config.get("MEDIUM_TECH_PUBLICATION_ID")
        if output_language == "fr":
            return self.config.get("MEDIUM_FR_PUBLICATION_ID")
        return self.config.get("MEDIUM_EN_PUBLICATION_ID")

    def publish(self, *, title, content, tags, output_language, niche) -> PublishResult:
        token = self.config["MEDIUM_ACCESS_TOKEN"]
        publish_status = "public" if _is_publish_status(self.config) else "draft"
        html_content = self._html_converter(content, title)

        article = {
            "title": title,
            "contentFormat": "html",
            "content": html_content,
            "tags": tags[:5],  # Medium allows up to 5 tags
            "publishStatus": publish_status,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Charset": "utf-8",
        }

        response = None
        try:
            publication_id = self._select_publication_id(output_language, niche)
            post_to_publication = self.config.get("POST_TO_PUBLICATION", False)

            if post_to_publication and publication_id:
                response = requests.post(
                    f"https://api.medium.com/v1/publications/{publication_id}/posts",
                    headers=headers,
                    json=article,
                )
            else:
                user_info = requests.get("https://api.medium.com/v1/me", headers=headers)
                user_info.raise_for_status()
                user_id = user_info.json()["data"]["id"]
                response = requests.post(
                    f"https://api.medium.com/v1/users/{user_id}/posts",
                    headers=headers,
                    json=article,
                )

            response.raise_for_status()
            url = response.json()["data"]["url"]
            return PublishResult(self.name, True, url=url)

        except Exception as e:
            detail = response.text if response is not None else "No response"
            return PublishResult(self.name, False, error=f"{e} | {detail}")


# ---------------------------------------------------------------------------
# Dev.to (Forem)
# ---------------------------------------------------------------------------
class DevToPublisher(BasePublisher):
    """
    Publish to Dev.to via the Forem API (https://developers.forem.com/api).

    Dev.to consumes Markdown directly through ``body_markdown`` and supports up
    to 4 tags (alphanumeric, no spaces). The free API key is created at
    https://dev.to/settings/extensions.
    """

    name = "devto"
    _API_URL = "https://dev.to/api/articles"

    def is_configured(self) -> bool:
        return bool(self.config.get("DEVTO_API_KEY"))

    def publish(self, *, title, content, tags, output_language, niche) -> PublishResult:
        api_key = self.config["DEVTO_API_KEY"]

        # Dev.to tags: max 4, lowercase, alphanumeric only, no empties.
        sane_tags = [t for t in (_slugify_tag(tag) for tag in tags) if t][:4]

        article: Dict[str, Any] = {
            "title": title,
            "body_markdown": content,
            "published": _is_publish_status(self.config),
            "tags": sane_tags,
        }

        organization_id = self.config.get("DEVTO_ORGANIZATION_ID")
        if organization_id:
            article["organization_id"] = organization_id

        cover_image = _first_image_url(content)
        if cover_image:
            article["main_image"] = cover_image

        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/vnd.forem.api-v1+json",
        }

        response = None
        try:
            response = requests.post(self._API_URL, headers=headers, json={"article": article})
            response.raise_for_status()
            url = response.json().get("url")
            return PublishResult(self.name, True, url=url)
        except Exception as e:
            detail = response.text if response is not None else "No response"
            return PublishResult(self.name, False, error=f"{e} | {detail}")


# ---------------------------------------------------------------------------
# Hashnode
# ---------------------------------------------------------------------------
class HashnodePublisher(BasePublisher):
    """
    Publish to Hashnode via the GraphQL API (https://gql.hashnode.com/).

    Hashnode consumes Markdown through ``contentMarkdown``. A publication ID is
    required (find it in your Hashnode dashboard URL or via the API). Drafts and
    live posts use different mutations, honoured here based on ``PUBLISH_STATUS``.
    The free personal access token is created at
    https://hashnode.com/settings/developer.
    """

    name = "hashnode"
    _API_URL = "https://gql.hashnode.com/"

    def is_configured(self) -> bool:
        return bool(self.config.get("HASHNODE_API_KEY") and self.config.get("HASHNODE_PUBLICATION_ID"))

    def _build_tags(self, tags: List[str]) -> List[Dict[str, str]]:
        built = []
        seen = set()
        for tag in tags[:5]:
            slug = _slugify_tag(tag)
            if slug and slug not in seen:
                seen.add(slug)
                built.append({"slug": slug, "name": tag})
        return built

    def publish(self, *, title, content, tags, output_language, niche) -> PublishResult:
        api_key = self.config["HASHNODE_API_KEY"]
        publication_id = self.config["HASHNODE_PUBLICATION_ID"]
        is_live = _is_publish_status(self.config)

        post_input: Dict[str, Any] = {
            "title": title,
            "contentMarkdown": content,
            "tags": self._build_tags(tags),
            "publicationId": publication_id,
        }

        cover_image = _first_image_url(content)
        if cover_image:
            post_input["coverImageOptions"] = {"coverImageURL": cover_image}

        if is_live:
            query = (
                "mutation PublishPost($input: PublishPostInput!) {"
                " publishPost(input: $input) { post { url } } }"
            )
        else:
            # Drafts do not accept publicationId/tags the same way; keep it minimal.
            post_input.pop("tags", None)
            query = (
                "mutation CreateDraft($input: CreateDraftInput!) {"
                " createDraft(input: $input) { draft { id } } }"
            )

        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }

        response = None
        try:
            response = requests.post(
                self._API_URL,
                headers=headers,
                json={"query": query, "variables": {"input": post_input}},
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("errors"):
                return PublishResult(self.name, False, error=str(payload["errors"]))

            data = payload.get("data", {})
            if is_live:
                url = data.get("publishPost", {}).get("post", {}).get("url")
                return PublishResult(self.name, True, url=url)
            # Draft: no public URL, but creation succeeded.
            draft_id = data.get("createDraft", {}).get("draft", {}).get("id")
            return PublishResult(self.name, bool(draft_id), url=None,
                                 error=None if draft_id else "Draft not created")
        except Exception as e:
            detail = response.text if response is not None else "No response"
            return PublishResult(self.name, False, error=f"{e} | {detail}")


# ---------------------------------------------------------------------------
# Factory + orchestration
# ---------------------------------------------------------------------------
#: Maps platform identifiers to their publisher classes.
_PUBLISHER_REGISTRY: Dict[str, Callable[..., BasePublisher]] = {
    MediumPublisher.name: MediumPublisher,
    DevToPublisher.name: DevToPublisher,
    HashnodePublisher.name: HashnodePublisher,
}


def build_publishers(
    config: Dict[str, Any],
    medium_html_converter: Callable[[str, str], str],
) -> List[BasePublisher]:
    """
    Build the list of enabled, properly configured publishers.

    A publisher is included when:
    1. Its name is listed in ``config["PUBLISH_PLATFORMS"]`` (defaults to
       ``["medium"]`` for backward compatibility), and
    2. ``is_configured()`` returns True (required credentials present).

    Args:
        config: Loaded configuration dictionary.
        medium_html_converter: Callable ``(markdown, title) -> html`` used by the
            Medium publisher to format content.

    Returns:
        List of ready-to-use ``BasePublisher`` instances.
    """
    requested = config.get("PUBLISH_PLATFORMS") or ["medium"]
    publishers: List[BasePublisher] = []

    for name in requested:
        factory = _PUBLISHER_REGISTRY.get(name)
        if factory is None:
            print(f"⚠ Unknown publish platform '{name}' — skipping")
            continue

        if name == MediumPublisher.name:
            publisher = factory(config, medium_html_converter)
        else:
            publisher = factory(config)

        if publisher.is_configured():
            publishers.append(publisher)
            print(f"✓ Publisher enabled: {name}")
        else:
            print(f"⚠ Publisher '{name}' is listed but not configured — skipping")

    if not publishers:
        print("⚠ No publishers configured — articles will only be saved locally")

    return publishers


def publish_to_all(
    publishers: List[BasePublisher],
    *,
    title: str,
    content: str,
    tags: List[str],
    output_language: str,
    niche: str,
) -> Dict[str, PublishResult]:
    """
    Publish one article to every configured platform.

    Returns a mapping of ``platform name -> PublishResult``. Failures on one
    platform never prevent attempts on the others.
    """
    results: Dict[str, PublishResult] = {}

    for publisher in publishers:
        try:
            result = publisher.publish(
                title=title,
                content=content,
                tags=tags,
                output_language=output_language,
                niche=niche,
            )
        except Exception as e:  # Defensive: a publisher should not crash the run.
            result = PublishResult(publisher.name, False, error=str(e))

        results[publisher.name] = result

        if result.success:
            location = result.url or "(draft created)"
            print(f"✓ Published to {publisher.name}: {location}")
        else:
            print(f"✗ Failed to publish to {publisher.name}: {result.error}")

    return results


def select_primary_url(results: Dict[str, PublishResult]) -> str:
    """
    Pick the best URL to record as the canonical reference for an article.

    Preference order: Medium URL, then any other platform URL, then a marker
    indicating a draft was created, else ``"not_published"``.
    """
    medium = results.get(MediumPublisher.name)
    if medium and medium.success and medium.url:
        return medium.url

    for result in results.values():
        if result.success and result.url:
            return result.url

    if any(result.success for result in results.values()):
        return "posted_as_draft"

    return "not_published"
