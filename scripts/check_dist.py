"""Fail if the built sdist or wheel holds anything it shouldn't, or metadata is off.

Run after `uv build`: python scripts/check_dist.py
"""

import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
FORBIDDEN = ("harness", "findings", ".private", "results", ".env", "advisory")
SDIST_FILES = re.compile(
    r"^(src/litellm_doctor/[a-z_]+\.py|tests/[a-z_]+\.py|README\.md|LICENSE|CHANGELOG\.md|pyproject\.toml|"
    r"PKG-INFO|\.gitignore)$")
WHEEL_FILES = re.compile(r"^(litellm_doctor/[a-z_]+\.py|litellm_doctor-[^/]+\.dist-info/.+)$")


def version() -> str:
    text = (ROOT / "src" / "litellm_doctor" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def main() -> int:
    errors = []
    v = version()
    sdists, wheels = sorted(DIST.glob("*.tar.gz")), sorted(DIST.glob("*.whl"))
    if [p.name for p in sdists] != [f"litellm_doctor-{v}.tar.gz"]:
        errors.append(f"expected exactly dist/litellm_doctor-{v}.tar.gz, found {[p.name for p in sdists]}")
    if [p.name for p in wheels] != [f"litellm_doctor-{v}-py3-none-any.whl"]:
        errors.append(f"expected exactly dist/litellm_doctor-{v}-py3-none-any.whl, found {[p.name for p in wheels]}")
    if errors:
        print("\n".join(errors))
        return 1

    with tarfile.open(sdists[0]) as tar:
        prefix = f"litellm_doctor-{v}/"
        names = [m.name[len(prefix):] for m in tar.getmembers() if m.isfile()]
        for name in names:
            if not SDIST_FILES.match(name):
                errors.append(f"sdist has an unexpected file: {name}")
        gitignore = tar.extractfile(prefix + ".gitignore").read().decode() if ".gitignore" in names else ""
        pkg_info = tar.extractfile(prefix + "PKG-INFO").read().decode()
    with zipfile.ZipFile(wheels[0]) as whl:
        wheel_names = whl.namelist()
        metadata = whl.read(f"litellm_doctor-{v}.dist-info/METADATA").decode()
    for name in wheel_names:
        if not WHEEL_FILES.match(name):
            errors.append(f"wheel has an unexpected file: {name}")

    for name in names + wheel_names:
        if any(word in name.lower() for word in FORBIDDEN):
            errors.append(f"private-looking file in an artifact: {name}")
    if any(word in gitignore.lower() for word in ("private", "advisory", "security")):
        errors.append("the .gitignore that ships in the sdist mentions private material")

    for label, text in (("wheel METADATA", metadata), ("sdist PKG-INFO", pkg_info)):
        if "Metadata-Version: 2.4\n" not in text:
            errors.append(f"{label} isn't Metadata-Version 2.4")
        if f"Version: {v}\n" not in text:
            errors.append(f"{label} doesn't say Version: {v}")
        if "License-Expression: MIT\n" not in text:
            errors.append(f"{label} has no License-Expression: MIT")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"dist OK: {len(names)} sdist files, {len(wheel_names)} wheel files, version {v}, metadata 2.4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
