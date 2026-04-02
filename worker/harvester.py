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
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

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
VIDEO_HOST_PATTERN = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com)", re.IGNORECASE)
DIRECT_VIDEO_PATTERN = re.compile(r"\.(mp4|m4v|mov|webm)(?:$|\?|#)", re.IGNORECASE)
DRIVE_FILE_PATTERN = re.compile(r"drive\.google\.com/file/d/", re.IGNORECASE)
VIDEO_FILE_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
MIN_IMAGE_DIMENSION = 250
MAX_VIDEO_HEIGHT = 1080
MAX_VIDEO_FILESIZE_BYTES = 200 * 1024 * 1024
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
    return bool(VIDEO_HOST_PATTERN.search(lowered) or DIRECT_VIDEO_PATTERN.search(lowered))


def normalize_drive_file_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None

    if "drive.google.com" not in parsed.netloc.lower() or not DRIVE_FILE_PATTERN.search(url):
        return None

    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if not match:
        return None

    file_id = match.group(1)
    canonical_path = f"/file/d/{file_id}/view"
    return parsed._replace(path=canonical_path, fragment="").geturl()


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
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)

    try:
        await page.wait_for_load_state("networkidle", timeout=45_000)
    except PlaywrightTimeoutError:
        # Some sites keep long-lived background requests open indefinitely.
        log(
            "navigation_networkidle_timeout",
            timeout_ms=45_000,
            current_url=page.url,
        )
        await page.wait_for_selector("body", state="attached", timeout=10_000)
        await page.wait_for_timeout(1_500)

    # Scroll to trigger lazy-loaded images/videos on modern portfolio pages.
    for _ in range(8):
        current_height = await page.evaluate("() => document.body.scrollHeight")
        await page.evaluate("height => window.scrollTo(0, height)", current_height)
        await page.wait_for_timeout(650)
        next_height = await page.evaluate("() => document.body.scrollHeight")
        if next_height <= current_height:
            break

    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)
    log("navigation_complete", final_url=page.url)


async def extract_media_urls(page: Page) -> tuple[set[str], set[str], set[str]]:
    dom_snapshot = await page.evaluate(
        r"""
        () => {
            const values = {
                images: new Set(),
                links: new Set(),
                iframes: new Set(),
                videos: new Set(),
                driveLinks: new Set(),
                driveIframes: new Set(),
                skippedLogoImages: [],
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
            const isDriveFileLink = (raw) => typeof raw === "string" && /drive\.google\.com\/file\/d\//i.test(raw);

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
                if (isDriveFileLink(href)) {
                    push(values.driveLinks, href);
                }
            });

            document.querySelectorAll("iframe[src]").forEach((frame) => {
                const src = frame.getAttribute("src");
                push(values.iframes, src);
                if (isDriveFileLink(src)) {
                    push(values.driveIframes, src);
                }
            });

            document.querySelectorAll("video").forEach((video) => {
                push(values.videos, video.getAttribute("src"));
                video.querySelectorAll("source[src]").forEach((source) => {
                    push(values.videos, source.getAttribute("src"));
                });
            });

            return {
                images: [...values.images],
                links: [...values.links],
                iframes: [...values.iframes],
                videos: [...values.videos],
                drive_links: [...values.driveLinks],
                drive_iframes: [...values.driveIframes],
                skipped_logo_images: values.skippedLogoImages,
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

    for raw in dom_snapshot.get("iframes", []):
        normalized = normalize_url(base_url, raw)
        if normalized and is_video_candidate(normalized):
            video_urls.add(normalized)

    for raw in dom_snapshot.get("videos", []):
        normalized = normalize_url(base_url, raw)
        if normalized and is_video_candidate(normalized):
            video_urls.add(normalized)

    for raw in dom_snapshot.get("drive_links", []):
        normalized = normalize_url(base_url, raw)
        if not normalized:
            continue

        drive_url = normalize_drive_file_url(normalized)
        if not drive_url:
            continue

        video_urls.add(drive_url)
        log("drive_link_detected", source=drive_url)

    for raw in dom_snapshot.get("drive_iframes", []):
        normalized = normalize_url(base_url, raw)
        if not normalized:
            continue

        drive_url = normalize_drive_file_url(normalized)
        if not drive_url:
            continue

        video_urls.add(drive_url)
        log("drive_iframe_detected", source=drive_url)

    log(
        "media_discovered",
        image_candidates=len(image_urls),
        pdf_candidates=len(pdf_urls),
        video_candidates=len(video_urls),
    )
    return image_urls, pdf_urls, video_urls


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


def _download_video_sync(video_url: str, output_dir: Path, ordinal: int) -> list[Path]:
    prefix = f"raw_video_{ordinal:03d}"
    template = str(output_dir / f"{prefix}.%(ext)s")

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
            f"/best[height<={MAX_VIDEO_HEIGHT}]"
        ),
        "merge_output_format": "mp4",
        "max_filesize": MAX_VIDEO_FILESIZE_BYTES,
        "noplaylist": True,
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
        "logger": YTDLPLogger(),
        "ignoreerrors": False,
        "retries": 1,
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

    for index, video_url in enumerate(sorted(set(video_urls)), start=1):
        log("video_download_start", source=video_url)
        try:
            downloaded = await asyncio.to_thread(_download_video_sync, video_url, output_dir, index)
            video_files.extend(downloaded)
            log("video_download_complete", source=video_url, files=[path.name for path in downloaded])
        except DownloadError as exc:
            log("video_download_failed", source=video_url, error_type=type(exc).__name__, error=str(exc))
            screenshot = await capture_video_fallback_screenshot(page, output_dir, index)
            if screenshot:
                fallback_images.append(screenshot)
            fallbacks.append(FallbackEntry(source_url=video_url, reason=str(exc), screenshot_path=screenshot))
        except Exception as exc:  # noqa: BLE001 - must degrade gracefully
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

        try:
            await ensure_fully_rendered(page, url)
            image_urls, pdf_urls, video_urls = await extract_media_urls(page)
            image_files, doc_files = await download_assets(image_urls, pdf_urls, output_dir)
            video_files, fallback_entries, fallback_images = await extract_videos(page, video_urls, output_dir)
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
