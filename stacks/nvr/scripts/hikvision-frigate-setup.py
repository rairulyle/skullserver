#!/usr/bin/env python3
"""Apply Frigate-friendly settings to Hikvision cameras over ISAPI.

Usage: hikvision-frigate-setup.py [--dry-run] IP [IP ...]

Reads FRIGATE_RTSP_USER / FRIGATE_RTSP_PASSWORD / FRIGATE_NTP_SERVER from stacks/nvr/.env.local.
Settings and rationale: docs/FRIGATE.md."""
import pathlib
import re
import sys
import urllib.request

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env.local"

# (ISAPI path, [(regex, replacement)]) applied in order. Main stream codec and sub stream
# resolution get their own PUTs because the camera resets the bitrate cap on codec change
# and the frame rate on resolution change.
def steps(ntp_server):
    return [
        ("/ISAPI/Streaming/channels/101", [  # main/record: H.265
            (r"<videoCodecType>[^<]*<", "<videoCodecType>H.265<"),
        ]),
        ("/ISAPI/Streaming/channels/101", [  # main/record: 15 fps, 2 s keyframes, highest VBR quality, AAC audio
            (r"<maxFrameRate>\d+<", "<maxFrameRate>1500<"),
            (r"<GovLength>\d+<", "<GovLength>30<"),
            (r"<videoQualityControlType>[^<]*<", "<videoQualityControlType>VBR<"),
            (r"<fixedQuality>\d+<", "<fixedQuality>100<"),
            # The cap is what sets the H.265 bitrate: at 8192 it runs ~7 Mbps (more than H.264
            # did), at 4096 ~3.7 Mbps, about 38% below H.264 at 8192 with similar detail
            (r"<vbrUpperCap>\d+<", "<vbrUpperCap>4096<"),
            (r"(<Audio>\s*<enabled>)false", r"\1true"),
            (r"<audioCompressionType>[^<]*<", "<audioCompressionType>AAC<"),
        ]),
        ("/ISAPI/Streaming/channels/102", [  # sub/detect: 1280x720
            (r"<videoResolutionWidth>\d+<", "<videoResolutionWidth>1280<"),
            (r"<videoResolutionHeight>\d+<", "<videoResolutionHeight>720<"),
        ]),
        ("/ISAPI/Streaming/channels/102", [  # 6 fps (5 is not offered), 1 s keyframes, highest VBR quality
            (r"<maxFrameRate>\d+<", "<maxFrameRate>600<"),
            (r"<GovLength>\d+<", "<GovLength>6<"),
            (r"<videoQualityControlType>[^<]*<", "<videoQualityControlType>VBR<"),
            (r"<fixedQuality>\d+<", "<fixedQuality>100<"),
            (r"<vbrUpperCap>\d+<", "<vbrUpperCap>2048<"),
        ]),
        ("/ISAPI/System/time/ntpServers", [  # NTP by IP, hourly sync
            (r"<addressingFormatType>[^<]*</addressingFormatType>\s*<(hostName|ipAddress)>[^<]*</\1>",
             f"<addressingFormatType>ipaddress</addressingFormatType>\n<ipAddress>{ntp_server}</ipAddress>"),
            (r"<synchronizeInterval>\d+<", "<synchronizeInterval>60<"),
        ]),
        ("/ISAPI/System/time", [
            (r"<timeMode>[^<]*<", "<timeMode>NTP<"),
        ]),
        ("/ISAPI/System/Video/inputs/channels/1/overlays", [  # hide "Camera 01" OSD; date/time stays
            (r"(<channelNameOverlay>\s*<enabled>)true", r"\1false"),
        ]),
    ]


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"')
    return env


def opener(ip, user, password):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{ip}/", user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))


def configure(ip, user, password, ntp_server, dry_run):
    http = opener(ip, user, password)
    for path, edits in steps(ntp_server):
        url = f"http://{ip}{path}"
        xml = http.open(url, timeout=10).read().decode()
        for pattern, repl in edits:
            xml = re.sub(pattern, repl, xml, count=1)
        if dry_run:
            print(f"{ip} {path} -> (dry run)")
            continue
        req = urllib.request.Request(url, data=xml.encode(), method="PUT",
                                     headers={"Content-Type": "application/xml"})
        status = re.search(r"<statusString>([^<]*)", http.open(req, timeout=15).read().decode())
        print(f"{ip} {path} -> {status.group(1) if status else 'unknown'}")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    ips = [a for a in args if a != "--dry-run"]
    if not ips:
        sys.exit(__doc__)
    env = load_env()
    for ip in ips:
        configure(ip, env["FRIGATE_RTSP_USER"], env["FRIGATE_RTSP_PASSWORD"], env["FRIGATE_NTP_SERVER"], dry_run)


if __name__ == "__main__":
    main()
