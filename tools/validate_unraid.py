"""Local preflight for this repository's Unraid app; the CA portal is authoritative."""

from __future__ import annotations

import re
import shlex
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(node, name):
    return (node.findtext(name) or "").strip()


def local_asset(url, raw_base, root):
    require(url.startswith(raw_base + "/"), f"Asset URL must use this repository's raw main branch: {url}")
    relative = url.removeprefix(raw_base + "/")
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()) and path.is_file(), f"Missing local asset: {relative}")
    return path


def validate(root=ROOT):
    profile = ET.parse(root / "ca_profile.xml").getroot()
    require(profile.tag == "CommunityApplications" and text(profile, "Profile"), "ca_profile.xml needs CommunityApplications/Profile")
    require("MIT License" in (root / "LICENSE").read_text(), "Keep the app's OSI-approved MIT license")
    files = sorted((root / "templates").glob("*.xml"))
    require(files, "No app templates found in templates/")
    names = set()
    for file in files:
        app = ET.parse(file).getroot()
        require(app.tag == "Container" and app.get("version") == "2", f"{file.name}: use Container version 2")
        name = text(app, "Name")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) and name not in names, "App names must be valid, unique Docker names")
        names.add(name)
        project = text(app, "Project")
        parsed = urlsplit(project)
        require(parsed.scheme == "https" and parsed.netloc == "github.com" and len(parsed.path.strip("/").split("/")) == 2, "Project must point to the public GitHub repository")
        repository = parsed.path.strip("/")
        raw_base = f"https://raw.githubusercontent.com/{repository}/main"
        require(text(profile, "WebPage") == project, "Profile and app project URLs disagree")
        require(text(app, "Repository") == f"ghcr.io/{repository.lower()}:latest", "App image must match the GHCR release workflow")
        require(text(app, "Registry") == f"https://ghcr.io/{repository.lower()}", "Registry URL does not match the image")
        require(text(app, "TemplateURL") == f"{raw_base}/{file.relative_to(root).as_posix()}", "TemplateURL must point to this exact XML file")
        require(text(app, "Overview") and text(app, "Category") == "Tools:", "Provide an overview and the Tools category")
        require(text(app, "License") == "MIT", "Template license must match LICENSE")
        require(text(app, "Network") == "bridge" and text(app, "Privileged") == "false", "Use bridge networking without privileged mode")
        require(not text(app, "WebUI"), "The headless worker has no WebUI")
        require(text(app, "Shell") == "sh", "Use the shell provided by the image")
        flags = shlex.split(text(app, "ExtraParams"))
        required_flags = {"--user=99:100", "--restart=unless-stopped", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"}
        require(required_flags.issubset(flags) and not any(flag.startswith("--privileged") for flag in flags), "Missing non-root, read-only, or restart settings")
        for element in (profile, app):
            icon = local_asset(text(element, "Icon"), raw_base, root)
            header = icon.read_bytes()[:24]
            require(len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR", "Provide a PNG icon")
            require(struct.unpack(">II", header[16:24]) == (256, 256), "Use the supplied 256 by 256 icon")
        local_asset(text(app, "ReadMe"), raw_base, root)
        configs = {}
        for config in app.findall("Config"):
            require({"Name", "Target", "Default", "Mode", "Description", "Type", "Display", "Required", "Mask"}.issubset(config.attrib), "Every setting needs the v2 Config attributes")
            require(config.get("Type") in {"Path", "Variable"}, "No host port or device mappings are needed")
            require(config.get("Display") in {"always", "advanced", "hidden"}, "Invalid setting visibility")
            require(config.get("Required") in {"true", "false"} and config.get("Mask") in {"true", "false"}, "Invalid boolean Config attributes")
            target = config.get("Target")
            require(target not in configs, f"Duplicate setting: {target}")
            configs[target] = config
        require("/config" in configs and configs["/config"].get("Type") == "Path" and configs["/config"].get("Mode") == "rw", "Map writable persistent appdata to /config")
        require(configs["/config"].get("Default") == "/mnt/user/appdata/launch-alert", "Unexpected default appdata path")
        for target, value in {"DB_PATH": "/config/launch-alert.sqlite3", "ENV_FILE": "/dev/null", "LEAD_MINUTES": "30", "POLL_SECONDS": "300", "MAX_DATA_AGE_SECONDS": "900", "NTFY_SERVER": "https://ntfy.sh"}.items():
            require(target in configs and configs[target].get("Default") == value, f"Unexpected default for {target}")
        for target in ("NTFY_TOPIC", "NTFY_TOKEN", "LL2_TOKEN"):
            require(target in configs and not configs[target].get("Default") and not (configs[target].text or "").strip() and configs[target].get("Mask") == "true", f"{target} must be masked with no shared secret default")
        require(configs["NTFY_TOPIC"].get("Required") == "true", "Prompt each user for a private topic")
        require("DISPLAY_TIMEZONE" in configs and not configs["DISPLAY_TIMEZONE"].get("Default"), "Inherit Unraid's TZ unless the user overrides it")
        require(not ({"PUID", "PGID"} & configs.keys()), "This image uses --user, not PUID/PGID environment variables")
    return len(files)


def main():
    try:
        count = validate()
    except (ValueError, OSError, ET.ParseError) as error:
        print(f"Unraid preflight failed: {error}", file=sys.stderr)
        return 1
    print(f"Unraid preflight passed: profile, {count} template, icon, URLs, settings, and persistence.")
    print("Public image/URL availability and real Unraid compatibility still need release testing and CA portal validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
