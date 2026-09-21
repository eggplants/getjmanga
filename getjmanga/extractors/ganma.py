"""GANMA! (ガンマ), whose web reader asks a GraphQL API that only takes persisted queries."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal, published_on

if TYPE_CHECKING:
    from datetime import date

    from httpx2 import Client

BASE_URL = "https://ganma.jp"
#: The Next.js app's backend. Every query the reader makes goes through here.
GRAPHQL_URL = f"{BASE_URL}/api/graphql"
LOGIN_URL = f"{BASE_URL}/api/3.0/session"
_LOGIN_PARAMS = {"mode": "mail", "clientType": "browser", "explicit": "true"}

# `/web/reader/<alias or magazine id>/<story id>/<page>`; the site 404s without the page number,
# but the API only needs the two ids, so it is optional here.
_EPISODE_PATH = re.compile(r"^/web/reader/(?P<magazine>[A-Za-z0-9_-]+)/(?P<story>[0-9a-f-]{36})(?:/\d+)?/?$")
# `/web/magazine/<alias or magazine id>`, and the `/magazine/<alias>` and `/<alias>` shapes that redirect to it.
_SERIES_PATH = re.compile(r"^(?:(?:/web)?/magazine)?/(?P<magazine>[A-Za-z0-9_-]+)/?$")
#: First path segments that are sections of the site, not magazine aliases.
_NOT_AN_ALIAS = frozenset({"web", "g", "api", "store", "magazine"})

#: What the browser adds to every API call. The API answers 400 without `x-from`.
_API_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json;charset=UTF-8",
    "x-noescape": "true",
}
#: How many stories to ask the listing for at once; the site pages with a cursor.
_LISTING_PAGE_SIZE = 100

#: The `storyContents` error the API answers for a story id the magazine does not have.
_STORY_NOT_FOUND = "STORY_NOT_FOUND"

# The persisted queries, verbatim from the site's bundle: the API refuses anything but a query whose
# SHA-256 it knows (`OnlyPersistedQueryIsAllowed`), so a changed byte is a rejected query.
_QUERIES = {
    "magazineStoryForReader": """query magazineStoryForReader($magazineIdOrAlias: String!, $storyId: String!) {
  magazine(magazineIdOrAlias: $magazineIdOrAlias) {
    authorName
    author {
      profileImageURL
    }
    magazineId
    alias
    title
    description
    distributionLabel
    isFinished
    overview
    rectangleWithLogoImageURL
    squareImageURL
    squareWithLogoImageURL
    storyLimitCount
    shareText
    totalStoryCount
    isWebOnlySensitive
    upcoming {
      title
      subtitle
      releaseAt
    }
    magazineRecommendation {
      magazines {
        magazineId
        alias
        title
        overview
        rectangleWithLogoImageURL
        squareImageURL
        squareWithLogoImageURL
      }
    }
    storyContents(storyId: $storyId) {
      __typename
      ... on StoryContents {
        storyInfo {
          isVerticalOnly
          title
          subtitle
          nextStoryInfo {
            isStoryCountLimited
            storyId
            storyThumbnailURLs
            title
            subtitle
            isPurchased
            appealMessage
            contentsAccessCondition {
              __typename
              ... on FreeStoryContentsAccessCondition {
                disableCM
              }
              ... on SubscriptionRequiredStoryContentsAccessCondition {
                reason
              }
              ... on PurchaseStoryRequiredStoryContentsAccessCondition {
                info {
                  coins
                  returnPercentage
                }
              }
              ... on PurchaseOrPremiumReadableStoryContentsAccessCondition {
                info {
                  coins
                  returnPercentage
                }
              }
            }
          }
          previousStoryInfo {
            storyId
          }
          generalUpcoming {
            title
            subtitle
            storyThumbnailURLs
            appealMessage
            generalReleaseAt
          }
        }
        pageImages {
          pageCount
          pageImageBaseURL
          pageImageSign
        }
        afterword {
          imageURL
          text
        }
        disableAd
        exchange {
          coverImageURL
        }
        storyEndImage {
          imageURL
          transition {
            destinationURL
          }
        }
        storyEndImageOnMagazine {
          imageURL
          transition {
            destinationURL
          }
        }
      }
      ... on StoryContentsError {
        error
      }
    }
  }
}""",
    "storyInfoList": "query storyInfoList($magazineIdOrAlias: String!, $first: Int, $after: String, "
    """$last: Int, $before: String) {
  magazine(magazineIdOrAlias: $magazineIdOrAlias) {
    magazineId
    totalStoryCount
    title
    authorName
    upcoming {
      __typename
      title
      subtitle
      releaseAt
    }
    storyInfos(first: $first, after: $after, last: $last, before: $before) {
      pageInfo {
        endCursor
        hasNextPage
        hasPreviousPage
        startCursor
      }
      edges {
        node {
          ...StoryInfoListItem
        }
      }
    }
  }
}

