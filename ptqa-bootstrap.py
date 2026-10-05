"""Hash-pinned runtime extraction only. Does not install packages or run trading code."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import urllib.parse
import urllib.request
import zipfile

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_TOTAL = 24 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    parts = name.split("/")
    if (len(name) > 240 or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", p)
                               or p.endswith(".") for p in parts)
            or any(p.split(".")[0].upper() in
                   {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)],
                    *[f"LPT{i}" for i in range(10)]} for p in parts)):
        raise ValueError("Unsafe archive path")
    return name


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate(data, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(data) != expected:
        raise ValueError("Bundle SHA-256 mismatch")
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Bundle too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not 2 <= len(infos) <= 256 or sum(i.file_size for i in infos) > MAX_TOTAL:
            raise ValueError("Archive size/count limit exceeded")
        names = set()
        for item in infos:
            safe_name(item.filename)
            mode = item.external_attr >> 16
            if (item.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or item.flag_bits & 1 or item.file_size > MAX_ARCHIVE
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError("Unsupported archive entry")
            if item.filename.casefold() in names:
                raise ValueError("Duplicate or case-colliding archive path")
            names.add(item.filename.casefold())
        contents = {i.filename: archive.read(i) for i in infos}
        for name in contents:
            parts = name.split("/")
            if any("/".join(parts[:i]).casefold() in names for i in range(1, len(parts))):
                raise ValueError("Archive file/directory collision")
    manifest = json.loads(contents.pop("manifest.json"), object_pairs_hook=unique_object)
    if manifest.get("format") != "ptqa-runtime-v1" or set(manifest) != {"format", "files"}:
        raise ValueError("Unsupported manifest")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != set(contents):
        raise ValueError("Manifest file set mismatch")
    for name, value in contents.items():
        if not name.startswith("runtime/") or digest(value) != files[name]:
            raise ValueError("Runtime manifest hash mismatch")
    for day in range(1, 8):
        if not any(n.startswith(f"runtime/day-{day}/") for n in contents):
            raise ValueError("Missing daily runtime")
    contents["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    return contents


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Bundle URL must use HTTPS without embedded credentials")


def download(url):
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "PTQA-Bootstrap/1.0"})
    with urllib.request.build_opener(HTTPSOnly()).open(request, timeout=45) as response:
        check_url(response.url)
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Bundle download exceeds limit")
    return data


def no_links(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Refusing a symlink or junction in the destination path")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Refusing a Windows reparse point")


def verify_existing(folder, contents):
    actual = {}
    for path in folder.rglob("*"):
        no_links(path)
        if path.is_file():
            actual[path.relative_to(folder).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("Unexpected existing runtime entry")
    if actual != contents:
        raise ValueError("Existing runtime differs; preserve it and review before continuing")


def install(data, expected, project, dry_run=False):
    contents = validate(data, expected)
    project = Path(os.path.abspath(Path(project).expanduser()))
    no_links(project)
    if not project.is_dir():
        raise ValueError("Select an existing local project folder")
    target = project / ("ptqa-runtime-" + expected[:16])
    workspace = project / "day-1-recording-work"
    state_path = project / "ptqa-setup.json"
    academy_home = project / "ptqa-local-environment"
    for path in (target, workspace, state_path, academy_home):
        no_links(path)
    state = {"format": "ptqa-setup-v1", "bundle_sha256": expected,
             "runtime": str(target / "runtime"), "workspace": str(workspace),
             "academy_home": str(academy_home)}
    if state_path.exists() and json.loads(state_path.read_text()) != state:
        raise ValueError("Existing setup points elsewhere; preserve it and resume that project")
    if target.exists():
        verify_existing(target, contents)
    if workspace.exists() and not workspace.is_dir():
        raise ValueError("Workspace path is not a folder")
    if dry_run:
        return {"status": "validated-no-writes", "files": len(contents), **state}
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix="ptqa-staging-", dir=project))
        try:
            for name, value in contents.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    output.write(value)
            if target.exists():
                raise ValueError("Runtime appeared during install; retry after reviewing it")
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    workspace.mkdir(exist_ok=True)
    if not state_path.exists():
        with state_path.open("x", encoding="utf-8") as output:
            json.dump(state, output, indent=2)
            output.write("\n")
    return {"status": "runtime-installed-no-checks-run", **state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--url")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.archive:
            with args.archive.open("rb") as source_file:
                data = source_file.read(MAX_ARCHIVE + 1)
        else:
            data = download(args.url)
        print(json.dumps(install(data, args.sha256, args.project, args.dry_run), indent=2))
    except Exception as exc:
        # URL errors can include signed URLs; never echo their contents.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        parser.exit(1, f"Setup stopped: {message}. Existing member work was not replaced.\n")


if __name__ == "__main__":
    main()
