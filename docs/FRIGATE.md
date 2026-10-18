# 📹 Frigate NVR

Frigate runs in the **`nvr`** stack ([`stacks/nvr/docker-compose.yml`](../stacks/nvr/docker-compose.yml)). It records the IP cameras and runs object detection on the host's Intel iGPU.

> 🔒 Site-specific details live in **`${APP_DATA}/frigate/NOTES.md`**, which is not in git: addresses, VLAN and firewall rules, camera models and firmware, and the camera inventory. Keep them out of this public repo.

| What | Where |
| --- | --- |
| UI | `https://frigate.${DOMAIN_NAME}` (NPM). Login required |
| Config | `${APP_DATA}/frigate/config.yml` (not in git) |
| Detection model | `${APP_DATA}/frigate/model_cache/yolo.onnx` |
| Recordings | Dedicated array-only Unraid share (see [Storage](#-storage)) |
| Camera setup script | [`stacks/nvr/scripts/hikvision-frigate-setup.py`](../stacks/nvr/scripts/hikvision-frigate-setup.py) |

## 🔐 Stack variables (`stacks/nvr/.env.local`)

| Variable | Notes |
| --- | --- |
| `FRIGATE_MQTT_USER` / `FRIGATE_MQTT_PASSWORD` | Broker login |
| `FRIGATE_RTSP_USER` / `FRIGATE_RTSP_PASSWORD` | Camera admin login. Used raw by the setup script and camera APIs |
| `FRIGATE_RTSP_PASSWORD_URLENCODED` | Same password, URL-encoded, for RTSP URLs in `config.yml`. Regenerate with `python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' 'PASSWORD'` |
| `FRIGATE_NTP_SERVER` | NTP server the setup script points cameras at (the cameras have no internet) |

Frigate substitutes any `FRIGATE_*` env var into `config.yml` as `{FRIGATE_VAR}`. After editing, run `npm run sync-env` and restart the stack.

## 🖥️ Hardware

- **iGPU:** only `/dev/dri/renderD128` is passed through, no `privileged`. GPU stats work without extra caps on kernel ≥ 6.5.
- **Decode:** `ffmpeg.hwaccel_args: preset-vaapi`.
- **Detector:** OpenVINO on `GPU` with **YOLOv9-t @ 320** (`model_type: yolo-generic`, `coco-80` labelmap). OpenVINO ships no bundled model. The "Mobiledet" option in the docs' model picker is for Coral EdgeTPU only.
- **shm:** each 1280x720 detect stream needs ~40 MB: `(1280*720*1.5 + 270480) / 1048576 * 25`. Add ~58 MB of overhead and raise `shm_size` when adding cameras or if logs show `Bus error`.

### Rebuilding the model

Run the YOLOv9 export from the [Frigate docs](https://docs.frigate.video/configuration/object_detectors#yolov9) in an empty temp dir with `MODEL_SIZE=t` and `IMG_SIZE=320`, then:

```bash
cp yolov9-t-320.onnx /mnt/user/appdata/frigate/model_cache/yolo.onnx
docker builder prune -af   # the export leaves ~7 GB of build cache
```

The export prints an ONNX `No Adapter To Version $17 for Resize` error and then `export success`. That's expected.

## 📷 Camera-side settings

### Hikvision

Applied over ISAPI with the setup script, which is idempotent:

```bash
python3 stacks/nvr/scripts/hikvision-frigate-setup.py --dry-run CAMERA_IP   # fetch only
python3 stacks/nvr/scripts/hikvision-frigate-setup.py CAMERA_IP [CAMERA_IP ...]
```

| Setting | Value | Why |
| --- | --- | --- |
| Main stream — record + live | Full resolution, **15 fps, GOP 30** (2 s), H.264 | Frigate recommends 15 fps for recording. 2 s keyframes keep segment cuts and live start fast |
| Main stream quality | **VBR, quality Highest, cap 8192 kbps** | Sharper faces/plates. VBR keeps still scenes small |
| Main stream audio | **On, AAC** | Recorded with `preset-record-generic-audio-copy`, so no transcode |
| Sub stream — detect | **1280x720, 6 fps, GOP 6** | 5 fps isn't offered; Frigate samples to 5 |
| Sub stream quality | **VBR, Highest, cap 2048 kbps** | Snapshots, thumbnails and face/plate crops come from the detect stream. Not recorded |
| Smart Codec (H.264+) | **Off** | Smart codecs stretch keyframe intervals, which breaks segmenting and live view |
| RTSP auth | Digest, MD5 | Required by newer firmware for Frigate |
| Time | NTP by IP (`FRIGATE_NTP_SERVER`), hourly | No internet or DNS on the camera network |
| Channel-name OSD | **Off** (date/time stays) | Frigate shows the real camera name |
| SSH | **Off** | Not needed. Reduces attack surface |

**Gotchas:**

- Changing sub-stream resolution resets its frame rate, so the script sets resolution and fps in **two separate PUTs**.
- The camera answers `OK` even for an unsupported value and silently substitutes its own. Always read the setting back.
- ffprobe/ffmpeg error messages print the full input URL, **password included**. Redact output when testing streams.

### Reolink doorbell

Set via the HTTP API (`/api.cgi`). Enable HTTP, RTMP, RTSP and ONVIF in the Reolink app first.

| Setting | Value |
| --- | --- |
| Main stream | 15 fps, interframe 1x, max bitrate |
| Sub stream (detect) | 10 fps, interframe 1x, max bitrate. Override `detect` to the sub-stream size in `config.yml` |
| OSD | Time only |
| Cloud / P2P / push / email / FTP / UPnP | Off (offline network) |
| On-camera recording | Off. Frigate is the only recorder |

- Get value ranges from `GetEnc` with `"action": 1`.
- **Only send commands listed by `GetAbility`**, and prefer the `…V20` variants. Unknown commands can wedge the API until the device restarts.
- Frigate uses RTSP without an `ffmpeg:` prefix so go2rtc can use the backchannel for two-way talk. HTTP-FLV is Frigate's preferred Reolink stream, but it only works if the password has no special characters.
- Doorbell button presses aren't video events, so Frigate can't see them. Getting them into Home Assistant needs the HA Reolink integration and a firewall allow from HA to the doorbell.

### ⬆️ Firmware upgrades (Hikvision)

- Download from Hikvision's regional firmware page for the **exact model**. Some region pages list stale builds, so compare against what the camera reports so you don't downgrade by accident.
- Flash over ISAPI:

  ```bash
  curl --digest -u USER:PASS -X PUT -H 'Content-Type: application/octet-stream' \
    -T digicap.dav http://CAMERA_IP/ISAPI/System/updateFirmware   # expect subStatusCode rebootRequired
  curl --digest -u USER:PASS -X PUT http://CAMERA_IP/ISAPI/System/reboot
  ```

- Then re-run the setup script and probe both streams. Do one camera first when trying a new build.

### Adding a camera to Frigate

1. Apply the camera settings (script or API).
2. In `config.yml`, add two go2rtc streams: `<name>` (main stream plus `"ffmpeg:<name>#audio=opus"` for WebRTC audio) and `<name>_sub` (sub stream).
3. Add a `cameras.<name>` entry with the `record` input from `rtsp://127.0.0.1:8554/<name>` and the `detect` input from `rtsp://127.0.0.1:8554/<name>_sub`, both with `input_args: preset-rtsp-restream`. Set `ui.order` for its position in the UI.
4. Recheck `shm_size`.

## 💾 Storage

Recordings go to a dedicated Unraid share named **`nvr`** (generic in case Frigate is replaced). Frigate uses the `frigate/` subfolder.

| Setting | Value | Why |
| --- | --- | --- |
| Primary storage | Array, a single included disk | Only that disk and parity stay spun up. The other disks can spin down |
| Secondary storage (cache) | **None** | Writing through the cache would wear the SSDs, could fill the pool, and leave the mover fighting Frigate's own cleanup |
| Export (SMB/NFS) | Off | Only the container needs it |

NAS/enterprise 24/7-rated drives are fine. A surveillance-specific HDD isn't required.

## 🌐 Network

Cameras live on an **isolated VLAN with no internet**. Only the NVR host may open connections to them, on RTSP, HTTP (camera API) and ONVIF. The cameras may only reach DHCP on the gateway and NTP on the NVR host. The exact rules are in the private notes.

- Zone-based firewall: give the cameras their **own zone**, so it starts deny-all both ways. Turn **off** the network's "Isolate Network" toggle, because it would also block the NVR.
- **Auto Allow Return Traffic** must be on for the NVR → cameras policy, or connections time out.
- Docker containers on a bridge network are masqueraded to the host IP, so the policy should use the host as the source.
- The built-in "no internet" block is IPv4-only. Keep IPv6 off on the camera network.
- After moving cameras to the VLAN, PoE power-cycle their ports so they request a new DHCP lease.

## ⚙️ Frigate settings worth knowing

- **TLS off in Frigate:** NPM terminates HTTPS. Port 8971 still enforces login. Port 5000 (no auth) is not published.
- **WebRTC:** set the host IP as a candidate, plus `stun:8555`. Ports 8555/tcp+udp are published.
- **Retention:** no continuous recording. Motion, alerts, detections and snapshots are kept for 14 days.
- **Objects:** Frigate's default (`person`). Add `car` per camera together with zones, otherwise parked cars become constant detections.
- **First login / reset:** the admin password is printed in `docker logs frigate` on first start. A recreated container loses that log. To reset, set `auth.reset_admin_password: true`, restart, read the log, then remove the option.
- **No `/etc/localtime` mount:** on Unraid it's a dangling symlink, and Docker fails with `mkdir /etc/localtime: file exists`. `TZ: ${TZ}` is enough.