fragment StoryInfoListItem on StoryInfo {
  __typename
  storyId
  title
  subtitle
  storyThumbnailURLs
  isLast
  isSellByStory
  contentsAccessCondition {
    __typename
    ... on FreeStoryContentsAccessCondition {
      disableCM
    }
    ... on SubscriptionRequiredStoryContentsAccessCondition {
      reason
    }
    ... on PurchaseStoryRequiredStoryContentsAccessCondition {
      info {
        coins
        returnPercentage
      }
    }
    ... on PurchaseOrPremiumReadableStoryContentsAccessCondition {
      info {
        coins
        returnPercentage
      }
    }
  }
  isPurchased
  heartCount
  releaseForFree
  contentsRelease
  storyContents {
    __typename
    ... on StoryContentsError {
      __typename
      error
    }
  }
}""",
}


def episode_url(magazine: str, story_id: str) -> str:
    """The canonical URL of an episode.

    Args:
        magazine: The magazine's alias or id, `/web/reader/<magazine>/`.
        story_id: The story id, `/<story>/`.

    Returns:
        The reader URL, opened on its first page.
    """
    return f"{BASE_URL}/web/reader/{magazine}/{story_id}/0"


def story_title(story: dict[str, Any]) -> str:
    """Name a story the way the reader's header does.

    Args:
        story: A story as the API describes it, with `title` and `subtitle`.

    Returns:
        `<title> <subtitle>`, or just the title when there is no subtitle.
    """
    title = str(story.get("title") or "")
    subtitle = story.get("subtitle")
    return f"{title} {subtitle}" if subtitle else title


def page_urls(page_images: dict[str, Any]) -> list[str]:
    """Build the page URLs the reader builds from a `pageImages` answer.

    The CDN serves `<pageImageBaseURL><n>.jpg` for `n` from 1 to `pageCount`,
    and only with the CloudFront signature `pageImageSign` as the query string.

    Args:
        page_images: The `pageImages` of a readable `StoryContents`.

    Returns:
        One signed URL per page, in reading order.
    """
    base = str(page_images.get("pageImageBaseURL") or "")
    sign = str(page_images.get("pageImageSign") or "")
    count = int(page_images.get("pageCount") or 0)
    return [f"{base}{index}.jpg?{sign}" for index in range(1, count + 1)]


class Ganma(Extractor):
    """Fetch episodes from GANMA!."""

    NAME = "ganma"
    HOSTS = ("ganma.jp",)
    PUBLISHER = "コミスマ"
    URL_FORMS = (
        "https://ganma.jp/web/reader/<alias>/<story>/<page>",
        "https://ganma.jp/web/magazine/<alias>",
        "https://ganma.jp/magazine/<alias>",
        "https://ganma.jp/<alias>",
    )
    CONFIG_KEY = "ganma"
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # `storyInfoList` answers per magazine; a bulk run through locked
        # episodes asks for the same magazine once per episode, so it is kept.
        self._listings: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a reader page or a magazine page on `ganma.jp`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or cls._series_id(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole magazine rather than one story.

        Args:
            url: The URL to check.

        Returns:
            True for `/web/magazine/<alias>` and the shapes that redirect to it.
        """
        return cls._series_id(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every story of a magazine, first story first.

        Args:
            url: A magazine URL.

        Returns:
            One reader URL per listed story, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a magazine page.
            NotAnEpisodePageError: The site knows no such magazine, or it lists no story.
        """
        magazine = self._series_id(urlparse(url).path)
        if magazine is None:
            msg = f"{url} is not a magazine page."
            raise UnsupportedUrlError(msg)
        urls: list[str] = []
        for story in self._listing(magazine, url):
            candidate = episode_url(magazine, str(story.get("storyId") or ""))
            if story.get("storyId") and candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the magazine at {url} lists no story."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one story and list its pages.

        `magazineStoryForReader` answers a `StoryContents` with the signed
        page base for a readable story, or a `StoryContentsError` naming why
        not: `STORY_COUNT_LIMITED` (the web reader stops after the magazine's
        first few stories; the rest are app-only), `STORY_NOT_PURCHASED` (a
        coin story), `GANMA_PREMIUM_REQUIRED` or `PLATFORM_NOT_SUPPORTED`.
        Those come back with no pages; the magazine listing then names the
        story and the one after it.

        Args:
            url: The reader URL.

        Returns:
            The episode. `pages` is empty when the story is not readable.

        Raises:
            UnsupportedUrlError: The URL is not a reader URL.
            NotAnEpisodePageError: The site knows no such magazine or story.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a reader page."
            raise UnsupportedUrlError(msg)
        magazine_key, story_id = match["magazine"], match["story"]

        magazine = self._graphql(
            "magazineStoryForReader", {"magazineIdOrAlias": magazine_key, "storyId": story_id}, url
        )
        if magazine is None:
            msg = f"no magazine {magazine_key!r} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        contents = magazine.get("storyContents") or {}
        series_title = str(magazine.get("title") or magazine_key)
        writer = str(magazine.get("authorName") or "")

        released = self._release(magazine_key, story_id, url)
        number = ordinal([str(s.get("storyId")) for s in self._listing(magazine_key, url)], story_id)
        if contents.get("__typename") == "StoryContents":
            info = contents.get("storyInfo") or {}
            prev_story = (info.get("previousStoryInfo") or {}).get("storyId")
            next_story = (info.get("nextStoryInfo") or {}).get("storyId")
            return Episode(
                url=episode_url(magazine_key, story_id),
                series_title=series_title,
                episode_title=story_title(info) or story_id,
                pages=tuple(Page(url=src) for src in page_urls(contents.get("pageImages") or {})),
                prev_url=episode_url(magazine_key, str(prev_story)) if prev_story else None,
                next_url=episode_url(magazine_key, str(next_story)) if next_story else None,
                metadata=magazine,
                writer=writer,
                publisher=self.PUBLISHER,
                published=released,
                number=number,
            )

        if contents.get("error") == _STORY_NOT_FOUND:
            msg = f"no story {story_id} in the magazine {magazine_key!r} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        # Locked: the reader answer says nothing about the story itself, the listing does.
        stories = self._listing(magazine_key, url)
        index = next((i for i, story in enumerate(stories) if story.get("storyId") == story_id), None)
        story = stories[index] if index is not None else {}
        before, after = neighbours(stories, story) if index is not None else (None, None)
        return Episode(
            url=episode_url(magazine_key, story_id),
            series_title=series_title,
            episode_title=story_title(story) or story_id,
            pages=(),
            prev_url=episode_url(magazine_key, str(before.get("storyId"))) if before else None,
            next_url=episode_url(magazine_key, str(after.get("storyId"))) if after else None,
            metadata={**magazine, "storyInfo": story},
            writer=writer,
            publisher=self.PUBLISHER,
            published=released,
            number=number,
        )

    def _release(self, magazine_key: str, story_id: str, referer: str) -> date | None:
        """The day the listing says the story came out (`contentsRelease`); the reader answer has no date."""
        entry = next((s for s in self._listing(magazine_key, referer) if s.get("storyId") == story_id), None)
        return published_on(entry.get("contentsRelease")) if entry else None

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in with an email address, so purchased stories become readable.

        The sign-in page posts `{"mail", "password"}` as JSON to
        `/api/3.0/session`; the cookie the answer sets is what the GraphQL
        API reads afterwards. Accounts registered through Apple, Google, X or
        LINE cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            LOGIN_URL,
            params=_LOGIN_PARAMS,
            json={"mail": username, "password": password},
            headers={**self.HEADERS, **_API_HEADERS, "x-from": f"{BASE_URL}/web/signin", "Origin": BASE_URL},
            timeout=self.TIMEOUT,
        )
        if not res.is_success:
            try:
                body = res.json()
            except ValueError:
                body = None
            reason = str(body.get("message") or body.get("code") or "") if isinstance(body, dict) else ""
            msg = f"{BASE_URL} refused the credentials for {username!r} ({url}): {reason or f'HTTP {res.status_code}'}"
            raise LoginError(msg)

    @staticmethod
    def _series_id(path: str) -> str | None:
        """The magazine alias or id a series path names, or None when it is not one."""
        match = _SERIES_PATH.match(path)
        if match is None or match["magazine"] in _NOT_AN_ALIAS:
            return None
        return match["magazine"]

    def _graphql(self, operation: str, variables: dict[str, Any], referer: str) -> dict[str, Any] | None:
        """Run one persisted query and hand back its `magazine`, or None when the site knows none.

        Raises:
            NotAnEpisodePageError: The API refused the query.
        """
        query = _QUERIES[operation]
        res = self._session.post(
            GRAPHQL_URL,
            json={
                "operationName": operation,
                "query": query,
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha256(query.encode()).hexdigest()}},
            },
            headers={**self.HEADERS, **_API_HEADERS, "x-from": referer, "Origin": BASE_URL},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        body = res.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            errors = body.get("errors") if isinstance(body, dict) else None
            reason = ", ".join(str(error.get("message")) for error in errors) if isinstance(errors, list) else ""
            msg = f"{GRAPHQL_URL} refused {operation} for {referer}: {reason or 'no data'}"
            raise NotAnEpisodePageError(msg)
        magazine = data.get("magazine")
        return magazine if isinstance(magazine, dict) else None

    def _listing(self, magazine: str, referer: str) -> list[dict[str, Any]]:
        """Every story of a magazine as `storyInfoList` describes it, fetched once.

        Raises:
            NotAnEpisodePageError: The site knows no such magazine.
        """
        if magazine not in self._listings:
            stories: list[dict[str, Any]] = []
            after: str | None = None
            while True:
                answer = self._graphql(
                    "storyInfoList",
                    {"magazineIdOrAlias": magazine, "first": _LISTING_PAGE_SIZE, "after": after},
                    referer,
                )
                if answer is None:
                    msg = f"no magazine {magazine!r} on {BASE_URL}."
                    raise NotAnEpisodePageError(msg)
                infos = answer.get("storyInfos") or {}
                stories.extend(edge["node"] for edge in infos.get("edges") or [] if isinstance(edge.get("node"), dict))
                page_info = infos.get("pageInfo") or {}
                after = page_info.get("endCursor")
                if not page_info.get("hasNextPage") or not after:
                    break
            self._listings[magazine] = stories
        return self._listings[magazine]
