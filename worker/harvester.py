#!/usr/bin/env python3
"""Isolated media harvester worker for candidate portfolio URLs."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape as html_unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright
from yt_dlp import DownloadError, YoutubeDL

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
VIDEO_HOST_PATTERN = re.compile(r"(youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com)", re.IGNORECASE)
DIRECT_VIDEO_PATTERN = re.compile(r"\.(mp4|webm|m3u8|m4v|mov)(?:[\?#].*)?$", re.IGNORECASE)
DRIVE_HOST_PATTERN = re.compile(r"(?:^|\.)drive\.google\.com$", re.IGNORECASE)
DRIVE_FILE_PATH_PATTERN = re.compile(r"/file/d/([^/?#]+)", re.IGNORECASE)
DRIVE_FOLDER_PATH_PATTERN = re.compile(r"/drive/(?:u/\d+/)?folders/([^/?#]+)", re.IGNORECASE)
DRIVE_EMBEDDED_ANCHOR_PATTERN = re.compile(
    r"<a[^>]+href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
VIDEO_FILE_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
STREAM_VIDEO_PATTERN = re.compile(r"\.m3u8(?:$|\?|#)", re.IGNORECASE)
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,}$")
YOUTUBE_THUMBNAIL_PATTERN = re.compile(
    r"(?:img\.youtube\.com|i\.ytimg\.com)/(?:vi|vi_webp)/([A-Za-z0-9_-]{6,})/",
    re.IGNORECASE,
)
URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

MEDIA_PATTERNS = [
    VIDEO_HOST_PATTERN,
    DIRECT_VIDEO_PATTERN,
    STREAM_VIDEO_PATTERN,
    re.compile(r"instagram\.com/(?:p|reel)/"),
]

MIN_IMAGE_DIMENSION = 250
MAX_VIDEO_HEIGHT = 1080
MAX_VIDEO_FILESIZE_BYTES = 200 * 1024 * 1024
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 180
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


@dataclass
class FallbackEntry:
    source_url: str
    reason: str
    screenshot_path: Path | None


def log(event: str, **fields: object) -> None:
    """Print structured worker logs to stdout."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in fields.items())
    if payload:
        print(f"[{stamp}] {event} {payload}", flush=True)
        return
    print(f"[{stamp}] {event}", flush=True)


def log_exception(event: str, exc: BaseException, **fields: object) -> None:
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log(event, error_type=type(exc).__name__, error=str(exc), traceback=trace, **fields)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Portfolio media harvester worker")
    parser.add_argument("--url", required=True, help="Candidate portfolio URL")
    parser.add_argument("--job-id", default="", help="Job ID used for Drive upload folder naming")
    parser.add_argument(
        "--output",
        default="./temp_harvest",
        help="Directory where harvested files are saved (default: ./temp_harvest)",
    )
    args = parser.parse_args()
    log(
        "args_parsed",
        url=args.url,
        job_id=args.job_id,
        output=args.output,
        cwd=os.getcwd(),
        python_executable=sys.executable,
    )
    return args


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def is_video_candidate(url: str) -> bool:
    lowered = url.lower()
    return any(pat.search(lowered) for pat in MEDIA_PATTERNS)


def is_probable_video_response(url: str, content_type: str, resource_type: str) -> bool:
    lowered_url = url.lower()
    lowered_content_type = content_type.lower()
    lowered_resource_type = resource_type.lower()

    if is_video_candidate(url):
        return True
    if lowered_resource_type == "media":
        return True
    if "video/" in lowered_content_type:
        return True
    if any(
        hint in lowered_content_type
        for hint in (
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "application/dash+xml",
        )
    ):
        return True
    if any(
        hint in lowered_url
        for hint in (
            ".mpd",
            "format=mp4",
            "mime=video",
            "contenttype=video",
        )
    ):
        return True
    return False


def canonicalize_video_request_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return parsed._replace(fragment="").geturl()

    volatile_query_params = {
        "range",
        "start",
        "end",
        "bytestart",
        "byteend",
        "rn",
        "rbuf",
    }

    retained_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in volatile_query_params
    ]
    canonical_query = urlencode(retained_pairs, doseq=True)
    return parsed._replace(query=canonical_query, fragment="").geturl()


def is_unusable_segment_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in (".m4s", ".ts?", "/chunk/", "segment="))


def is_drive_folder_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not DRIVE_HOST_PATTERN.search(host):
        return False

    if DRIVE_FOLDER_PATH_PATTERN.search(parsed.path):
        return True

    if parsed.path.lower().startswith("/folderview"):
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        return any(key.lower() == "id" and value for key, value in query_pairs)

    return False


def normalize_drive_video_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = parsed.netloc.lower()
    if not DRIVE_HOST_PATTERN.search(host):
        return None

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)

    folder_match = DRIVE_FOLDER_PATH_PATTERN.search(parsed.path)
    if folder_match:
        folder_id = folder_match.group(1)
        resource_key = next(
            (value for key, value in query_pairs if key.lower() == "resourcekey" and value),
            "",
        )
        canonical_query = urlencode([("resourcekey", resource_key)]) if resource_key else ""
        return parsed._replace(
            netloc="drive.google.com",
            path=f"/drive/folders/{folder_id}",
            query=canonical_query,
            fragment="",
        ).geturl()

    if parsed.path.lower().startswith("/folderview"):
        folder_id = next(
            (value for key, value in query_pairs if key.lower() == "id" and value),
            "",
        )
        if folder_id:
            resource_key = next(
                (value for key, value in query_pairs if key.lower() == "resourcekey" and value),
                "",
            )
            canonical_query = urlencode([("resourcekey", resource_key)]) if resource_key else ""
            return parsed._replace(
                netloc="drive.google.com",
                path=f"/drive/folders/{folder_id}",
                query=canonical_query,
                fragment="",
            ).geturl()

    file_id = ""
    file_match = DRIVE_FILE_PATH_PATTERN.search(parsed.path)
    if file_match:
        file_id = file_match.group(1)
    else:
        for key, value in query_pairs:
            if key.lower() == "id" and value:
                file_id = value
                break

    if not file_id:
        return None

    resource_key = next(
        (value for key, value in query_pairs if key.lower() == "resourcekey" and value),
        "",
    )
    canonical_path = f"/file/d/{file_id}/view"
    canonical_query = urlencode([("resourcekey", resource_key)]) if resource_key else ""
    return parsed._replace(
        netloc="drive.google.com",
        path=canonical_path,
        query=canonical_query,
        fragment="",
    ).geturl()


def extract_drive_folder_id(url: str) -> str | None:
    parsed = urlparse(url)
    if not DRIVE_HOST_PATTERN.search(parsed.netloc.lower()):
        return None

    folder_match = DRIVE_FOLDER_PATH_PATTERN.search(parsed.path)
    if folder_match:
        return folder_match.group(1)

    if parsed.path.lower().startswith("/folderview"):
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() == "id" and value:
                return value

    return None


