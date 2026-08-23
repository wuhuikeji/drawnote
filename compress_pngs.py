#!/usr/bin/env python3
"""Compress PNG and JPEG files in a directory tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_KIND = "png"
JPEG_KIND = "jpeg"
IMAGE_KINDS = (PNG_KIND, JPEG_KIND)
MANIFEST_VERSION = 1
DEFAULT_MANIFEST_NAME = ".image-compress-manifest.json"


@dataclass
class Stats:
    total: int = 0
    compressed: int = 0
    skipped: int = 0
    failed: int = 0
    original_bytes: int = 0
    final_bytes: int = 0


@dataclass(frozen=True)
class ImageFile:
    path: Path
    kind: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively compress PNG and JPEG files in a directory.",
    )
    parser.add_argument("directory", help="Directory that contains PNG/JPEG files.")
    parser.add_argument(
        "--png-quality",
        default="65-80",
        help="pngquant quality range, for example 65-80. Default: 65-80.",
    )
    parser.add_argument(
        "--png-speed",
        type=int,
        default=1,
        choices=range(1, 12),
        metavar="1-11",
        help="pngquant speed, 1 is slowest/best compression and 11 is fastest. Default: 1.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=82,
        choices=range(1, 101),
        metavar="1-100",
        help="cjpeg quality for JPG/JPEG files. Default: 82.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List PNG/JPEG files without changing them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each file result.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=10,
        help="Print a progress line every N files when not using --verbose. Use 0 to disable. Default: 10.",
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_NAME,
        help=f"Manifest file path used to skip already processed files. Default: {DEFAULT_MANIFEST_NAME}.",
    )
    parser.add_argument(
        "--ignore-manifest",
        action="store_true",
        help="Ignore manifest records and try to compress every detected image.",
    )
    return parser.parse_args()


def detect_image_kind(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            header = file.read(8)
    except OSError:
        return None

    if header.startswith(PNG_SIGNATURE):
        return PNG_KIND
    if header.startswith(JPEG_SIGNATURE):
        return JPEG_KIND
    return None


def iter_image_files(directory: Path) -> list[ImageFile]:
    images: list[ImageFile] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue

        kind = detect_image_kind(path)
        if kind is not None:
            images.append(ImageFile(path=path, kind=kind))

    return sorted(images, key=lambda image: str(image.path))


def format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_manifest_path(directory: Path, manifest: str) -> Path:
    path = Path(manifest).expanduser()
    if not path.is_absolute():
        path = directory / path
    return path.resolve()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "files": {}}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": MANIFEST_VERSION, "files": {}}

    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": MANIFEST_VERSION, "files": {}}

    return data


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compression_params(kind: str, args: argparse.Namespace) -> dict:
    if kind == PNG_KIND:
        return {
            "tool": "pngquant",
            "quality": args.png_quality,
            "speed": args.png_speed,
        }

    return {
        "tool": "djpeg+cjpeg",
        "quality": args.jpeg_quality,
    }


def manifest_key(directory: Path, path: Path) -> str:
    try:
        return path.relative_to(directory).as_posix()
    except ValueError:
        return path.as_posix()


def manifest_matches(record: object, kind: str, digest: str, params: dict) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        record.get("kind") == kind
        and record.get("sha256") == digest
        and record.get("params") == params
    )


def update_manifest_record(manifest: dict, key: str, kind: str, digest: str, size: int, params: dict, status: str) -> None:
    manifest.setdefault("files", {})[key] = {
        "kind": kind,
        "sha256": digest,
        "size": size,
        "params": params,
        "status": status,
    }


def add_stats(stats: Stats, before: int, after: int, ok: bool, message: str) -> None:
    stats.original_bytes += before
    stats.final_bytes += after

    if ok:
        stats.compressed += 1
    elif message.startswith("skipped:"):
        stats.skipped += 1
    else:
        stats.failed += 1


def print_stats(label: str, stats: Stats) -> None:
    saved_bytes = stats.original_bytes - stats.final_bytes
    print(f"{label}:")
    print(f"  Scanned: {stats.total}")
    print(f"  Compressed: {stats.compressed}")
    print(f"  Skipped: {stats.skipped}")
    print(f"  Failed: {stats.failed}")
    print(f"  Saved: {format_bytes(saved_bytes)}")


def print_progress(done: int, total: int, stats: Stats) -> None:
    print(
        f"Progress: {done}/{total} "
        f"(compressed {stats.compressed}, skipped {stats.skipped}, failed {stats.failed}, "
        f"saved {format_bytes(stats.original_bytes - stats.final_bytes)})",
        flush=True,
    )


def preserve_if_smaller(path: Path, temp_path: Path, original_size: int, original_mode: int) -> tuple[bool, str, int, int]:
    compressed_size = temp_path.stat().st_size
    if compressed_size == 0:
        return False, "compressor produced an empty file", original_size, original_size

    if compressed_size >= original_size:
        return False, "skipped: compressed file is not smaller", original_size, original_size

    temp_path.chmod(original_mode)
    shutil.move(str(temp_path), str(path))
    return True, "compressed", original_size, compressed_size


def compress_png(path: Path, quality: str, speed: int) -> tuple[bool, str, int, int]:
    original_stat = path.stat()
    original_size = original_stat.st_size
    original_mode = stat.S_IMODE(original_stat.st_mode)

    with tempfile.NamedTemporaryFile(
        prefix=f"{path.stem}.",
        suffix=".png",
        dir=path.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    command = [
        "pngquant",
        "--force",
        "--skip-if-larger",
        "--quality",
        quality,
        "--speed",
        str(speed),
        "--output",
        str(temp_path),
        "--",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            if result.returncode == 98:
                return False, "skipped: pngquant output is not smaller or not worth saving", original_size, original_size
            if result.returncode == 99:
                return False, "skipped: pngquant could not meet the minimum quality", original_size, original_size

            message = (result.stderr or result.stdout).strip()
            return False, message or f"pngquant exited with {result.returncode}", original_size, original_size

        return preserve_if_smaller(path, temp_path, original_size, original_mode)
    finally:
        temp_path.unlink(missing_ok=True)


def compress_jpeg(path: Path, quality: int) -> tuple[bool, str, int, int]:
    original_stat = path.stat()
    original_size = original_stat.st_size
    original_mode = stat.S_IMODE(original_stat.st_mode)

    with tempfile.NamedTemporaryFile(
        prefix=f"{path.stem}.",
        suffix=".ppm",
        dir=path.parent,
        delete=False,
    ) as ppm_file:
        ppm_path = Path(ppm_file.name)

    with tempfile.NamedTemporaryFile(
        prefix=f"{path.stem}.",
        suffix=path.suffix,
        dir=path.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    decode_command = [
        "djpeg",
        "-outfile",
        str(ppm_path),
        str(path),
    ]
    compress_command = [
        "cjpeg",
        "-quality",
        str(quality),
        "-outfile",
        str(temp_path),
        str(ppm_path),
    ]

    try:
        decode_result = subprocess.run(
            decode_command,
            check=False,
            capture_output=True,
            text=True,
        )

        if decode_result.returncode != 0:
            message = (decode_result.stderr or decode_result.stdout).strip()
            return False, message or f"djpeg exited with {decode_result.returncode}", original_size, original_size

        compress_result = subprocess.run(
            compress_command,
            check=False,
            capture_output=True,
            text=True,
        )

        if compress_result.returncode != 0:
            message = (compress_result.stderr or compress_result.stdout).strip()
            return False, message or f"cjpeg exited with {compress_result.returncode}", original_size, original_size

        return preserve_if_smaller(path, temp_path, original_size, original_mode)
    finally:
        ppm_path.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)


def check_tools(files: list[ImageFile]) -> bool:
    needs_pngquant = any(image.kind == PNG_KIND for image in files)
    needs_cjpeg = any(image.kind == JPEG_KIND for image in files)
    ok = True

    if needs_pngquant and shutil.which("pngquant") is None:
        print(
            "Error: pngquant is not installed or not found in PATH.\n"
            "Install it first, for example: brew install pngquant",
            file=sys.stderr,
        )
        ok = False

    if needs_cjpeg and shutil.which("cjpeg") is None:
        print(
            "Error: cjpeg is not installed or not found in PATH.\n"
            "Install it first, for example: brew install jpeg-turbo",
            file=sys.stderr,
        )
        ok = False
    if needs_cjpeg and shutil.which("djpeg") is None:
        print(
            "Error: djpeg is not installed or not found in PATH.\n"
            "Install it first, for example: brew install jpeg-turbo",
            file=sys.stderr,
        )
        ok = False

    return ok


def main() -> int:
    args = parse_args()
    directory = Path(args.directory).expanduser().resolve()

    if not directory.is_dir():
        print(f"Error: directory does not exist: {directory}", file=sys.stderr)
        return 2

    files = iter_image_files(directory)
    stats = Stats(total=len(files))
    stats_by_kind = {kind: Stats() for kind in IMAGE_KINDS}
    for image in files:
        stats_by_kind[image.kind].total += 1

    if args.dry_run:
        print(f"Found {stats.total} PNG/JPEG file(s) under {directory}")
        for image in files:
            print(f"{image.path} [{image.kind}]")
        return 0

    if not check_tools(files):
        return 2

    manifest_path = resolve_manifest_path(directory, args.manifest)
    manifest = {"version": MANIFEST_VERSION, "files": {}} if args.ignore_manifest else load_manifest(manifest_path)
    manifest_changed = False

    print(
        f"Processing {stats.total} image file(s): "
        f"{stats_by_kind[PNG_KIND].total} PNG, {stats_by_kind[JPEG_KIND].total} JPEG",
        flush=True,
    )

    for index, image in enumerate(files, start=1):
        path = image.path
        params = compression_params(image.kind, args)
        key = manifest_key(directory, path)
        before = path.stat().st_size
        before_digest = file_sha256(path)

        if not args.ignore_manifest:
            record = manifest.get("files", {}).get(key)
            if manifest_matches(record, image.kind, before_digest, params):
                message = "skipped: already processed with the same parameters"
                add_stats(stats, before, before, False, message)
                add_stats(stats_by_kind[image.kind], before, before, False, message)

                if args.verbose:
                    print(f"{message} [{image.kind}]: {path}")
                elif args.progress_interval > 0 and (index == stats.total or index % args.progress_interval == 0):
                    print_progress(index, stats.total, stats)
                continue

        if image.kind == PNG_KIND:
            ok, message, before, after = compress_png(path, args.png_quality, args.png_speed)
        else:
            ok, message, before, after = compress_jpeg(path, args.jpeg_quality)

        add_stats(stats, before, after, ok, message)
        add_stats(stats_by_kind[image.kind], before, after, ok, message)

        if ok:
            after_digest = file_sha256(path)
            update_manifest_record(manifest, key, image.kind, after_digest, after, params, "compressed")
            manifest_changed = True

            if args.verbose:
                saved = before - after
                print(f"compressed [{image.kind}]: {path} ({format_bytes(saved)} saved)")
        else:
            if message.startswith("skipped:"):
                update_manifest_record(manifest, key, image.kind, before_digest, before, params, "skipped")
                manifest_changed = True

                if args.verbose:
                    print(f"{message} [{image.kind}]: {path}")
            else:
                print(f"failed [{image.kind}]: {path}: {message}", file=sys.stderr)

        if not args.verbose and args.progress_interval > 0:
            if index == stats.total or index % args.progress_interval == 0:
                print_progress(index, stats.total, stats)

    print_stats("Overall", stats)
    print_stats("PNG", stats_by_kind[PNG_KIND])
    print_stats("JPEG", stats_by_kind[JPEG_KIND])

    if manifest_changed:
        save_manifest(manifest_path, manifest)
        print(f"Manifest updated: {manifest_path}")

    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
