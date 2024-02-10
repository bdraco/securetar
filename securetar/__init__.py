"""Tarfile fileobject handler for encrypted files."""
import hashlib
import logging
import os
from pathlib import Path, PurePath
import tarfile
from typing import IO, Generator, Optional, Any
from contextlib import contextmanager
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import (
    Cipher,
    CipherContext,
    algorithms,
    modes,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

BLOCK_SIZE = 16
BLOCK_SIZE_BITS = 128
DEFAULT_BUFSIZE = 10240

MOD_READ = "r"
MOD_WRITE = "w"


class SecureTarFile:
    """Handle encrypted files for tarfile library."""

    def __init__(
        self,
        name: Path,
        mode: str,
        key: Optional[bytes] = None,
        gzip: bool = True,
        bufsize: int = DEFAULT_BUFSIZE,
        fileobj: Optional[IO[bytes]] = None,
    ) -> None:
        """Initialize encryption handler."""
        self._file: Optional[IO[bytes]] = None
        self._mode: str = mode
        self._name: Path = name
        self._bufsize: int = bufsize
        self._extra_args = {}

        # Tarfile options
        self._tar: Optional[tarfile.TarFile] = None
        if key:
            self._tar_mode = f"{mode}|"
        else:
            self._tar_mode = f"{mode}:"
            if gzip:
                self._extra_args["compresslevel"] = 6

        if fileobj:
            self._extra_args["fileobj"] = fileobj

        if gzip:
            self._tar_mode = self._tar_mode + "gz"

        # Encryption/Description
        self._aes: Optional[Cipher] = None
        self._key: Optional[bytes] = key

        # Function helper
        self._decrypt: Optional[CipherContext] = None
        self._encrypt: Optional[CipherContext] = None

    @contextmanager
    def create_inner_tar(
        self, name: str, gzip: bool = True
    ) -> Generator[Any, Any, tarfile.TarFile]:
        """Create inner tar file."""
        outer_tar = self._tar
        assert outer_tar
        tar_info = tarfile.TarInfo(name=name)
        offset_before_inner = outer_tar.offset
        outer_tar.addfile(tar_info)
        # Remove the member from the tarfile, so we can add it later with the correct size
        outer_tar.members.pop()
        with SecureTarFile(
            name=Path(name),
            mode=self._mode,
            key=self._key,
            gzip=gzip,
            bufsize=self._bufsize,
            fileobj=outer_tar.fileobj,
        ) as inner_tar:
            yield inner_tar
            inner_tar_offset = inner_tar.offset
        outer_tar.offset += inner_tar_offset
        tar_info.size = inner_tar_offset
        # Now that we know the size of the inner tar, we seek back
        # to where we started and re-add the member with the correct size
        outer_tar.fileobj.seek(offset_before_inner)
        outer_tar.addfile(tar_info)
        outer_tar.fileobj.seek(self._tar.offset)

    def __enter__(self) -> tarfile.TarFile:
        """Start context manager tarfile."""
        if not self._key:
            self._tar = tarfile.open(
                name=str(self._name),
                mode=self._tar_mode,
                dereference=False,
                bufsize=self._bufsize,
                **self._extra_args,
            )
            return self._tar

        # Encrypted/Decrypted Tarfile
        read_mode = self._mode.startswith("r")
        if read_mode:
            file_mode: int = os.O_RDONLY
        else:
            file_mode: int = os.O_WRONLY | os.O_CREAT

        fd = os.open(self._name, file_mode, 0o666)
        self._file = os.fdopen(fd, "rb" if read_mode else "wb")

        # Extract IV for CBC
        if self._mode == MOD_READ:
            cbc_rand = self._file.read(16)
        else:
            cbc_rand = os.urandom(16)
            self._file.write(cbc_rand)

        # Create Cipher
        self._aes = Cipher(
            algorithms.AES(self._key),
            modes.CBC(_generate_iv(self._key, cbc_rand)),
            backend=default_backend(),
        )

        self._decrypt = self._aes.decryptor()
        self._encrypt = self._aes.encryptor()

        self._tar = tarfile.open(
            fileobj=self,
            mode=self._tar_mode,
            dereference=False,
            bufsize=self._bufsize,
            **self._extra_args,
        )
        return self._tar

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Close file."""
        if self._tar:
            self._tar.close()
            self._tar = None
        if self._file:
            self._file.close()
            self._file = None

    def write(self, data: bytes) -> None:
        """Write data."""
        if len(data) % BLOCK_SIZE != 0:
            padder = padding.PKCS7(BLOCK_SIZE_BITS).padder()
            data = padder.update(data) + padder.finalize()

        self._file.write(self._encrypt.update(data))

    def read(self, size: int = 0) -> bytes:
        """Read data."""
        return self._decrypt.update(self._file.read(size))

    @property
    def path(self) -> Path:
        """Return path object of tarfile."""
        return self._name

    @property
    def size(self) -> float:
        """Return backup size."""
        if not self._name.is_file():
            return 0
        return round(self._name.stat().st_size / 1_048_576, 2)  # calc mbyte


def _generate_iv(key: bytes, salt: bytes) -> bytes:
    """Generate an iv from data."""
    temp_iv = key + salt
    for _ in range(100):
        temp_iv = hashlib.sha256(temp_iv).digest()
    return temp_iv[:16]


def secure_path(tar: tarfile.TarFile) -> Generator[tarfile.TarInfo, None, None]:
    """Security safe check of path.
    Prevent ../ or absolut paths
    """
    for member in tar:
        file_path = Path(member.name)
        try:
            if file_path.is_absolute():
                raise ValueError()
            Path("/fake", file_path).resolve().relative_to("/fake")
        except (ValueError, RuntimeError):
            _LOGGER.warning("Found issue with file %s", file_path)
            continue
        else:
            yield member


def _is_excluded_by_filter(path: PurePath, exclude_list: list[str]) -> bool:
    """Filter to filter excludes."""

    for exclude in exclude_list:
        if not path.match(exclude):
            continue
        _LOGGER.debug("Ignoring %s because of %s", path, exclude)
        return True

    return False


def atomic_contents_add(
    tar_file: tarfile.TarFile,
    origin_path: Path,
    excludes: list[str],
    arcname: str = ".",
) -> None:
    """Append directories and/or files to the TarFile if excludes wont filter."""

    if _is_excluded_by_filter(origin_path, excludes):
        return None

    # Add directory only (recursive=False) to ensure we also archive empty directories
    tar_file.add(origin_path.as_posix(), arcname=arcname, recursive=False)

    for directory_item in origin_path.iterdir():
        if _is_excluded_by_filter(directory_item, excludes):
            continue

        arcpath = PurePath(arcname, directory_item.name).as_posix()
        if directory_item.is_dir() and not directory_item.is_symlink():
            atomic_contents_add(tar_file, directory_item, excludes, arcpath)
            continue

        tar_file.add(directory_item.as_posix(), arcname=arcpath, recursive=False)

    return None
