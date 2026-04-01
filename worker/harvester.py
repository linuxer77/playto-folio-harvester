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
    parser.add_argument(
        "--output",
        default="./temp_harvest",
        help="Directory where harvested files are saved (default: ./temp_harvest)",
    )
    args = parser.parse_args()
    log(
        "args_parsed",
        url=args.url,
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

    options = {
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
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
                scrubbed.putdata(image.getdata())
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


async def run_harvest(url: str, output_dir: Path) -> None:
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


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output).expanduser().resolve()

    try:
        asyncio.run(run_harvest(args.url, output_dir))
    except KeyboardInterrupt:
        log("harvest_cancelled")
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - top-level worker safety
        log_exception("harvest_failed", exc, url=args.url, output=str(output_dir))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