async def extract_drive_folder_video_urls(folder_url: str) -> list[str]:
    folder_id = extract_drive_folder_id(folder_url)
    if not folder_id:
        return []

    embedded_url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    timeout = aiohttp.ClientTimeout(total=60)
    headers = {"User-Agent": USER_AGENT}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(embedded_url, allow_redirects=True) as response:
                if response.status != 200:
                    log(
                        "drive_folder_scan_error",
                        source=folder_url,
                        embedded_url=embedded_url,
                        status=response.status,
                    )
                    return []
                html = await response.text(errors="ignore")
    except Exception as exc:  # noqa: BLE001 - keep extraction resilient
        log(
            "drive_folder_scan_error",
            source=folder_url,
            embedded_url=embedded_url,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return []

    discovered: list[str] = []
    seen: set[str] = set()

    for match in DRIVE_EMBEDDED_ANCHOR_PATTERN.finditer(html):
        href = html_unescape(match.group("href") or "").strip()
        if not href:
            continue

        label_html = match.group("label") or ""
        label = html_unescape(re.sub(r"<[^>]+>", "", label_html)).strip()
        normalized_href = normalize_url(embedded_url, href)
        if not normalized_href:
            continue

        drive_url = normalize_drive_video_url(normalized_href)
        if not drive_url or is_drive_folder_url(drive_url):
            continue

        label_ext = Path(label).suffix.lower() if label else ""
        if label_ext and label_ext not in VIDEO_FILE_EXTENSIONS:
            continue

        if drive_url in seen:
            continue

        seen.add(drive_url)
        discovered.append(drive_url)
        log(
            "drive_video_detected",
            source=drive_url,
            drive_type="file",
            phase="drive_folder_scan",
            label=label,
        )

    log(
        "drive_folder_scan_complete",
        source=folder_url,
        embedded_url=embedded_url,
        discovered=len(discovered),
    )
    return discovered


def normalize_url(base_url: str, raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    candidate = raw_value.strip()
    if not candidate:
        return None

    if candidate.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:")):
        return None

    absolute = urljoin(base_url, candidate)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return absolute


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if "youtu.be" in host and path:
        short_id = path.split("/", maxsplit=1)[0]
        if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(short_id):
            return short_id

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        lower_path = path.lower()

        if lower_path == "watch":
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                if key == "v" and YOUTUBE_VIDEO_ID_PATTERN.fullmatch(value):
                    return value

        for prefix in ("embed/", "shorts/", "live/"):
            if lower_path.startswith(prefix):
                candidate = path[len(prefix):].split("/", maxsplit=1)[0]
                if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(candidate):
                    return candidate

        if lower_path == "attribution_link":
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                if key != "u" or not value:
                    continue
                nested_url = normalize_url(url, value)
                if not nested_url:
                    continue
                nested_id = extract_youtube_video_id(nested_url)
                if nested_id:
                    return nested_id

    return None


def expand_video_url_candidate(url: str, depth: int = 0) -> set[str]:
    if depth > 2:
        return set()

    parsed = urlparse(url)
    canonical = parsed._replace(fragment="").geturl()
    host = parsed.netloc.lower()
    discovered: set[str] = set()

    drive_url = normalize_drive_video_url(canonical)
    if drive_url:
        discovered.add(drive_url)

    youtube_id = extract_youtube_video_id(canonical)
    if youtube_id:
        discovered.add(youtube_watch_url(youtube_id))

    thumbnail_match = YOUTUBE_THUMBNAIL_PATTERN.search(canonical)
    if thumbnail_match:
        discovered.add(youtube_watch_url(thumbnail_match.group(1)))

    for _, value in parse_qsl(parsed.query, keep_blank_values=True):
        if not value:
            continue

        nested_inputs = {value.strip()}
        decoded_value = unquote(value).strip()
        if decoded_value:
            nested_inputs.add(decoded_value)

        for nested_input in nested_inputs:
            if not nested_input:
                continue
            if not nested_input.startswith(("http://", "https://", "//", "/")):
                continue
            nested_url = normalize_url(canonical, nested_input)
            if not nested_url and nested_input.startswith(("http://", "https://")):
                nested_url = nested_input
            if nested_url:
                discovered.update(expand_video_url_candidate(nested_url, depth + 1))

    is_youtube_thumbnail = "img.youtube.com" in host or "ytimg.com" in host
    is_youtube_host = "youtube.com" in host or "youtube-nocookie.com" in host or "youtu.be" in host

    if is_youtube_host:
        if youtube_id:
            discovered.add(youtube_watch_url(youtube_id))
    elif drive_url:
        discovered.add(drive_url)
    elif not is_youtube_thumbnail and is_video_candidate(canonical):
        discovered.add(canonicalize_video_request_url(canonical))

    return discovered


def extract_video_urls_from_values(base_url: str, values: Iterable[str]) -> set[str]:
    discovered: set[str] = set()
    seen_candidates: set[str] = set()

    for value in values:
        raw = value.strip() if isinstance(value, str) else ""
        if not raw:
            continue

        candidate_values = {raw}
        decoded_raw = unquote(raw)
        if decoded_raw and decoded_raw != raw:
            candidate_values.add(decoded_raw)

        for candidate in list(candidate_values):
            for match in URL_IN_TEXT_PATTERN.findall(candidate):
                candidate_values.add(match.rstrip(")],.;\"'"))

        for candidate in candidate_values:
            cleaned = candidate.strip()
            if not cleaned or cleaned in seen_candidates:
                continue
            seen_candidates.add(cleaned)

            normalized = normalize_url(base_url, cleaned)
            if not normalized and cleaned.startswith(("http://", "https://")):
                normalized = cleaned
            if not normalized:
                continue

            discovered.update(expand_video_url_candidate(normalized))

    return discovered


def infer_extension(url: str, content_type: str, default_ext: str) -> str:
    path_ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if path_ext and re.fullmatch(r"\.[a-z0-9]{1,8}", path_ext):
        return ".jpg" if path_ext == ".jpe" else path_ext

    clean_content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    guessed = mimetypes.guess_extension(clean_content_type) if clean_content_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed

    return default_ext


async def ensure_fully_rendered(page: Page, url: str) -> None:
    log("navigation_start", url=url)
    try:
        await page.goto(url, wait_until="networkidle", timeout=90_000)
    except PlaywrightTimeoutError:
        # Some sites keep long-lived background requests open indefinitely.
        log(
            "navigation_networkidle_timeout",
            timeout_ms=90_000,
            current_url=page.url,
        )
        await page.wait_for_selector("body", state="attached", timeout=10_000)
        await page.wait_for_timeout(1_500)

    print("[PRE-FLIGHT] Starting incremental scroll sweep to trigger lazy-loaded media...")
    max_scroll_steps = 60
    steps_taken = 0
    reached_bottom = False
    last_scroller = ""
    for _ in range(max_scroll_steps):
        steps_taken += 1
        step_state = await page.evaluate(
            """
            () => {
                const docScroller = document.scrollingElement || document.documentElement;
                const windowViewport = window.innerHeight || document.documentElement.clientHeight || 900;
                const windowTop = window.scrollY || window.pageYOffset || docScroller.scrollTop || 0;
                const documentHeight = Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight,
                    docScroller.scrollHeight || 0
                );
                const windowCanScroll = documentHeight > windowViewport + 2;

                let target = null;
                let targetKind = "none";

                if (windowCanScroll) {
                    target = docScroller;
                    targetKind = "window";
                } else {
                    const candidates = Array.from(document.querySelectorAll("main, [role='main'], [data-scroll], [class*='scroll'], div, section"));
                    let best = null;
                    let bestDelta = 0;

                    for (const candidate of candidates) {
                        if (!(candidate instanceof HTMLElement)) {
                            continue;
                        }
                        const style = window.getComputedStyle(candidate);
                        const overflowY = style.overflowY || "";
                        if (!["auto", "scroll", "overlay"].includes(overflowY)) {
                            continue;
                        }

                        const delta = candidate.scrollHeight - candidate.clientHeight;
                        if (delta > bestDelta && candidate.clientHeight > 120) {
                            bestDelta = delta;
                            best = candidate;
                        }
                    }

                    if (best) {
                        target = best;
                        targetKind = "element";
                    }
                }

                if (!target) {
                    return {
                        reachedBottom: true,
                        moved: false,
                        scroller: "none",
                        top: 0,
                        total: 0,
                    };
                }

                if (targetKind === "window") {
                    const currentTop = windowTop;
                    const alreadyAtBottom = currentTop + windowViewport >= documentHeight - 2;
                    if (!alreadyAtBottom) {
                        window.scrollBy(0, windowViewport);
                    }
                    const nextTop = window.scrollY || window.pageYOffset || docScroller.scrollTop || 0;
                    const refreshedDocumentHeight = Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight,
                        docScroller.scrollHeight || 0
                    );

                    return {
                        reachedBottom: nextTop + windowViewport >= refreshedDocumentHeight - 2,
                        moved: nextTop > currentTop,
                        scroller: "window",
                        top: nextTop,
                        total: refreshedDocumentHeight,
                    };
                }

                const element = target;
                const viewportHeight = element.clientHeight;
                const currentTop = element.scrollTop;
                const totalHeight = element.scrollHeight;
                const alreadyAtBottom = currentTop + viewportHeight >= totalHeight - 2;
                if (!alreadyAtBottom) {
                    element.scrollTop = Math.min(totalHeight, currentTop + viewportHeight);
                }
                const nextTop = element.scrollTop;
                const refreshedTotalHeight = element.scrollHeight;
                const classSummary = (element.className || "").toString().trim().split(/\\s+/).slice(0, 2).join(".");
                const scrollerName = `${element.tagName.toLowerCase()}${element.id ? '#' + element.id : ''}${classSummary ? '.' + classSummary : ''}`;

                return {
                    reachedBottom: nextTop + viewportHeight >= refreshedTotalHeight - 2,
                    moved: nextTop > currentTop,
                    scroller: scrollerName,
                    top: nextTop,
                    total: refreshedTotalHeight,
                };
            }
            """
        )

        scroller_name = step_state.get("scroller", "") if isinstance(step_state, dict) else ""
        if scroller_name and scroller_name != last_scroller:
            print(f"[PRE-FLIGHT] Active scroller: {scroller_name}")
            last_scroller = scroller_name

        reached_bottom = bool(step_state.get("reachedBottom", False)) if isinstance(step_state, dict) else False
        await page.wait_for_timeout(1_500)
        if reached_bottom:
            break

    await page.evaluate(
        """
        () => {
            window.scrollTo(0, 0);
            document.querySelectorAll("*").forEach((el) => {
                if (el instanceof HTMLElement && el.scrollTop > 0) {
                    el.scrollTop = 0;
                }
            });
        }
        """
    )
    print("[PRE-FLIGHT] Scroll sweep complete.")
    log("preflight_scroll_summary", steps_taken=steps_taken, max_steps=max_scroll_steps, reached_bottom=reached_bottom)
    log("navigation_complete", final_url=page.url)


async def extract_media_urls(page: Page) -> tuple[set[str], set[str], set[str], list[str]]:
    dom_snapshot = await page.evaluate(
        r"""
        () => {
            const values = {
                images: new Set(),
                links: new Set(),
                driveLinks: new Set(),
                skippedLogoImages: [],
                candidates: [],
            };

            const push = (targetSet, raw) => {
                if (!raw || typeof raw !== "string") {
                    return;
                }
                const trimmed = raw.trim();
                if (trimmed) {
                    targetSet.add(trimmed);
                }
            };

            const hasLogoKeyword = (raw) => typeof raw === "string" && /(logo|icon|favicon)/i.test(raw);
            const isDriveVideoLink = (raw) => typeof raw === "string" && /drive\.google\.com\/(?:file\/d\/|drive\/(?:u\/\d+\/)?folders\/|folderview\?id=|open\?|uc\?)/i.test(raw);
            const mediaHrefPattern = /(youtube\.com|youtu\.be|vimeo\.com|instagram\.com\/(?:p|reel)\/|\.m3u8(?:$|\?|#)|\.(mp4|m4v|mov|webm)(?:$|\?|#))/i;
            const isMediaHref = (raw) => typeof raw === "string" && mediaHrefPattern.test(raw.trim());

            document.querySelectorAll("img").forEach((img) => {
                const candidateSources = new Set();
                push(candidateSources, img.currentSrc);
                push(candidateSources, img.getAttribute("src"));
                push(candidateSources, img.getAttribute("data-src"));
                push(candidateSources, img.getAttribute("data-lazy-src"));

                const srcset = img.getAttribute("srcset") || img.getAttribute("data-srcset") || "";
                srcset.split(",").forEach((entry) => {
                    push(candidateSources, entry.trim().split(/\\s+/)[0]);
                });

                const srcValue = img.currentSrc || img.getAttribute("src") || "";
                const altValue = img.getAttribute("alt") || "";
                const classValue = img.getAttribute("class") || "";
                const idValue = img.getAttribute("id") || "";

                if ([srcValue, altValue, classValue, idValue].some(hasLogoKeyword)) {
                    values.skippedLogoImages.push({
                        src: srcValue,
                        alt: altValue,
                        className: classValue,
                        id: idValue,
                    });
                    return;
                }

                candidateSources.forEach((source) => {
                    push(values.images, source);
                });
            });

            document.querySelectorAll("a[href]").forEach((anchor) => {
                const href = anchor.getAttribute("href");
                push(values.links, href);
                if (isDriveVideoLink(href)) {
                    push(values.driveLinks, href);
                }
            });

            let candidateCounter = 0;
            const candidateFingerprints = new Set();

            const registerCandidate = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return;
                }

                const style = window.getComputedStyle(element);
                if (!style || style.opacity === "0" || style.display === "none" || style.visibility === "hidden") {
                    return;
                }

                const rect = element.getBoundingClientRect();
                if (rect.width <= 150 || rect.height <= 100) {
                    return;
                }

                const fingerprint = `${Math.round(rect.left)}:${Math.round(rect.top)}:${Math.round(rect.width)}:${Math.round(rect.height)}`;
                if (candidateFingerprints.has(fingerprint)) {
                    return;
                }

                candidateFingerprints.add(fingerprint);
                const existingId = element.getAttribute("data-heuristic-id");
                const uid = existingId || ("h-" + (++candidateCounter));
                element.setAttribute("data-heuristic-id", uid);
                values.candidates.push(uid);
            };

            const thumbnailPattern = /(img\.youtube\.com|i\.ytimg\.com|drive\.google\.com\/thumbnail|\/thumbnails\/|_next\/image\?url=)/i;
            document.querySelectorAll("img").forEach((img) => {
                const src = img.currentSrc || img.getAttribute("src") || "";
                if (!thumbnailPattern.test(src)) {
                    return;
                }

                let node = img;
                while (node && node !== document.body) {
                    if (!(node instanceof HTMLElement)) {
                        break;
                    }

                    const nodeStyle = window.getComputedStyle(node);
                    const role = (node.getAttribute("role") || "").toLowerCase();
                    const tagName = node.tagName.toLowerCase();
                    const isClickable = nodeStyle.cursor === "pointer"
                        || ["a", "button"].includes(tagName)
                        || role === "button"
                        || node.hasAttribute("onclick");

                    if (isClickable) {
                        registerCandidate(node);
                        break;
                    }

                    node = node.parentElement;
                }
            });

            document.querySelectorAll("*").forEach((el) => {
                const style = window.getComputedStyle(el);
                if (!style || style.opacity === "0" || style.display === "none" || style.visibility === "hidden") {
                    return;
                }

                const isClickable = (style.cursor === "pointer") || ["a", "button"].includes(el.tagName.toLowerCase());
                if (!isClickable) {
                    return;
                }

                const hasImgOrBg = el.querySelector("img") !== null || (style.backgroundImage && style.backgroundImage !== "none");
                if (!hasImgOrBg) {
                    return;
                }

                const anchors = el.tagName.toLowerCase() === "a"
                    ? [el]
                    : Array.from(el.querySelectorAll("a[href]"));
                const linksToDirectMedia = anchors.some((anchor) => {
                    const href = anchor.getAttribute("href") || anchor.href || "";
                    return isMediaHref(href);
                });
                if (linksToDirectMedia) {
                    return;
                }

                registerCandidate(el);
            });

            return {
                images: [...values.images],
                links: [...values.links],
                drive_links: [...values.driveLinks],
                skipped_logo_images: values.skippedLogoImages,
                candidates: values.candidates,
            };
        }
        """
    )

    base_url = page.url

    image_urls: set[str] = set()
    pdf_urls: set[str] = set()
    video_urls: set[str] = set()

    for skipped_logo in dom_snapshot.get("skipped_logo_images", []):
        log(
            "logo_image_skipped",
            src=skipped_logo.get("src", ""),
            alt=skipped_logo.get("alt", ""),
            class_name=skipped_logo.get("className", ""),
            element_id=skipped_logo.get("id", ""),
        )

    for raw in dom_snapshot.get("images", []):
        normalized = normalize_url(base_url, raw)
        if normalized:
            image_urls.add(normalized)

    for raw in dom_snapshot.get("links", []):
        normalized = normalize_url(base_url, raw)
        if not normalized:
            continue
        if is_pdf_url(normalized):
            pdf_urls.add(normalized)
        if is_video_candidate(normalized):
            video_urls.add(normalized)

    for raw in dom_snapshot.get("drive_links", []):
        normalized = normalize_url(base_url, raw)
        if not normalized:
            continue

        drive_url = normalize_drive_video_url(normalized)
        if not drive_url:
            continue

        video_urls.add(drive_url)
        log(
            "drive_video_detected",
            source=drive_url,
            drive_type="folder" if is_drive_folder_url(drive_url) else "file",
            phase="static_link_extraction",
        )

    candidates = dom_snapshot.get("candidates", [])

    log(
        "media_discovered",
        image_candidates=len(image_urls),
        pdf_candidates=len(pdf_urls),
        video_candidates=len(video_urls),
        heuristic_candidates=len(candidates),
    )
    return image_urls, pdf_urls, video_urls, candidates


async def extract_dynamic_video_urls(page: Page) -> set[str]:
    raw_values = await page.evaluate(
        r"""
        () => {
            const values = new Set();

            const push = (raw) => {
                if (!raw || typeof raw !== "string") {
                    return;
                }
                const trimmed = raw.trim();
                if (!trimmed || trimmed.startsWith("data:") || trimmed.startsWith("javascript:")) {
                    return;
                }
                values.add(trimmed);
            };

            document.querySelectorAll("iframe[src], a[href], video[src], video[poster], source[src], img[src], [data-src], [data-url], [data-video], [data-video-url], [data-href], [data-iframe-src], [data-embed]").forEach((el) => {
                push(el.getAttribute("src"));
                push(el.getAttribute("href"));
                push(el.getAttribute("poster"));
                push(el.getAttribute("data-src"));
                push(el.getAttribute("data-url"));
                push(el.getAttribute("data-video"));
                push(el.getAttribute("data-video-url"));
                push(el.getAttribute("data-href"));
                push(el.getAttribute("data-iframe-src"));
                push(el.getAttribute("data-embed"));

                if (el instanceof HTMLImageElement) {
                    push(el.currentSrc);
                    const srcset = el.getAttribute("srcset") || "";
                    srcset.split(",").forEach((entry) => {
                        push(entry.trim().split(/\s+/)[0]);
                    });
                }
            });

            const html = document.documentElement ? document.documentElement.outerHTML : "";
            const patterns = [
                /https?:\/\/(?:www\.)?youtube(?:-nocookie)?\.com\/watch\?[^\s\"'<>]+/gi,
                /https?:\/\/(?:www\.)?youtube(?:-nocookie)?\.com\/embed\/[A-Za-z0-9_-]{6,}[^\s\"'<>]*/gi,
                /https?:\/\/youtu\.be\/[A-Za-z0-9_-]{6,}[^\s\"'<>]*/gi,
                /https?:\/\/(?:www\.)?vimeo\.com\/[0-9]+[^\s\"'<>]*/gi,
                /https?:\/\/[^\"'\s<>]*_next\/image\?url=[^\"'\s<>]+/gi,
                /https?:\/\/(?:img\.youtube\.com|i\.ytimg\.com)\/[^\s\"'<>]+/gi,
            ];

            patterns.forEach((pattern) => {
                const matches = html.match(pattern) || [];
                matches.forEach(push);
            });

            return Array.from(values);
        }
        """
    )

    return extract_video_urls_from_values(page.url, raw_values)


async def dismiss_interaction_overlay(page: Page) -> None:
    close_selectors = [
        'button[aria-label*="close" i]',
        '[role="button"][aria-label*="close" i]',
        'button[class*="close" i]',
        '[data-dismiss="dialog"]',
        '[data-state="open"] button[class*="close" i]',
    ]

    for selector in close_selectors:
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            continue

        button = locator.first
        try:
            if await button.is_visible():
                await button.click(timeout=1_000, force=True)
                await page.wait_for_timeout(150)
        except Exception:
            continue

    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
        except Exception:
            continue

    try:
        await page.mouse.click(5, 5)
        await page.wait_for_timeout(150)
    except Exception:
        return


async def click_locator_with_fallback(page: Page, locator) -> bool:
    try:
        await locator.click(timeout=5_000, force=True)
        return True
    except Exception:
        pass

    try:
        await locator.evaluate(
            """
            (element) => {
                element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
                ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach((eventName) => {
                    element.dispatchEvent(new MouseEvent(eventName, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                    }));
                });
            }
            """
        )
        return True
    except Exception:
        pass

    try:
        box = await locator.bounding_box()
        if not box:
            return False
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return True
    except Exception:
        return False


async def collect_click_revealed_video_urls(
    page: Page,
    candidates: Iterable[str],
    baseline_urls: Iterable[str],
) -> set[str]:
    discovered_urls = set(baseline_urls)
    candidate_ids = list(candidates)

    log("heuristic_discovery_start", total_candidates=len(candidate_ids))

    for index, uid in enumerate(candidate_ids, start=1):
        locator = page.locator(f'[data-heuristic-id="{uid}"]').first
        try:
            count = await locator.count()
            if count == 0:
                continue
            if not await locator.is_visible():
                continue

            await locator.scroll_into_view_if_needed(timeout=5_000)
            await page.wait_for_timeout(150)

            clicked = await click_locator_with_fallback(page, locator)
            if not clicked:
                log("heuristic_trigger_skipped", candidate=uid, reason="click_failed", index=index)
                continue

            # Framefolio-style players usually mount 1-2 seconds after click.
            await page.wait_for_timeout(2_000)
            try:
                await page.wait_for_selector(
                    'iframe[src*="youtube"], iframe[src*="youtu"], iframe[src*="vimeo"], a[href*="youtube.com/watch"], a[href*="youtu.be"], video',
                    timeout=2_500,
                )
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(350)

            dom_urls = await extract_dynamic_video_urls(page)
            new_urls = dom_urls - discovered_urls
            if new_urls:
                log(
                    "heuristic_video_urls_extracted",
                    candidate=uid,
                    index=index,
                    new_count=len(new_urls),
                    sample=sorted(new_urls)[:5],
                )
            discovered_urls.update(dom_urls)
        except Exception as exc:  # noqa: BLE001 - keep extraction resilient
            log("heuristic_trigger_error", candidate=uid, index=index, error=str(exc))
        finally:
            await dismiss_interaction_overlay(page)

    return discovered_urls


async def download_url(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    stem: str,
    default_ext: str,
) -> Path | None:
    log("download_start", source=url)
    try:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                log("download_skipped", source=url, status=response.status)
                return None

            extension = infer_extension(url, response.headers.get("Content-Type", ""), default_ext)
            destination = output_dir / f"{stem}{extension}"

            with destination.open("wb") as file_handle:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    file_handle.write(chunk)

            log("download_complete", source=url, saved_as=destination.name)
            return destination
    except Exception as exc:  # noqa: BLE001 - resilience for unreliable web sources
        log("download_error", source=url, error_type=type(exc).__name__, error=str(exc))
        return None


async def download_assets(
    image_urls: Iterable[str],
    pdf_urls: Iterable[str],
    output_dir: Path,
) -> tuple[list[Path], list[Path]]:
    image_files: list[Path] = []
    doc_files: list[Path] = []

    timeout = aiohttp.ClientTimeout(total=90)
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for index, image_url in enumerate(sorted(set(image_urls)), start=1):
            path = await download_url(session, image_url, output_dir, f"raw_image_{index:03d}", ".jpg")
            if path:
                try:
                    with Image.open(path) as image:
                        width, height = image.size
                except Exception as exc:  # noqa: BLE001 - skip unreadable images
                    log("image_dimension_check_error", file=path.name, error_type=type(exc).__name__, error=str(exc))
                    path.unlink(missing_ok=True)
                    continue

                if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                    log(
                        "image_small_skipped",
                        file=path.name,
                        width=width,
                        height=height,
                        min_size=MIN_IMAGE_DIMENSION,
                    )
                    path.unlink(missing_ok=True)
                    continue

                image_files.append(path)

        for index, pdf_url in enumerate(sorted(set(pdf_urls)), start=1):
            path = await download_url(session, pdf_url, output_dir, f"raw_doc_{index:03d}", ".pdf")
            if path:
                doc_files.append(path)

    log("asset_download_summary", images=len(image_files), docs=len(doc_files))
    return image_files, doc_files


async def download_direct_video_url(page: Page, video_url: str, output_dir: Path, ordinal: int) -> Path | None:
    browser_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "video/*,*/*;q=0.9",
        "Referer": page.url,
    }

    try:
        browser_response = await page.context.request.get(
            video_url,
            fail_on_status_code=False,
            timeout=120_000,
            headers=browser_headers,
        )
        if browser_response.status in {200, 206}:
            browser_content_type = browser_response.headers.get("content-type", "")
            normalized_browser_content_type = browser_content_type.split(";", maxsplit=1)[0].strip().lower()
            if not normalized_browser_content_type or normalized_browser_content_type.startswith("video/"):
                length_header = browser_response.headers.get("content-length", "").strip()
                if length_header.isdigit() and int(length_header) > MAX_VIDEO_FILESIZE_BYTES:
                    log(
                        "video_direct_download_skipped",
                        source=video_url,
                        reason="content_length_exceeded",
                        content_length=int(length_header),
                    )
                else:
                    body = await browser_response.body()
                    if body and len(body) <= MAX_VIDEO_FILESIZE_BYTES:
                        extension = infer_extension(video_url, browser_content_type, ".mp4")
                        destination = output_dir / f"raw_video_{ordinal:03d}_direct{extension}"
                        with destination.open("wb") as file_handle:
                            file_handle.write(body)

                        if destination.suffix.lower() in VIDEO_FILE_EXTENSIONS:
                            return destination

                        destination.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - continue to aiohttp fallback
        log(
            "video_direct_download_browser_error",
            source=video_url,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    timeout = aiohttp.ClientTimeout(total=180)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "video/*,*/*;q=0.9",
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(video_url, allow_redirects=True) as response:
                if response.status not in {200, 206}:
                    log("video_direct_download_skipped", source=video_url, status=response.status)
                    return None

                content_type = response.headers.get("Content-Type", "")
                normalized_content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
                if normalized_content_type and not (
                    normalized_content_type.startswith("video/")
                    or normalized_content_type in {
                        "application/vnd.apple.mpegurl",
                        "application/x-mpegurl",
                        "application/dash+xml",
                        "application/octet-stream",
                    }
                ):
                    log(
                        "video_direct_download_skipped",
                        source=video_url,
                        reason="content_type_not_video",
                        content_type=normalized_content_type,
                    )
                    return None

                extension = infer_extension(video_url, content_type, ".mp4")
                destination = output_dir / f"raw_video_{ordinal:03d}_direct{extension}"

                with destination.open("wb") as file_handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        file_handle.write(chunk)

                if destination.suffix.lower() not in VIDEO_FILE_EXTENSIONS:
                    destination.unlink(missing_ok=True)
                    log(
                        "video_direct_download_skipped",
                        source=video_url,
                        reason="unsupported_extension",
                        extension=extension,
                    )
                    return None

                return destination
    except Exception as exc:  # noqa: BLE001 - direct fallback should never crash worker
        log("video_direct_download_error", source=video_url, error_type=type(exc).__name__, error=str(exc))
        return None


def _download_video_sync(video_url: str, output_dir: Path, ordinal: int) -> list[Path]:
    prefix = f"raw_video_{ordinal:03d}"
    is_drive_folder_source = is_drive_folder_url(video_url)
    template_name = f"{prefix}_%(autonumber)03d.%(ext)s" if is_drive_folder_source else f"{prefix}.%(ext)s"
    template = str(output_dir / template_name)

    class YTDLPLogger:
        def debug(self, message: str) -> None:  # noqa: D401 - yt-dlp logger interface
            _ = message

        def warning(self, message: str) -> None:  # noqa: D401 - yt-dlp logger interface
            _ = message

        def error(self, message: str) -> None:  # noqa: D401 - yt-dlp logger interface
            _ = message

    options = {
        "format": (
            f"bestvideo*[height<={MAX_VIDEO_HEIGHT}]"
            f"+bestaudio/best[height<={MAX_VIDEO_HEIGHT}]"
            f"/best[height<={MAX_VIDEO_HEIGHT}]/best"
        ),
        "merge_output_format": "mp4",
        "max_filesize": MAX_VIDEO_FILESIZE_BYTES,
        "noplaylist": not is_drive_folder_source,
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
        "logger": YTDLPLogger(),
        "ignoreerrors": False,
        "retries": 3,
        "extractor_retries": 3,
    }

    with YoutubeDL(options) as downloader:
        downloader.download([video_url])

    produced_files = [
        path
        for path in output_dir.glob(f"{prefix}*")
        if path.is_file() and path.suffix.lower() in VIDEO_FILE_EXTENSIONS
    ]

    if not produced_files:
        raise RuntimeError("yt-dlp completed without producing a local video file")

    for produced_file in produced_files:
        produced_size = produced_file.stat().st_size
        if produced_size > MAX_VIDEO_FILESIZE_BYTES:
            produced_file.unlink(missing_ok=True)
            raise DownloadError(
                f"video exceeded {MAX_VIDEO_FILESIZE_BYTES} byte limit: {produced_file.name}"
            )

    return produced_files


async def capture_video_fallback_screenshot(page: Page, output_dir: Path, ordinal: int) -> Path | None:
    screenshot_path = output_dir / f"raw_video_fallback_{ordinal:03d}.png"
    selectors = [
        'iframe[src*="youtube"]',
        'iframe[src*="youtu.be"]',
        'iframe[src*="vimeo"]',
        "video",
        "iframe",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            continue

        element = locator.first
        try:
            await element.scroll_into_view_if_needed(timeout=5_000)
            await element.screenshot(path=str(screenshot_path))
            log("video_fallback_screenshot", selector=selector, saved_as=screenshot_path.name)
            return screenshot_path
        except Exception:  # noqa: BLE001 - try next selector first
            continue

    try:
        await page.screenshot(path=str(screenshot_path), full_page=False)
        log("video_fallback_screenshot", selector="viewport", saved_as=screenshot_path.name)
        return screenshot_path
    except Exception as exc:  # noqa: BLE001 - capture failure should not crash flow
        log("video_fallback_screenshot_error", error=str(exc))
        return None


async def extract_videos(
    page: Page,
    video_urls: Iterable[str],
    output_dir: Path,
) -> tuple[list[Path], list[FallbackEntry], list[Path]]:
    video_files: list[Path] = []
    fallbacks: list[FallbackEntry] = []
    fallback_images: list[Path] = []

    def video_download_sort_key(candidate_url: str) -> tuple[int, int, str]:
        parsed_candidate = urlparse(candidate_url)
        host = parsed_candidate.netloc.lower()
        extension = Path(unquote(parsed_candidate.path)).suffix.lower()
        is_youtube = "youtube.com" in host or "youtu.be" in host
        is_drive_folder = is_drive_folder_url(candidate_url)
        is_direct_file = extension in VIDEO_FILE_EXTENSIONS

        # Prioritize direct hosted files first; process YouTube links and Drive folders last.
        return (1 if (is_youtube or is_drive_folder) else 0, 0 if is_direct_file else 1, candidate_url)

    ordered_video_urls = sorted(set(video_urls), key=video_download_sort_key)

    expanded_video_urls: list[str] = []
    seen_expanded_video_urls: set[str] = set()

    for candidate_url in ordered_video_urls:
        if not is_drive_folder_url(candidate_url):
            if candidate_url not in seen_expanded_video_urls:
                seen_expanded_video_urls.add(candidate_url)
                expanded_video_urls.append(candidate_url)
            continue

        folder_video_urls = await extract_drive_folder_video_urls(candidate_url)
        if not folder_video_urls:
            log("drive_folder_scan_empty", source=candidate_url)
            fallbacks.append(
                FallbackEntry(
                    source_url=candidate_url,
                    reason="drive_folder_scan_no_video_links",
                    screenshot_path=None,
                )
            )
            continue

        for folder_video_url in folder_video_urls:
            if folder_video_url in seen_expanded_video_urls:
                continue
            seen_expanded_video_urls.add(folder_video_url)
            expanded_video_urls.append(folder_video_url)
            log(
                "drive_video_queued",
                source=folder_video_url,
                drive_type="file",
                via="drive_folder_scan",
            )

    for index, video_url in enumerate(expanded_video_urls, start=1):
        parsed = urlparse(video_url)
        clean_name = unquote(parsed.path.split('/')[-1])
        clean_name = "".join(c for c in clean_name if c.isalnum() or c in "-_.")
        if not clean_name:
            clean_name = f"video_{index:03d}"
        
        info_txt_path = output_dir / f"{clean_name}.txt"
        try:
            info_txt_path.write_text(f"{video_url}\n", encoding="utf-8")
        except BaseException:
            pass

        is_drive_folder_candidate = is_drive_folder_url(video_url)

        if "youtube.com" in parsed.netloc.lower():
            path_lower = parsed.path.lower()
            if not path_lower or path_lower == "/" or any(path_lower.startswith(p) for p in ("/@", "/c/", "/channel/", "/user/")):
                log("video_ignored_channel", source=video_url, reason="soft check matched channel/user path")
                continue

        log("video_download_start", source=video_url)

        direct_extension = Path(unquote(parsed.path)).suffix.lower()
        if direct_extension in VIDEO_FILE_EXTENSIONS:
            direct_file = await download_direct_video_url(page, video_url, output_dir, index)
            if direct_file:
                video_files.append(direct_file)
                log("video_download_complete_direct", source=video_url, file=direct_file.name)
                continue

        try:
            downloaded = await asyncio.wait_for(
                asyncio.to_thread(_download_video_sync, video_url, output_dir, index),
                timeout=VIDEO_DOWNLOAD_TIMEOUT_SECONDS,
            )
            video_files.extend(downloaded)
            log("video_download_complete", source=video_url, files=[path.name for path in downloaded])
        except asyncio.TimeoutError:
            log("video_download_timeout", source=video_url, timeout_sec=VIDEO_DOWNLOAD_TIMEOUT_SECONDS)

            for partial in output_dir.glob(f"raw_video_{index:03d}*"):
                if partial.suffix.lower() in {".part", ".ytdl", ".temp"}:
                    partial.unlink(missing_ok=True)

            direct_file = await download_direct_video_url(page, video_url, output_dir, index)
            if direct_file:
                video_files.append(direct_file)
                log("video_download_complete_direct", source=video_url, file=direct_file.name)
                continue

            screenshot = await capture_video_fallback_screenshot(page, output_dir, index)
            if screenshot:
                fallback_images.append(screenshot)
            fallbacks.append(FallbackEntry(source_url=video_url, reason="video_download_timeout", screenshot_path=screenshot))
        except DownloadError as exc:
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("playlist", "channel", "user", "not a video")) and not is_drive_folder_candidate:
                log("video_ignored_not_a_video", source=video_url, reason=str(exc))
                continue

            if "unsupported url" in exc_str:
                direct_file = await download_direct_video_url(page, video_url, output_dir, index)
                if direct_file:
                    video_files.append(direct_file)
                    log("video_download_complete_direct", source=video_url, file=direct_file.name)
                    continue

            log("video_download_failed", source=video_url, error_type=type(exc).__name__, error=str(exc))
            screenshot = await capture_video_fallback_screenshot(page, output_dir, index)
            if screenshot:
                fallback_images.append(screenshot)
            fallbacks.append(FallbackEntry(source_url=video_url, reason=str(exc), screenshot_path=screenshot))
        except Exception as exc:  # noqa: BLE001 - must degrade gracefully
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("playlist", "channel", "user", "not a video")) and not is_drive_folder_candidate:
                log("video_ignored_not_a_video", source=video_url, reason=str(exc))
                continue

            if "unsupported url" in exc_str:
                direct_file = await download_direct_video_url(page, video_url, output_dir, index)
                if direct_file:
                    video_files.append(direct_file)
                    log("video_download_complete_direct", source=video_url, file=direct_file.name)
                    continue

            log("video_download_failed", source=video_url, error_type=type(exc).__name__, error=str(exc))
            screenshot = await capture_video_fallback_screenshot(page, output_dir, index)
            if screenshot:
                fallback_images.append(screenshot)
            fallbacks.append(FallbackEntry(source_url=video_url, reason=str(exc), screenshot_path=screenshot))

    log("video_download_summary", videos=len(video_files), fallbacks=len(fallbacks))
    return video_files, fallbacks, fallback_images


def sanitize_images(image_paths: Iterable[Path]) -> list[Path]:
    sanitized: list[Path] = []

    for image_path in image_paths:
        try:
            with Image.open(image_path) as image:
                image.load()
                image_format = image.format or "PNG"

                scrubbed = Image.new(image.mode, image.size)
                pixel_data = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
                scrubbed.putdata(pixel_data)
                if image_format.upper() in {"JPEG", "JPG"} and scrubbed.mode not in {"RGB", "L"}:
                    scrubbed = scrubbed.convert("RGB")

                temp_path = image_path.with_name(f"{image_path.stem}_clean{image_path.suffix}")
                scrubbed.save(temp_path, format=image_format)

            temp_path.replace(image_path)
            log("image_sanitized", file=image_path.name)
            sanitized.append(image_path)
        except Exception as exc:  # noqa: BLE001 - continue processing remaining files
            log("image_sanitization_error", file=image_path.name, error_type=type(exc).__name__, error=str(exc))
            sanitized.append(image_path)

    return sanitized


def rename_generic(
    files: Iterable[Path],
    prefix: str,
    forced_extension: str | None = None,
) -> tuple[list[Path], dict[Path, Path]]:
    files_list = sorted(set(files), key=lambda path: path.name)
    width = max(2, len(str(len(files_list))))

    renamed_files: list[Path] = []
    mapping: dict[Path, Path] = {}

    for index, original_path in enumerate(files_list, start=1):
        extension = forced_extension or original_path.suffix.lower() or ".bin"
        target_name = f"{prefix}_{index:0{width}d}{extension}"
        target_path = original_path.with_name(target_name)

        if target_path != original_path:
            original_path.rename(target_path)
            log("file_renamed", old=original_path.name, new=target_name)
        else:
            log("file_renamed", old=original_path.name, new=target_name)

        renamed_files.append(target_path)
        mapping[original_path] = target_path

    return renamed_files, mapping


def write_video_fallback_readme(output_dir: Path, fallback_entries: Iterable[FallbackEntry]) -> None:
    entries = list(fallback_entries)
    if not entries:
        return

    readme_path = output_dir / "video_fallback_readme.txt"
    lines = [
        "Video download fallback report",
        "",
        "yt-dlp could not download the following video URLs.",
        "",
    ]

    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. URL: {entry.source_url}")
        if entry.screenshot_path:
            lines.append(f"   Screenshot: {entry.screenshot_path.name}")
        lines.append(f"   Error: {entry.reason}")
        lines.append("")

    readme_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log("video_fallback_readme_created", file=readme_path.name, entries=len(entries))


def resolve_oauth_client_secret_path() -> Path:
    from_env = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()

    return (Path(__file__).resolve().parents[1] / "oauth-secret.json").resolve()


def resolve_oauth_token_path() -> Path:
    from_env = os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()

    return (Path(__file__).resolve().parents[1] / "token.json").resolve()


def get_drive_credentials() -> Credentials:
    token_path = resolve_oauth_token_path()
    client_secret_path = resolve_oauth_client_secret_path()

    credentials: Credentials | None = None
    login_reason = "token_missing"

    if token_path.is_file():
        log("drive_oauth_token_load_start", token_file=str(token_path))
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
            has_required_scopes = credentials.has_scopes(DRIVE_SCOPES)

            log(
                "drive_oauth_token_load_complete",
                token_file=str(token_path),
                valid=bool(credentials.valid),
                expired=bool(credentials.expired),
                has_refresh_token=bool(credentials.refresh_token),
                has_required_scopes=has_required_scopes,
            )

            if not has_required_scopes:
                credentials = None
                login_reason = "token_missing_required_scope"
            else:
                login_reason = "token_invalid"
        except Exception as exc:  # noqa: BLE001 - login fallback should still proceed
            log_exception("drive_oauth_token_load_failed", exc, token_file=str(token_path))
            credentials = None
            login_reason = "token_unreadable"

    if credentials and credentials.expired and credentials.refresh_token:
        log("drive_oauth_token_refresh_start", token_file=str(token_path))
        try:
            credentials.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            log("drive_oauth_token_refresh_complete", token_file=str(token_path))
        except Exception as exc:  # noqa: BLE001 - login fallback should still proceed
            log_exception("drive_oauth_token_refresh_failed", exc, token_file=str(token_path))
            credentials = None
            login_reason = "token_refresh_failed"

    if credentials and credentials.valid:
        log("drive_oauth_credentials_ready", source="token", token_file=str(token_path))
        return credentials

    if not client_secret_path.is_file():
        raise FileNotFoundError(f"OAuth client secret file not found: {client_secret_path}")

    log(
        "drive_oauth_browser_login_required",
        reason=login_reason,
        client_secret_file=str(client_secret_path),
        token_file=str(token_path),
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), DRIVE_SCOPES)

    # Explicitly log when the worker is waiting for user browser consent.
    log("drive_oauth_browser_login_waiting", local_server_port=0)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    log("drive_oauth_browser_login_complete", token_file=str(token_path))
    return credentials


