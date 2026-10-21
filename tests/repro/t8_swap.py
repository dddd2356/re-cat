"""⑧ 교체 업로드 도중(기존 폴더를 치운 직후) 새 폴더 이동이 실패하면?"""
import sys, tempfile, shutil
from pathlib import Path
R = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(R))
from backend.app.security.repo_upload import extract_repo
S = Path(__file__).resolve().parent
root = Path(tempfile.mkdtemp())
extract_repo(S / "kit/1_shop_test.zip", "shop", root)
print("  교체 전:", sorted(p.name for p in root.iterdir()), "/ v1 파일 있음:", (root / "shop/billing/invoice.py").exists())
orig = Path.rename
def flaky(self, target):
    if self.name.startswith(".upload-"):           # 새 폴더를 제자리로 옮기는 순간 디스크 오류
        raise OSError(28, "No space left on device")
    return orig(self, target)
Path.rename = flaky
try:
    extract_repo(S / "kit/2_shop_test_v2.zip", "shop", root, replace=True)
except OSError as e:
    print("  교체 중 오류:", e)
Path.rename = orig
print("  교체 후:", sorted(p.name for p in root.iterdir()), "/ shop 폴더 있음:", (root / "shop").exists())
shutil.rmtree(root)
