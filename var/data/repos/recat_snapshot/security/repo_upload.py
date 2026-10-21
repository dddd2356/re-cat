"""저장소 zip 업로드 — 관리자가 화면에서 코드를 올리면 data/repos/<이름>/ 에 안전하게 풀어 둔다.

막는 것
- 경로 이탈(zip slip): 절대 경로, '..', 드라이브 문자가 든 항목은 업로드 전체를 거부한다
- 심볼릭 링크·장치 파일: 거부한다 (압축 안의 링크로 서버의 다른 파일을 가리키지 못하게)
- 압축 폭탄: 압축 크기, 풀린 전체 크기, 파일 하나 크기, 파일 수, 압축률 상한
- 덮어쓰기: 새 이름만 받는다. 교체는 '업로드로 등록한' 저장소만 허용하고 policy.yaml 저장소는 거부한다
- 부분 실패: 임시 폴더에 다 푼 뒤 마지막에 한 번에 옮긴다 (실패하면 아무것도 남기지 않는다)
코드는 실행하지 않는다 — 정적 분석으로 읽기만 한다. 실행 권한 비트는 버린다.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
SKIP_PARTS = {"__MACOSX", ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
              ".idea", ".vscode", "build", "dist", "target", ".gradle"}


@dataclass
class Limits:
    max_zip_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 300 * 1024 * 1024
    max_file_bytes: int = 20 * 1024 * 1024
    max_files: int = 20_000
    max_ratio: int = 200                # 파일 하나의 압축률 상한 (풀린 크기 / 압축 크기)


class UploadRejected(ValueError):
    pass


def _members(zf: zipfile.ZipFile, lim: Limits) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    out, total = [], 0
    for info in zf.infolist():
        raw = info.filename.replace("\\", "/")
        p = PurePosixPath(raw)
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or ".." in p.parts:
            raise UploadRejected(f"허용되지 않는 경로: {info.filename}")
        ftype = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)   # 0이면 형식 정보 없음(윈도우 zip 등) → 일반 파일
        if ftype and ftype not in (stat.S_IFREG, stat.S_IFDIR):
            raise UploadRejected(f"링크·특수 파일은 올릴 수 없습니다: {info.filename}")
        if info.is_dir() or any(part in SKIP_PARTS for part in p.parts):
            continue
        if info.file_size > lim.max_file_bytes:
            raise UploadRejected(f"파일이 너무 큽니다: {info.filename}")
        if info.compress_size and info.file_size / info.compress_size > lim.max_ratio and info.file_size > 1024 * 1024:
            raise UploadRejected(f"압축률이 비정상적으로 높습니다(압축 폭탄 의심): {info.filename}")
        total += info.file_size
        out.append((info, p))
    if len(out) > lim.max_files:
        raise UploadRejected(f"파일이 너무 많습니다 ({len(out)}개, 상한 {lim.max_files})")
    if total > lim.max_total_bytes:
        raise UploadRejected(f"압축을 푼 크기가 상한을 넘습니다 ({total // (1024 * 1024)}MB)")
    if not out:
        raise UploadRejected("압축 안에 올릴 파일이 없습니다")
    # 'my-service/...'처럼 최상위 폴더 하나로 묶여 있으면 그 폴더를 벗긴다
    tops = {p.parts[0] for _, p in out}
    if len(tops) == 1 and all(len(p.parts) > 1 for _, p in out):
        out = [(i, PurePosixPath(*p.parts[1:])) for i, p in out]
    return out


def extract_repo(zip_path: Path, name: str, repos_root: Path, *, replace: bool = False,
                 lim: Limits | None = None) -> dict:
    """zip을 repos_root/name 에 푼다. 반환: {"files": n, "bytes": total}."""
    lim = lim or Limits()
    if not _NAME.match(name):
        raise UploadRejected("저장소 이름은 영문·숫자·_ . - 로 64자 이하")
    if zip_path.stat().st_size > lim.max_zip_bytes:
        raise UploadRejected(f"zip이 너무 큽니다 (상한 {lim.max_zip_bytes // (1024 * 1024)}MB)")
    root = repos_root.resolve()
    dest = root / name
    if dest.exists() and not replace:
        raise UploadRejected("같은 이름의 저장소가 이미 있습니다")
    if dest.is_symlink():
        raise UploadRejected("저장소 경로가 링크입니다")
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UploadRejected("zip 파일이 아닙니다")
    with zf:
        members = _members(zf, lim)
        tmp = Path(tempfile.mkdtemp(prefix=".upload-", dir=root))
        try:
            written = 0
            for info, rel in members:
                target = (tmp / rel).resolve()
                if tmp.resolve() not in target.parents:
                    raise UploadRejected(f"허용되지 않는 경로: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        written += len(chunk)
                        if written > lim.max_total_bytes:      # 헤더를 속인 경우 대비, 실제로 쓴 양으로 다시 확인
                            raise UploadRejected("압축을 푼 크기가 상한을 넘습니다")
                        dst.write(chunk)
                os.chmod(target, 0o644)
            if dest.exists() and not replace:
                # 트러블슈팅 ⑦: 처음 확인 뒤 다른 요청이 같은 이름을 먼저 설치했다 — 덮어쓰지 않고 거절
                raise UploadRejected("같은 이름의 저장소가 이미 있습니다")
            if dest.exists():
                old = root / f".replaced-{name}"
                shutil.rmtree(old, ignore_errors=True)
                dest.rename(old)
                try:
                    tmp.rename(dest)
                except BaseException:
                    # 트러블슈팅 ⑧: 새 폴더를 못 옮기면 치워 둔 기존 폴더를 제자리로 돌린다(저장소가 사라지지 않게)
                    old.rename(dest)
                    raise
                shutil.rmtree(old, ignore_errors=True)
            else:
                tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return {"files": len(members), "bytes": written}
