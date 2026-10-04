#!/usr/bin/env python3
"""Publish only the pinned, prebuilt catalog artifact; never build a database.

All writes are confined to a new draft release and its new assets. Existing
releases/tags/assets are never edited, replaced or deleted. Network errors fail
closed; a partially uploaded draft is preserved for inspection, not resumed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = "autisticryptic/carrier_Bundles"
COMMIT = "814b057ea9c0982bd636f3e4908117d72bd509be"
RUN = 37201477372
ARTIFACT = 11303097227
DIGEST = "sha256:4471b902945a6f17f7a928d1c108d74d673f9c5f62c2387c1656a8c3429aa090"
TAG = "v0.3.1-catalog-v7"
WORKFLOW = ".github/workflows/publish-catalog-set.yml"
PLAN_PATH = "release-staging/catalog-set/publication.json"
BASES = (
    "carrier-bundles-ios-ipcc",
    "carrier-bundles-iphone16promax-27.0.1",
    "carrier-bundles-pixel-mustang",
    "carrier-bundles-xiaomi15ultra-xuanyuan-baseband",
)
DB_NAMES = {base + suffix + ".sqlite3" for base in BASES
            for suffix in ("", "-no-icons", "-minimal-no-icons")}
ASSET_NAMES = DB_NAMES | {base + "-minimal-no-icons.pruning.json" for base in BASES} | {
    "SHA256SUMS", "catalog-variants.json", "simulation-evidence.json", "actions-verification.json"}
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pins_check(pins):
    require(pins["commit"] == COMMIT and pins["run_id"] == RUN and
            pins["artifact_id"] == ARTIFACT and pins["artifact_digest"] == DIGEST,
            "Unexpected pinned provenance")
    require(set(pins["assets"]) == ASSET_NAMES and len(ASSET_NAMES) == 20,
            "Expected exactly 20 pinned release assets")
    for name, item in pins["assets"].items():
        require(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) and item["bytes"] > 0,
                "Invalid pin: " + name)


def parse_sums(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None, "Malformed checksum line")
        sha, name = match.groups()
        require(name not in result, "Duplicate checksum filename")
        result[name] = sha
    return result


def validate_set(directory, pins, require_readonly=True):
    from tools.verify_catalog import verify_catalog
    pins_check(pins)
    require({p.name for p in directory.iterdir()} == ASSET_NAMES, "Unexpected asset set")
    for name, expected in pins["assets"].items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Unsafe asset: " + name)
        require(path.stat().st_size == expected["bytes"] and digest(path) == expected["sha256"],
                "Asset mismatch: " + name)
    sums = parse_sums((directory / "SHA256SUMS").read_text(encoding="ascii"))
    require(set(sums) == ASSET_NAMES - {"SHA256SUMS"}, "Incomplete checksum coverage")
    require(all(sha == pins["assets"][name]["sha256"] for name, sha in sums.items()),
            "Checksum list differs from pins")
    manifest = read_json(directory / "catalog-variants.json")
    require(len(manifest["catalogs"]) == 4, "Expected four independent sources")
    require(manifest["policy"]["runtime_minimal"]["enabled"] is True,
            "Missing runtime-minimal policy")
    seen = set()
    totals = {"no-icons": 0, "minimal-no-icons": 0}
    for entry in manifest["catalogs"]:
        require(set(entry["variants"]) == {"full", "no-icons", "minimal-no-icons"},
                "Expected three variants per source")
        require(entry["source"] == entry["variants"]["full"], "Full source metadata changed")
        for variant, value in entry["variants"].items():
            name = value["database"]
            require(name in DB_NAMES and name not in seen, "Duplicate/unexpected database")
            seen.add(name)
            require(value["sha256"] == pins["assets"][name]["sha256"] and
                    value["bytes"] == pins["assets"][name]["bytes"], "Manifest pin mismatch")
            checked = verify_catalog(directory / name, require_readonly=require_readonly)
            require(all(checked[key] == val for key, val in value.items()
                        if key not in {"sha256", "bytes"}), "Database summary mismatch: " + name)
            require(checked["counts"] == pins["databases"][name]["counts"], "Pinned row-count mismatch")
            if variant != "full":
                totals[variant] += value["bytes"]
                require(checked["counts"]["visual_assets"] == 0, "Icons remain")
            if variant == "minimal-no-icons":
                require(checked["counts"]["field_evidence"] == 0, "Runtime audit rows remain")
            if "xiaomi" in name:
                require(checked["static_client_readiness"]["vowifi_ready_profiles"] == 380,
                        "Xiaomi WFC recovery regressed")
    require(seen == DB_NAMES and totals == {"no-icons": 46387200, "minimal-no-icons": 20549632},
            "Incorrect database count or byte totals")
    provenance = read_json(directory / "actions-verification.json")
    require(provenance["commit"] == COMMIT and int(provenance["run_id"]) == RUN and
            provenance["repository"] == REPO, "Embedded artifact provenance mismatch")
    return {"verified": True, "databases": 12, "assets": 20, "totals": totals,
            "commit": COMMIT, "artifact_digest": DIGEST, "build_performed": False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, method="GET", data=None, binary=False, missing=False):
        url = path if path.startswith("https://") else "https://api.github.com/repos/" + REPO + path
        require(urllib.parse.urlparse(url).hostname in {"api.github.com", "uploads.github.com"},
                "Refusing authenticated request to another host")
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            if binary:
                headers["Content-Type"] = "application/octet-stream"
            else:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing:
                return None
            raise RuntimeError(f"GitHub {method} failed (HTTP {error.code}); no destructive recovery attempted") from None

    def artifact_download(self, output):
        url = f"https://api.github.com/repos/{REPO}/actions/artifacts/{ARTIFACT}/zip"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        try:
            response = urllib.request.build_opener(NoRedirect).open(req, timeout=60)
        except urllib.error.HTTPError as error:
            require(error.code == 302, "Expected signed artifact download redirect")
            location = error.headers["Location"]
            require(urllib.parse.urlparse(location).scheme == "https", "Unsafe artifact redirect")
            # Never forward Authorization to signed storage URLs.
            response = urllib.request.urlopen(location, timeout=180)
        with response, output.open("xb") as handle:
            shutil.copyfileobj(response, handle)
        require("sha256:" + digest(output) == DIGEST, "Downloaded ZIP digest mismatch")


def check_provenance(api):
    run = api.request(f"/actions/runs/{RUN}")
    require(run["head_sha"] == COMMIT and run["conclusion"] == "success" and
            run["status"] == "completed" and run["head_repository"]["full_name"] == REPO and
            run["path"] == ".github/workflows/validate-runtime-minimal.yml", "Unverified source run")
    jobs = api.request(f"/actions/runs/{RUN}/jobs?per_page=100")
    require(jobs["total_count"] == 1 and jobs["jobs"][0]["conclusion"] == "success", "Source jobs failed")
    steps = {step["name"]: step["conclusion"] for step in jobs["jobs"][0]["steps"]}
    for name in ("Install test dependencies and check current source",
                 "Rebuild fixed Xiaomi full catalog (380 WFC-ready profiles)",
                 "Build and verify all four sources and twelve variants"):
        require(steps.get(name) == "success", "Required source test/verification did not pass")
    artifact = api.request(f"/actions/artifacts/{ARTIFACT}")
    require(artifact["id"] == ARTIFACT and not artifact["expired"] and artifact["digest"] == DIGEST and
            artifact["workflow_run"]["id"] == RUN and artifact["workflow_run"]["head_sha"] == COMMIT and
            artifact["name"] == f"runtime-minimal-validation-{RUN}", "Unverified artifact metadata")
    require(api.request("/commits/" + COMMIT)["sha"] == COMMIT, "Target commit unavailable")


def check_unused(api):
    require(api.request("/git/ref/tags/" + TAG, missing=True) is None, "Tag already exists; refusing reuse")
    require(api.request("/releases/tags/" + TAG, missing=True) is None, "Release already exists; refusing reuse")
    page = 1
    while True:
        releases = api.request(f"/releases?per_page=100&page={page}")
        require(not any(release["tag_name"] == TAG for release in releases), "Existing draft/release; refusing reuse")
        if len(releases) < 100:
            break
        page += 1


def extract_verified(archive, directory, pins):
    pins_check(pins)
    require("sha256:" + digest(archive) == DIGEST, "Artifact digest mismatch before extraction")
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        expected = {"catalog-set/" + name for name in ASSET_NAMES} | {"runtime-minimal.log", "xiaomi-rebuild.log"}
        require(len(names) == len(expected) and set(names) == expected, "Unexpected/duplicate archive paths")
        directory.mkdir(exist_ok=False)
        for name in sorted(ASSET_NAMES):
            info = zipped.getinfo("catalog-set/" + name)
            require(not stat.S_ISLNK(info.external_attr >> 16) and
                    info.file_size == pins["assets"][name]["bytes"], "Unsafe archive entry")
            path = directory / name
            with zipped.open(info) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)
            require(digest(path) == pins["assets"][name]["sha256"], "Extracted asset mismatch")
            path.chmod(0o444)  # ZIP download loses the original sealed permissions.


def check_uploaded(assets, pins):
    require(len(assets) == 20 and {asset["name"] for asset in assets} == ASSET_NAMES,
            "Release asset set is not exact")
    for asset in assets:
        expected = pins["assets"][asset["name"]]
        require(asset["state"] == "uploaded" and asset["size"] == expected["bytes"] and
                asset.get("digest") == "sha256:" + expected["sha256"], "Uploaded asset digest/size mismatch")


def check_dry_run(api, plan):
    dry = plan["validated_dry_run"]
    run = api.request("/actions/runs/" + str(dry["run_id"]))
    require(run["conclusion"] == "success" and run["head_sha"] == dry["commit"] and
            run["path"] == WORKFLOW and run["head_branch"] == os.environ["GITHUB_REF_NAME"],
            "Required safe Actions dry-run did not pass")
    comparison = api.request(f"/compare/{dry['commit']}...{os.environ['GITHUB_SHA']}")
    require(comparison["status"] == "ahead" and comparison["ahead_by"] == 1 and
            [file["filename"] for file in comparison["files"]] == [PLAN_PATH],
            "Publication code changed after safe Actions validation")


def publish(api, directory, pins, plan):
    require(plan["publish"] is True and plan["tag"] == TAG and plan["target"] == COMMIT,
            "Publication not armed for pinned tag/target")
    check_dry_run(api, plan)
    check_unused(api)  # Immediately before the first write; no automatic mutation retries.
    notes = ("Four independent schema-v7 catalogs (Pixel Mustang, iPhone 16 Pro Max 27.0.1, iOS IPCC, "
             "Xiaomi 15 Ultra Xuanyuan), each with full, no-icons and minimal-no-icons variants.\n\n"
             "These are the exact 12 prebuilt and verified Actions databases, not a release-time rebuild. "
             "Minimal-no-icons totals 20,549,632 bytes versus 46,387,200 bytes for no-icons. "
             "Xiaomi retains 380 statically WFC-ready profiles. Minimal variants omit runtime-unused audit "
             "evidence; original evidence remains in full catalogs. Schema and contract remain v7.\n\n"
             "Offline checks/simulations are not proof of live network registration or carrier certification. "
             "No device access or live-network verification was performed.\n\n"
             f"Source commit: `{COMMIT}`\nVerified build: https://github.com/{REPO}/actions/runs/{RUN}\n"
             f"Artifact SHA-256: `{DIGEST.removeprefix('sha256:')}`\n\n"
             "`actions-verification.json` is preserved byte-for-byte from the pre-publication build "
             "and therefore records `publication: false`; this is historical build provenance. "
             "`SHA256SUMS` covers all other 19 assets. The release target is the verified snapshot, "
             "not the publication workflow commit.\n")
    release = api.request("/releases", "POST", {"tag_name": TAG, "target_commitish": COMMIT,
                          "name": "Carrier catalogs v0.3.1 (schema v7, four-source runtime-minimal set)",
                          "body": notes, "draft": True, "prerelease": False, "make_latest": "false"})
    release_id = release["id"]
    require(release["draft"] is True and release["target_commitish"] == COMMIT,
            "New draft target mismatch; leaving it untouched")
    upload_url = release["upload_url"].split("{")[0]
    for name in sorted(ASSET_NAMES):
        require(digest(directory / name) == pins["assets"][name]["sha256"], "Asset changed before upload")
        api.request(upload_url + "?name=" + urllib.parse.quote(name), "POST",
                    (directory / name).read_bytes(), binary=True)
        print("Uploaded verified asset:", name, flush=True)
    assets = api.request(f"/releases/{release_id}/assets?per_page=100")
    check_uploaded(assets, pins)
    draft = api.request(f"/releases/{release_id}")
    require(draft["draft"] is True and draft["tag_name"] == TAG and draft["target_commitish"] == COMMIT,
            "Draft changed unexpectedly; refusing publication")
    tag = api.request("/git/ref/tags/" + TAG, missing=True)
    require(tag is None or tag["object"]["sha"] == COMMIT, "Concurrent tag target mismatch")
    published = api.request(f"/releases/{release_id}", "PATCH", {"draft": False, "make_latest": "true"})
    require(published["draft"] is False, "Release did not become public")
    require(api.request("/git/ref/tags/" + TAG)["object"]["sha"] == COMMIT, "Published tag target mismatch")
    check_uploaded(api.request(f"/releases/{release_id}/assets?per_page=100"), pins)
    return {"url": published["html_url"], "tag": TAG, "target": COMMIT, "assets": 20,
            "databases": 12, "published": True, "publication_run": os.environ["GITHUB_RUN_ID"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "publish", "local-readonly"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--pins", type=Path, default=ROOT / "release-staging/catalog-set/pins.json")
    parser.add_argument("--plan", type=Path, default=ROOT / PLAN_PATH)
    args = parser.parse_args()
    pins = read_json(args.pins)
    if args.mode == "local-readonly":
        print(json.dumps(validate_set(args.directory, pins, require_readonly=False), indent=2))
        return
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY") == REPO and
            os.environ.get("GITHUB_REF", "").startswith("refs/heads/release-staging/catalog-set/"),
            "Publication workflow must run on its isolated branch")
    plan = read_json(args.plan)
    require(plan["tag"] == TAG and plan["target"] == COMMIT, "Unexpected publication plan")
    api = GitHub(os.environ["GH_TOKEN"])
    check_provenance(api)
    check_unused(api)
    archive = Path("pinned-catalog-artifact.zip")
    directory = Path("verified-release-assets")
    api.artifact_download(archive)
    extract_verified(archive, directory, pins)
    evidence = validate_set(directory, pins)
    if args.mode == "publish":
        evidence.update(publish(api, directory, pins, plan))
    else:
        evidence.update({"published": False, "dry_run": True})
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("publish=" + str(plan["publish"] is True).lower() + "\n")
    Path("publication-proof.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
