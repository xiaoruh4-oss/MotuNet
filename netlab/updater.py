"""Download verified public GitHub releases without UI or installer execution.

The caller runs these blocking operations on a worker thread.  Only the fixed
repository's manifest and versioned Windows installer are accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MANIFEST_LIMIT = 128 * 1024
DOWNLOAD_LIMIT = 512 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
NETWORK_TIMEOUT = 15
_ASSET_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "github-releases.githubusercontent.com",
})
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")


class UpdateError(Exception):
    """An update could not be checked or verified."""


class UpdateCancelled(UpdateError):
    """The user cancelled a download."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: int
    notes: str


def _version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise UpdateError("版本号格式无效，必须为稳定版本 x.y.z")
    return tuple(int(part) for part in value.split("."))


def _validate_network_url(url: str) -> None:
    try:
        if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _ASSET_HOSTS
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError
    except (ValueError, TypeError):
        raise UpdateError("更新地址不安全：仅允许 GitHub 的 HTTPS 发布资源") from None


class _GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject an unsafe Location before urllib can connect to its host.
        _validate_network_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UpdateClient:
    def __init__(self, repository: str, current_version: str):
        if (not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository)
                or repository.split("/")[-1] in {".", ".."}):
            raise UpdateError("更新仓库格式无效，必须为 GitHub 用户名/仓库名")
        self.repository = repository
        self.current_version = current_version
        self._current = _version_tuple(current_version)
        self._repo_path = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        self.manifest_url = f"https://github.com/{self._repo_path}/releases/latest/download/latest.json"
        self._opener = urllib.request.build_opener(_GitHubRedirectHandler())

    def _installer_url(self, version: str) -> str:
        return (f"https://github.com/{self._repo_path}/releases/download/v{version}/"
                f"MotuNet-{version}-Setup-x64.exe")

    def _open(self, url: str):
        _validate_network_url(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": f"MotuNet/{self.current_version}",
            "Accept": "application/octet-stream", "Accept-Encoding": "identity",
        })
        response = self._opener.open(request, timeout=NETWORK_TIMEOUT)
        try:
            _validate_network_url(response.geturl())
            if response.status != 200:
                raise UpdateError(f"更新服务返回异常状态：HTTP {response.status}")
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise UpdateError("更新服务返回了不支持的压缩内容")
        except Exception:
            response.close()
            raise
        return response

    @staticmethod
    def _content_length(response) -> int | None:
        value = response.headers.get("Content-Length")
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{1,12}", str(value)):
            raise UpdateError("更新服务返回的文件长度无效")
        return int(value)

    def _parse_manifest(self, manifest: object) -> UpdateInfo:
        if not isinstance(manifest, dict):
            raise UpdateError("更新清单格式无效")
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise UpdateError("更新清单版本不受支持")
        if manifest.get("platform") != "windows-x64":
            raise UpdateError("更新清单不适用于 Windows x64")
        version = manifest.get("version")
        _version_tuple(version)
        url = manifest.get("url")
        if url != self._installer_url(version):
            raise UpdateError("安装包地址与发布仓库、版本或文件名不一致")
        sha256 = manifest.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise UpdateError("更新清单中的 SHA256 校验值无效")
        size = manifest.get("size")
        if type(size) is not int or not 0 < size <= DOWNLOAD_LIMIT:
            raise UpdateError("更新安装包大小无效或超过 512 MiB")
        notes = manifest.get("notes", "")
        if not isinstance(notes, str):
            raise UpdateError("更新说明格式无效或过长")
        try:
            if len(notes.encode("utf-8")) > MANIFEST_LIMIT:
                raise UpdateError("更新说明格式无效或过长")
        except UnicodeError:
            raise UpdateError("更新说明包含无效字符") from None
        return UpdateInfo(version, url, sha256.lower(), size, notes)

    def check(self) -> UpdateInfo | None:
        try:
            with self._open(self.manifest_url) as response:
                length = self._content_length(response)
                if length is not None and length > MANIFEST_LIMIT:
                    raise UpdateError("更新清单超过 128 KiB")
                content = bytearray()
                while True:
                    chunk = response.read(min(CHUNK_SIZE, MANIFEST_LIMIT + 1 - len(content)))
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > MANIFEST_LIMIT:
                        raise UpdateError("更新清单超过 128 KiB")
                if length is not None and len(content) != length:
                    raise UpdateError("更新清单下载不完整")
            manifest = json.loads(content.decode("utf-8"))
            info = self._parse_manifest(manifest)
            return info if _version_tuple(info.version) > self._current else None
        except UpdateError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise UpdateError("更新清单不是有效的 UTF-8 JSON") from None
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"无法获取更新清单：HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise UpdateError(f"无法连接更新服务：{exc}") from None

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise UpdateCancelled("已取消下载安装包")

    def download(self, info: UpdateInfo, directory: Path,
                 progress: Callable[[int, int], None], cancel: threading.Event) -> Path:
        """Return a verified installer; remove this attempt's files on failure."""
        self._check_cancel(cancel)
        if not isinstance(info, UpdateInfo):
            raise UpdateError("待下载的更新信息无效")
        info = self._parse_manifest({
            "schema_version": 1, "platform": "windows-x64", "version": info.version,
            "url": info.url, "sha256": info.sha256, "size": info.size, "notes": info.notes,
        })
        attempt = None
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            attempt = Path(tempfile.mkdtemp(prefix="motunet-update-", dir=directory))
            destination = attempt / f"MotuNet-{info.version}-Setup-x64.exe"
            partial = destination.with_suffix(".exe.part")
            digest = hashlib.sha256()
            received = 0
            self._check_cancel(cancel)
            progress(0, info.size)
            self._check_cancel(cancel)
            with self._open(info.url) as response, partial.open("xb") as output:
                length = self._content_length(response)
                if length is not None and length != info.size:
                    raise UpdateError("安装包长度与更新清单不一致")
                while True:
                    self._check_cancel(cancel)
                    chunk = response.read(min(CHUNK_SIZE, info.size + 1 - received))
                    self._check_cancel(cancel)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > info.size:
                        raise UpdateError("安装包超过更新清单声明的大小")
                    output.write(chunk)
                    digest.update(chunk)
                    progress(received, info.size)
                self._check_cancel(cancel)
                if received != info.size:
                    raise UpdateError("安装包下载不完整，请重试")
                if not hmac.compare_digest(digest.hexdigest(), info.sha256):
                    raise UpdateError("安装包 SHA256 校验失败，请重新下载")
                output.flush()
                os.fsync(output.fileno())
            self._check_cancel(cancel)
            os.replace(partial, destination)
            self._check_cancel(cancel)
            return destination
        except UpdateError:
            if attempt is not None:
                shutil.rmtree(attempt, ignore_errors=True)
            raise
        except Exception as exc:
            if attempt is not None:
                shutil.rmtree(attempt, ignore_errors=True)
            if cancel.is_set():
                raise UpdateCancelled("已取消下载安装包") from None
            if isinstance(exc, urllib.error.HTTPError):
                raise UpdateError(f"无法下载安装包：HTTP {exc.code}") from None
            raise UpdateError(f"下载安装包失败：{exc}") from None
