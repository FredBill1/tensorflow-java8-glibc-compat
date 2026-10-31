"""Validate and append immutable-by-content assets to an existing GitHub Release.

Requires GitHub CLI authentication through GH_TOKEN. This never creates a
Release, replaces an asset, or deletes an asset left by an unsuccessful upload.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote
import xml.etree.ElementTree as ET

import build
import release
from verify_jar import ARTIFACT, JAR_NAME, VERSION, sha256


ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def validate_version(tag, root=ROOT):
    pom = ET.parse(str(Path(root) / "pom.xml")).getroot()
    version = pom.findtext("m:version", namespaces=NS)
    artifact = pom.findtext("m:artifactId", namespaces=NS)
    normalized = tag[1:] if tag.startswith("v") else tag
    consumer = ET.fromstring(release.release_pom()).findtext("m:version", namespaces=NS)
    expected_jar = "{}-{}.jar".format(artifact, version)
    if (not version or artifact != ARTIFACT or normalized != version or version != VERSION
            or consumer != version or JAR_NAME != expected_jar or build.JAR != expected_jar
            or build.SOURCES_JAR != release.SOURCES_NAME
            or release.POM_NAME != expected_jar[:-4] + ".pom"
            or release.SOURCE_ARCHIVE != expected_jar[:-4] + "-source.zip"):
        raise ValueError("Release tag, pom.xml, and build/release script versions must match: "
                         "tag={!r}, POM={!r}, scripts={!r}".format(tag, version, VERSION))
    return version


def gh(*arguments):
    result = subprocess.run(["gh", *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("GitHub CLI failed: " + (result.stderr or result.stdout).strip())
    return result.stdout


def get_release(repo, tag):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo must be an owner/repository name")
    model = json.loads(gh("api", "repos/{}/releases/tags/{}".format(repo, quote(tag, safe=""))))
    if model.get("tag_name") != tag or model.get("draft") is not False:
        raise ValueError("The target must be the published Release for tag " + tag)
    if model.get("immutable") is True:
        raise ValueError("Immutable Releases cannot accept assets after publication. "
                         "This workflow requires a mutable Release; use draft upload before publication for immutable releases.")
    if model.get("immutable") is not False:
        raise ValueError("GitHub did not report whether the Release is immutable")
    if type(model.get("id")) is not int or model["id"] <= 0:
        raise ValueError("GitHub returned an invalid Release ID")
    return model


def preflight(repo, tag, root=ROOT):
    validate_version(tag, root)
    model = get_release(repo, tag)
    print("Release preflight passed: " + tag)
    return model


def release_files(dist):
    """Only this release's checksummed allowlist is eligible for upload."""
    dist = Path(dist)
    expected = {JAR_NAME, release.SOURCES_NAME, release.SOURCE_ARCHIVE, release.POM_NAME,
                "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "third-party-licenses.zip", "verification.json"}
    checksum = dist / "SHA256SUMS"
    if checksum.is_symlink() or not checksum.is_file():
        raise ValueError("Missing regular SHA256SUMS file")
    records = {}
    for line in checksum.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\\r\n]+)", line)
        if not match or match[2] in records:
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        records[match[2]] = match[1]
    if set(records) != expected:
        raise ValueError("SHA256SUMS must list exactly the nine current release payloads")
    for name, digest in records.items():
        path = dist / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError("Release checksum mismatch or missing regular file: " + name)
    records["SHA256SUMS"] = sha256(checksum)
    return records


def list_assets(repo, release_id):
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          "repos/{}/releases/{}/assets?per_page=100".format(repo, release_id)))
    assets = {}
    for page in pages:
        for asset in page:
            name = asset["name"]
            if name in assets:
                raise ValueError("GitHub returned duplicate Release asset names: " + name)
            assets[name] = asset
    return assets


def downloaded_digest(repo, asset_id):
    """Older assets may lack an API digest. Hash their download without buffering it."""
    if type(asset_id) is not int or asset_id <= 0:
        raise ValueError("GitHub returned an invalid asset ID")
    command = ["gh", "api", "-H", "Accept: application/octet-stream",
               "repos/{}/releases/assets/{}".format(repo, asset_id)]
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as process:
            digest = hashlib.sha256()
            for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                digest.update(block)
            if process.wait():
                errors.seek(0)
                raise RuntimeError("Cannot verify existing asset: " + errors.read().decode("utf-8", "replace"))
    return digest.hexdigest()


def check_existing(repo, dist, records, assets):
    missing = []
    for name, expected in records.items():
        asset = assets.get(name)
        if asset is None:
            missing.append(name)
            continue
        if asset.get("state") != "uploaded":
            raise ValueError("Incomplete existing asset: {}. Remove that incomplete asset and rerun.".format(name))
        if asset.get("size") != (Path(dist) / name).stat().st_size:
            raise ValueError("Existing asset differs; refusing to replace: " + name)
        remote = asset.get("digest") or ""
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", remote):
            actual = remote[7:].lower()
        else:
            actual = downloaded_digest(repo, asset.get("id"))
        if actual != expected:
            raise ValueError("Existing asset differs; refusing to replace: " + name)
    return missing


def upload(repo, tag, dist, root=ROOT):
    model = preflight(repo, tag, root)
    records = release_files(dist)
    assets = list_assets(repo, model["id"])
    # Check every existing file before writing any missing file, including the
    # checksum manifest. A rebuild can differ even when its version is unchanged.
    missing = check_existing(repo, dist, records, assets)
    if missing:
        payloads = sorted(name for name in missing if name != "SHA256SUMS")
        if payloads:
            gh("release", "upload", tag, *[str(Path(dist) / name) for name in payloads], "--repo", repo)
            remaining = check_existing(repo, dist, records, list_assets(repo, model["id"]))
            if set(remaining) - {"SHA256SUMS"}:
                raise RuntimeError("Upload finished without all expected assets: " + ", ".join(remaining))
        # gh uploads files concurrently, so ordering its arguments is insufficient
        # to keep the manifest from appearing before the large binary is complete.
        if "SHA256SUMS" in missing:
            gh("release", "upload", tag, str(Path(dist) / "SHA256SUMS"), "--repo", repo)
        remaining = check_existing(repo, dist, records, list_assets(repo, model["id"]))
        if remaining:
            raise RuntimeError("Upload finished without all expected assets: " + ", ".join(remaining))
    print("Release assets verified: {} ({} uploaded, {} unchanged)".format(tag, len(missing), len(records) - len(missing)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "upload"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        if args.operation == "preflight":
            preflight(args.repo, args.tag)
        else:
            upload(args.repo, args.tag, args.dist)
    except (OSError, ValueError, RuntimeError, KeyError, ET.ParseError) as error:
        print("Release publishing failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
