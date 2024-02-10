"""Test Tarfile functions."""
import os
from pathlib import Path, PurePath
from dataclasses import dataclass
import shutil

import pytest

from securetar import (
    SecureTarFile,
    _is_excluded_by_filter,
    atomic_contents_add,
    secure_path,
)


@dataclass
class TarInfo:
    """Fake TarInfo."""

    name: str


def test_secure_path() -> None:
    """Test Secure Path."""
    test_list = [
        TarInfo("test.txt"),
        TarInfo("data/xy.blob"),
        TarInfo("bla/blu/ble"),
        TarInfo("data/../xy.blob"),
    ]
    assert test_list == list(secure_path(test_list))


def test_not_secure_path() -> None:
    """Test Not secure path."""
    test_list = [
        TarInfo("/test.txt"),
        TarInfo("data/../../xy.blob"),
        TarInfo("/bla/blu/ble"),
    ]
    assert [] == list(secure_path(test_list))


def test_is_excluded_by_filter_good() -> None:
    """Test exclude filter."""
    filter_list = ["not/match", "/dev/xy"]
    test_list = [
        PurePath("test.txt"),
        PurePath("data/xy.blob"),
        PurePath("bla/blu/ble"),
        PurePath("data/../xy.blob"),
    ]

    for path_object in test_list:
        assert _is_excluded_by_filter(path_object, filter_list) is False


def test_is_exclude_by_filter_bad() -> None:
    """Test exclude filter."""
    filter_list = ["*.txt", "data/*", "bla/blu/ble"]
    test_list = [
        PurePath("test.txt"),
        PurePath("data/xy.blob"),
        PurePath("bla/blu/ble"),
        PurePath("data/test_files/kk.txt"),
    ]

    for path_object in test_list:
        assert _is_excluded_by_filter(path_object, filter_list) is True


@pytest.mark.parametrize("bufsize", [10240, 4 * 2**20])
def test_create_pure_tar(tmp_path: Path, bufsize: int) -> None:
    """Test to create a tar file without encryption."""
    # Prepare test folder
    temp_orig = tmp_path.joinpath("orig")
    fixture_data = Path(__file__).parent.joinpath("fixtures/tar_data")
    shutil.copytree(fixture_data, temp_orig, symlinks=True)

    # Create Tarfile
    temp_tar = tmp_path.joinpath("backup.tar")
    with SecureTarFile(temp_tar, "w", bufsize=bufsize) as tar_file:
        atomic_contents_add(
            tar_file,
            temp_orig,
            excludes=[],
            arcname=".",
        )

    assert temp_tar.exists()

    # Restore
    temp_new = tmp_path.joinpath("new")
    with SecureTarFile(temp_tar, "r", bufsize=bufsize) as tar_file:
        tar_file.extractall(path=temp_new, members=tar_file)

    assert temp_new.is_dir()
    assert temp_new.joinpath("test_symlink").is_symlink()
    assert temp_new.joinpath("test1").is_dir()
    assert temp_new.joinpath("test1/script.sh").is_file()

    # 775 is correct for local, but in GitHub action it's 755, both is fine
    assert oct(temp_new.joinpath("test1/script.sh").stat().st_mode)[-3:] in [
        "755",
        "775",
    ]
    assert temp_new.joinpath("README.md").is_file()


@pytest.mark.parametrize("bufsize", [10240, 4 * 2**20])
def test_create_encrypted_tar(tmp_path: Path, bufsize: int) -> None:
    """Test to create a tar file with encryption."""
    key = os.urandom(16)

    # Prepare test folder
    temp_orig = tmp_path.joinpath("orig")
    fixture_data = Path(__file__).parent.joinpath("fixtures/tar_data")
    shutil.copytree(fixture_data, temp_orig, symlinks=True)

    # Create Tarfile
    temp_tar = tmp_path.joinpath("backup.tar")
    with SecureTarFile(temp_tar, "w", key=key, bufsize=bufsize) as tar_file:
        atomic_contents_add(
            tar_file,
            temp_orig,
            excludes=[],
            arcname=".",
        )

    assert temp_tar.exists()

    # Restore
    temp_new = tmp_path.joinpath("new")
    with SecureTarFile(temp_tar, "r", key=key, bufsize=bufsize) as tar_file:
        tar_file.extractall(path=temp_new, members=tar_file)

    assert temp_new.is_dir()
    assert temp_new.joinpath("test_symlink").is_symlink()
    assert temp_new.joinpath("test1").is_dir()
    assert temp_new.joinpath("test1/script.sh").is_file()

    # 775 is correct for local, but in GitHub action it's 755, both is fine
    assert oct(temp_new.joinpath("test1/script.sh").stat().st_mode)[-3:] in [
        "755",
        "775",
    ]
    assert temp_new.joinpath("README.md").is_file()


def test_gzipped_tar_inside_tar(tmp_path: Path) -> None:
    # Prepare test folder
    temp_orig = tmp_path.joinpath("orig")
    fixture_data = Path(__file__).parent.joinpath("fixtures/tar_data")
    shutil.copytree(fixture_data, temp_orig, symlinks=True)

    # Create Tarfile
    main_tar = tmp_path.joinpath("backup.tar")
    inner_tgz_files =  ("core.tar.gz", "core2.tar.gz", "core3.tar.gz")
    outer_secure_tar_file = SecureTarFile(main_tar, "w", gzip=False)
    with outer_secure_tar_file as outer_tar_file:
        for inner_tgz_file in inner_tgz_files:
            with outer_secure_tar_file.create_inner_tar(inner_tgz_file, gzip=True) as inner_tar_file:
                atomic_contents_add(
                    inner_tar_file,
                    temp_orig,
                    excludes=[],
                    arcname=".",
                )


    assert main_tar.exists()

    # Restore
    temp_new = tmp_path.joinpath("new")
    with SecureTarFile(main_tar, "r", gzip=False) as tar_file:
        tar_file.extractall(path=temp_new, members=tar_file)

    assert temp_new.is_dir()
    assert temp_new.joinpath("core.tar.gz").is_file()
    assert temp_new.joinpath("core2.tar.gz").is_file()
    assert temp_new.joinpath("core3.tar.gz").is_file()

    # Extract inner tars
    for inner_tgz in inner_tgz_files:
        temp_inner_new = tmp_path.joinpath("{inner_tgz}_inner_new")

        with SecureTarFile(temp_new.joinpath(inner_tgz), "r", gzip=True) as tar_file:
            tar_file.extractall(path=temp_inner_new, members=tar_file)


        assert temp_inner_new.is_dir()
        assert temp_inner_new.joinpath("test_symlink").is_symlink()
        assert temp_inner_new.joinpath("test1").is_dir()
        assert temp_inner_new.joinpath("test1/script.sh").is_file()

        # 775 is correct for local, but in GitHub action it's 755, both is fine
        assert oct(temp_inner_new.joinpath("test1/script.sh").stat().st_mode)[-3:] in [
            "755",
            "775",
        ]
        assert temp_inner_new.joinpath("README.md").is_file()