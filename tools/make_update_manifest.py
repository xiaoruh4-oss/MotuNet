"""Build the public update manifest from the app version and release artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlab import __version__
from netlab.update_source import UPDATE_REPOSITORY
from netlab.updater import CHUNK_SIZE, DOWNLOAD_LIMIT, MANIFEST_LIMIT, UpdateClient, UpdateError


def release_notes(changelog: str, version: str) -> str:
    """Return exactly one version's Markdown body, preserving its line breaks."""
    heading = re.compile(r"^##\s+" + re.escape(version) + r"(?=$|[\s（(\[])")
    lines = changelog.splitlines()
    start = next((index + 1 for index, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md 缺少 {version} 的更新说明")
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ValueError(f"{version} 的更新说明不能为空")
    return notes


def validate_release(repository: str, version: str, changelog: str, tag: str | None = None) -> str:
    UpdateClient(repository, version)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"发布标签 {tag!r} 与应用版本 v{version} 不一致")
    return release_notes(changelog, version)


def make_manifest(installer: Path, repository: str, version: str, notes: str) -> dict:
    client = UpdateClient(repository, version)
    expected_name = f"MotuNet-{version}-Setup-x64.exe"
    if installer.name != expected_name:
        raise ValueError(f"安装包文件名必须为 {expected_name}")
    size = installer.stat().st_size
    if not 0 < size <= DOWNLOAD_LIMIT:
        raise ValueError("安装包大小无效或超过 512 MiB")
    digest = hashlib.sha256()
    measured = 0
    with installer.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            measured += len(chunk)
            if measured > DOWNLOAD_LIMIT:
                raise ValueError("安装包超过 512 MiB")
            digest.update(chunk)
    if measured != size or installer.stat().st_size != size:
        raise ValueError("生成清单期间安装包大小发生变化")
    manifest = {
        "schema_version": 1, "platform": "windows-x64", "version": version,
        "url": client._installer_url(version), "sha256": digest.hexdigest(),
        "size": size, "notes": notes,
    }
    # Share the shipped consumer's validation so incompatible assets fail
    # locally, before the release is visible to installed clients.
    client._parse_manifest(manifest)
    if len((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > MANIFEST_LIMIT:
        raise ValueError("更新清单超过 128 KiB")
    return manifest


def write_manifest(manifest: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    filename = f"MotuNet-{manifest['version']}-Setup-x64.exe"
    (output / f"{filename}.sha256").write_text(f"{manifest['sha256']}  {filename}\n", encoding="ascii", newline="\n")
    (output / "release-notes.md").write_text(manifest["notes"] + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=UPDATE_REPOSITORY)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "deliverables")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    parser.add_argument("--check-tag")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.repository:
            raise ValueError("尚未配置 UPDATE_REPOSITORY，不能构建带有空更新来源的发布包")
        if args.repository != UPDATE_REPOSITORY:
            raise ValueError("发布仓库与 UPDATE_REPOSITORY 不一致，请先配置应用的固定更新来源")
        notes = validate_release(args.repository, __version__, args.changelog.read_text(encoding="utf-8"), args.check_tag)
        if args.validate_only:
            print(f"发布配置有效：v{__version__} → {args.repository}")
            return 0
        installer = args.installer or args.output_dir / f"MotuNet-{__version__}-Setup-x64.exe"
        manifest = make_manifest(installer, args.repository, __version__, notes)
        write_manifest(manifest, args.output_dir)
        print(f"更新清单已生成：{args.output_dir / 'latest.json'}")
        return 0
    except (OSError, ValueError, UpdateError) as exc:
        parser.exit(1, f"发布清单生成失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