def upload_to_drive(output_dir: Path, job_id: str) -> str:
    target_folder_id = os.getenv("DRIVE_TARGET_FOLDER_ID", "").strip()
    if not target_folder_id:
        raise RuntimeError("DRIVE_TARGET_FOLDER_ID is required for Drive upload")

    credentials = get_drive_credentials()
    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    target_folder = (
        drive_service.files()
        .get(
            fileId=target_folder_id,
            fields="id,name,mimeType,driveId,capabilities(canAddChildren)",
            supportsAllDrives=True,
        )
        .execute()
    )

    target_folder_name = str(target_folder.get("name", ""))
    target_drive_id = str(target_folder.get("driveId", "")).strip()
    target_capabilities = target_folder.get("capabilities") or {}
    can_add_children = bool(target_capabilities.get("canAddChildren", False))

    log(
        "drive_target_folder_resolved",
        folder_id=target_folder_id,
        folder_name=target_folder_name,
        drive_id=target_drive_id,
        can_add_children=can_add_children,
    )

    if not can_add_children:
        raise RuntimeError(
            f"OAuth user cannot add files to DRIVE_TARGET_FOLDER_ID={target_folder_id} "
            f"({target_folder_name or 'unknown_name'})"
        )

    folder_metadata = {
        "name": f"Extraction_{job_id}",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [target_folder_id],
    }

    folder_id: str | None = None
    try:
        created_folder = (
            drive_service.files()
            .create(body=folder_metadata, fields="id", supportsAllDrives=True)
            .execute()
        )
        folder_id = str(created_folder["id"])

        for candidate in sorted(output_dir.iterdir(), key=lambda path: path.name):
            if not candidate.is_file():
                continue
            if candidate.name == "drive_link.txt":
                continue

            media = MediaFileUpload(str(candidate), resumable=False)
            drive_service.files().create(
                body={"name": candidate.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()

        drive_service.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()

        folder_details = (
            drive_service.files()
            .get(fileId=folder_id, fields="webViewLink", supportsAllDrives=True)
            .execute()
        )
        web_view_link = str(folder_details.get("webViewLink", "")).strip()
        if not web_view_link:
            raise RuntimeError("Google Drive did not return webViewLink for created folder")

        drive_link_path = output_dir / "drive_link.txt"
        drive_link_path.write_text(web_view_link + "\n", encoding="utf-8")
        return web_view_link
    except Exception:
        if folder_id:
            try:
                drive_service.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
                log("drive_temp_folder_deleted", folder_id=folder_id)
            except Exception as cleanup_exc:  # noqa: BLE001 - cleanup should not hide original error
                log(
                    "drive_temp_folder_delete_failed",
                    folder_id=folder_id,
                    error_type=type(cleanup_exc).__name__,
                    error=str(cleanup_exc),
                )
        raise


async def run_harvest(url: str, output_dir: Path, job_id: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log("harvest_started", url=url, output=str(output_dir))

    image_files: list[Path] = []
    doc_files: list[Path] = []
    video_files: list[Path] = []
    fallback_entries: list[FallbackEntry] = []

    async with async_playwright() as playwright:
        log("browser_launch_start", browser="chromium", headless=True)
        browser = await playwright.chromium.launch(headless=True)
        log("browser_launch_complete", browser="chromium")
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1600, "height": 900})
        page = await context.new_page()

        intercepted_video_urls: set[str] = set()

        def track_intercepted_url(raw_url: str) -> None:
            normalized_urls = extract_video_urls_from_values(page.url or url, [raw_url])
            for candidate in normalized_urls:
                if is_unusable_segment_url(candidate):
                    continue

                is_new_candidate = candidate not in intercepted_video_urls
                intercepted_video_urls.add(candidate)

                if is_new_candidate and normalize_drive_video_url(candidate):
                    log(
                        "drive_video_detected",
                        source=candidate,
                        drive_type="folder" if is_drive_folder_url(candidate) else "file",
                        phase="network_media_probe",
                    )

        def handle_response(response) -> None:
            req_url = response.url
            req_url_lower = req_url.lower()
            if "canva" in req_url_lower or "video" in req_url_lower or "mp4" in req_url_lower:
                print(f"[X-RAY NETWORK] Caught potential media request: {req_url}")

            content_type = response.headers.get("content-type", "")
            resource_type = response.request.resource_type

            if is_probable_video_response(req_url, content_type, resource_type):
                track_intercepted_url(req_url)
            else:
                drive_url = normalize_drive_video_url(req_url)
                if drive_url:
                    intercepted_video_urls.add(drive_url)
                    log(
                        "drive_video_detected",
                        source=drive_url,
                        drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                        phase="network_response",
                    )

        def handle_request(request) -> None:
            req_url = request.url
            if is_video_candidate(req_url):
                track_intercepted_url(req_url)
                return

            drive_url = normalize_drive_video_url(req_url)
            if drive_url:
                intercepted_video_urls.add(drive_url)
                log(
                    "drive_video_detected",
                    source=drive_url,
                    drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                    phase="network_request",
                )

        page.on("request", handle_request)
        page.on("response", handle_response)

        try:
            await ensure_fully_rendered(page, url)

            discovered_videos = await extract_dynamic_video_urls(page)
            initial_drive_urls = sorted(url for url in discovered_videos if normalize_drive_video_url(url))
            for drive_url in initial_drive_urls:
                log(
                    "drive_video_detected",
                    source=drive_url,
                    drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                    phase="dynamic_dom_initial",
                )
            log("initial_video_discovery", count=len(discovered_videos))

            image_urls, pdf_urls, embedded_video_raw_urls, candidates = await extract_media_urls(page)

            embedded_video_urls = extract_video_urls_from_values(page.url, embedded_video_raw_urls)
            embedded_drive_urls = sorted(url for url in embedded_video_urls if normalize_drive_video_url(url))
            for drive_url in embedded_drive_urls:
                log(
                    "drive_video_detected",
                    source=drive_url,
                    drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                    phase="embedded_value_extraction",
                )
            discovered_videos.update(embedded_video_urls)

            discovered_videos = await collect_click_revealed_video_urls(
                page,
                candidates,
                baseline_urls=discovered_videos.union(intercepted_video_urls),
            )

            post_click_urls = await extract_dynamic_video_urls(page)
            post_click_drive_urls = sorted(url for url in post_click_urls if normalize_drive_video_url(url))
            for drive_url in post_click_drive_urls:
                log(
                    "drive_video_detected",
                    source=drive_url,
                    drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                    phase="dynamic_dom_post_click",
                )
            discovered_videos.update(post_click_urls)

            all_video_urls = discovered_videos.union(intercepted_video_urls)
            queued_drive_urls = sorted(url for url in all_video_urls if normalize_drive_video_url(url))
            for drive_url in queued_drive_urls:
                log(
                    "drive_video_queued",
                    source=drive_url,
                    drive_type="folder" if is_drive_folder_url(drive_url) else "file",
                )

            log(
                "network_interception_summary",
                intercepted=len(intercepted_video_urls),
                discovered=len(discovered_videos),
                total_videos=len(all_video_urls),
                queued_drive_videos=len(queued_drive_urls),
            )

            image_files, doc_files = await download_assets(image_urls, pdf_urls, output_dir)
            video_files, fallback_entries, fallback_images = await extract_videos(page, all_video_urls, output_dir)
            image_files.extend(fallback_images)
        except PlaywrightTimeoutError as exc:
            log_exception("navigation_timeout", exc, current_url=page.url)
            raise
        finally:
            await context.close()
            await browser.close()
            log("browser_shutdown_complete")

    sanitized_images = sanitize_images(image_files)
    renamed_images, image_map = rename_generic(sanitized_images, "image")
    renamed_docs, _ = rename_generic(doc_files, "doc", forced_extension=".pdf")
    renamed_videos, _ = rename_generic(video_files, "video")

    for entry in fallback_entries:
        if entry.screenshot_path and entry.screenshot_path in image_map:
            entry.screenshot_path = image_map[entry.screenshot_path]

    write_video_fallback_readme(output_dir, fallback_entries)

    log(
        "harvest_completed",
        images=len(renamed_images),
        docs=len(renamed_docs),
        videos=len(renamed_videos),
        fallbacks=len(fallback_entries),
    )

    drive_link = await asyncio.to_thread(upload_to_drive, output_dir, job_id)
    log("drive_upload_completed", job_id=job_id, drive_link=drive_link)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output).expanduser().resolve()
    job_id = args.job_id.strip() or output_dir.name

    try:
        asyncio.run(run_harvest(args.url, output_dir, job_id))
    except KeyboardInterrupt:
        log("harvest_cancelled")
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - top-level worker safety
        log_exception("harvest_failed", exc, url=args.url, output=str(output_dir))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
