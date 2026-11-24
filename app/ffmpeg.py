"""Builds ffmpeg command lines and checks what this machine's ffmpeg can do.

Every piece that plays (episode part, commercial, "be right back" card) is
encoded to exactly the same format, so pieces can be joined into one endless
stream without the player noticing the seams.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("rerun.ffmpeg")

RESOLUTIONS = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}
FPS = "30000/1001"
GOP = 60
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


ENCODER_LABELS = {"none": "the processor", "vaapi": "Intel Quick Sync / AMD (VA-API)",
                  "vaapi_lp": "Intel Quick Sync, low-power mode", "nvenc": "NVIDIA NVENC", "auto": "Automatic"}
DEFAULT_RENDER = "/dev/dri/renderD128"


def render_device(configured: str | None = None) -> str:
    """The graphics chip to use. Left on the default, prefer Intel, then AMD, when a machine has several."""
    if configured and configured != DEFAULT_RENDER:
        return configured
    rank = {"i915": 0, "xe": 0, "amdgpu": 1, "radeon": 2}
    found = []
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        driver = os.path.basename(os.path.realpath(f"/sys/class/drm/{os.path.basename(node)}/device/driver"))
        found.append((rank.get(driver, 9), node))
    return min(found)[1] if found else DEFAULT_RENDER


def _tool(env: str, name: str) -> str:
    chosen = os.environ.get(env, "")
    if chosen and os.access(chosen, os.X_OK):
        return chosen
    return shutil.which(name) or name


def filter_escape(value: str) -> str:
    """Escape a path for a filter option inside -vf (ffmpeg parses two levels of escaping)."""
    v = value.replace("\\", "/")
    for ch in ("\\", "'", ":"):  # level 1: the filter's own option parser
        v = v.replace(ch, "\\" + ch)
    for ch in ("\\", "'", "[", "]", ",", ";"):  # level 2: the filtergraph parser
        v = v.replace(ch, "\\" + ch)
    return v


class FFmpeg:
    def __init__(self):
        # The installer may point these at a newer ffmpeg (Jellyfin's build on Ubuntu 22.04).
        self.ffmpeg = _tool("RERUN_FFMPEG", "ffmpeg")
        self.ffprobe = _tool("RERUN_FFPROBE", "ffprobe")
        self.version = ""
        self.major = 0
        self.encoders: set[str] = set()
        self.filters: set[str] = set()
        self.has_burst = False
        self.font = next((f for f in FONT_CANDIDATES if os.path.exists(f)), "")
        self.encoder_status = {"requested": "none", "using": "none", "label": ENCODER_LABELS["none"], "ok": False,
                               "message": "Not tested yet"}

    def _run(self, *args) -> str:
        try:
            r = subprocess.run([self.ffmpeg, "-hide_banner", *args], capture_output=True, text=True, timeout=30,
                               errors="replace")
            return r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            log.error("Could not run ffmpeg: %s", e)
            return ""

    def detect(self):
        out = self._run("-version")
        m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
        self.version = out.splitlines()[0] if out else "ffmpeg not found"
        self.major = int(m.group(1)) if m else 0
        minor = int(m.group(2)) if m else 0
        self.encoders = set(re.findall(r"^\s*V\S*\s+(\S+)", self._run("-encoders"), re.M))
        self.filters = set(re.findall(r"^\s*\S+\s+(\S+)\s+\S+->\S+", self._run("-filters"), re.M))
        # -readrate_initial_burst arrived in ffmpeg 6.1; it lets each piece start a little ahead of real time.
        self.has_burst = (self.major, minor) >= (6, 1) or "readrate_initial_burst" in self._run("-h", "full")
        if os.environ.get("RERUN_TEST_OLD_FFMPEG"):  # for testing the ffmpeg 4.4 path on a newer ffmpeg
            self.has_burst = False
        log.info("Using %s (burst=%s, drawtext=%s)", self.version, self.has_burst, "drawtext" in self.filters)

    @property
    def available(self) -> bool:
        return self.major > 0

    # ---- encoder selection ------------------------------------------------------------
    # "vaapi" is Intel Quick Sync (and AMD) through VA-API; "vaapi_lp" is the same chip in low-power
    # mode, which newer Intel chips (Ice Lake / Jasper Lake / Alder Lake N and later) may require.
    def _video_codec(self, s: dict, hw: str) -> list[str]:
        vk = int(s.get("video_kbps") or 4000)
        rate = ["-b:v", f"{vk}k", "-maxrate", f"{vk}k", "-bufsize", f"{vk * 2}k", "-g", str(GOP)]
        if hw in ("vaapi", "vaapi_lp"):
            return ["-c:v", "h264_vaapi", *(["-low_power", "1"] if hw == "vaapi_lp" else []), *rate, "-bf", "0"]
        if hw == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "cbr", *rate, "-bf", "0"]
        # superfast: about a quarter less processor than veryfast for a picture that looks the same on TV
        return ["-c:v", "libx264", "-preset", "superfast", "-profile:v", "high", "-level:v", "4.1", *rate,
                "-keyint_min", str(GOP), "-sc_threshold", "0", "-bf", "2"]

    def _hw_prefix(self, s: dict, hw: str, decode: bool = False) -> list[str]:
        if hw not in ("vaapi", "vaapi_lp"):
            return ["-hwaccel", "cuda"] if hw == "nvenc" and decode else []
        args = ["-init_hw_device", f"vaapi=va:{render_device(s.get('vaapi_device'))}", "-filter_hw_device", "va"]
        # Decoding on the graphics chip too takes most of the work off the processor. Frames come back to
        # memory for the picture filters, and anything the chip can't decode is decoded normally.
        return args + (["-hwaccel", "vaapi", "-hwaccel_device", "va"] if decode else [])

    def _vf_tail(self, hw: str) -> str:
        return "format=nv12,hwupload" if hw in ("vaapi", "vaapi_lp") else "format=yuv420p"

    def candidates(self, requested: str) -> list[str]:
        has_gpu = bool(glob.glob("/dev/dri/renderD*"))
        if requested == "auto":
            tries = ["vaapi", "vaapi_lp"] if has_gpu and "h264_vaapi" in self.encoders else []
        elif requested == "vaapi":
            tries = ["vaapi", "vaapi_lp"]
        else:
            tries = [requested]
        return list(dict.fromkeys([*tries, "none"]))

    async def self_test(self, s: dict) -> dict:
        """Try the chosen encoder for one second; fall back to plain software encoding if it fails."""
        requested = s.get("hwaccel") or "none"
        W, H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])
        status = {"requested": requested, "using": "none", "label": ENCODER_LABELS["none"], "ok": False, "message": ""}
        for hw in self.candidates(requested):
            args = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *self._hw_prefix(s, hw),
                    "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}", "-t", "1",
                    "-vf", self._vf_tail(hw), *self._video_codec(s, hw), "-f", "null", "-"]
            try:
                proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.PIPE)
                _, err = await asyncio.wait_for(proc.communicate(), 30)
                ok = proc.returncode == 0
            except (OSError, asyncio.TimeoutError) as e:
                ok, err = False, str(e).encode()
            if ok:
                status.update(using=hw, label=ENCODER_LABELS[hw], ok=True)
                if hw == "none" and requested not in ("none", "auto"):
                    status["message"] = (f"{ENCODER_LABELS.get(requested, requested)} didn't work on this machine, so "
                                         "Rerun is using the processor instead. Run “rerun check-gpu” on the server to see why.")
                if hw != "none":
                    status["device"] = render_device(s.get("vaapi_device")) if hw != "nvenc" else ""
                break
            log.warning("Encoder test failed for %s: %s", hw, err.decode(errors="replace").strip()[-300:])
        if not status["ok"]:
            status["message"] = "ffmpeg couldn't encode video. Check that ffmpeg is installed."
        self.encoder_status = status
        return status

    # ---- command lines ----------------------------------------------------------------
    def _input_pacing(self, burst: float | None) -> list[str]:
        if burst is None:  # a quick-start piece: made as fast as the machine can, not at play speed
            return []
        args = ["-re"]
        if self.has_burst and burst > 0:
            args += ["-readrate_initial_burst", f"{burst:.1f}"]
        return args

    def _audio_out(self, s: dict) -> list[str]:
        af = "aresample=async=1:first_pts=0"
        if s.get("even_volume", True):
            af += ",loudnorm=I=-18:TP=-2:LRA=11,aresample=48000"
        return ["-af", af, "-c:a", "aac", "-b:a", f"{int(s.get('audio_kbps') or 192)}k", "-ac", "2", "-ar", "48000"]

    def _ts_out(self, duration: float) -> list[str]:
        # flush_packets: hand each bit of picture on as soon as it's made, rather than in 32 KB lumps.
        return ["-t", f"{duration:.3f}", "-max_muxing_queue_size", "4096", "-flush_packets", "1",
                "-f", "mpegts", "pipe:1"]

    def media_cmd(self, src: str, offset: float, duration: float, s: dict, burst: float, has_audio: bool,
                  hw_decode: bool = True, overlay: dict | None = None) -> list[str]:
        """One piece of a show or commercial, as live TV. `overlay` can add the network's logo in the
        corner ("logo": a picture file) and the "Up next" banner ("banner": a picture file, shown from t0 to
        t1 seconds into the piece, fading in as it slides in from the left). Both sit inside the picture
        itself, before any side bars are added."""
        hw = self.encoder_status.get("using") or "none"
        W, H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])
        ov = overlay or {}
        args = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *self._hw_prefix(s, hw, hw_decode)]
        args += self._input_pacing(burst)
        if src.startswith(("http://", "https://")):
            args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        else:
            src = "file:" + src  # never let a file name be read as some other ffmpeg protocol
        if offset > 0.05:
            args += ["-ss", f"{offset:.3f}"]
        args += ["-t", f"{duration + 1:.3f}", "-i", src]
        n = 1
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            audio, n = f"{n}:a:0", n + 1
        else:
            audio = "0:a:0"
        can = "overlay" in self.filters
        logo = ov.get("logo") if can and os.path.isfile(ov.get("logo") or "") else None
        # banners: "Up next", "After the break", promos; each {file, art, t0, t1, motion, place}
        banners = list(ov.get("banners") or [])
        if ov.get("banner"):  # the older single-banner form
            banners.append({"file": ov["banner"], "art": ov.get("banner_art"), "t0": ov.get("t0"), "t1": ov.get("t1"),
                            "motion": ov.get("motion"), "place": ov.get("place")})
        banners = [b for b in banners if can and os.path.isfile(b.get("file") or "")]
        fit = f"bwdif=mode=send_frame:parity=auto:deint=interlaced,scale={W}:{H}:force_original_aspect_ratio=decrease"
        tail = f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},{self._vf_tail(hw)}"
        tpl = ov.get("template") if can and os.path.isfile((ov.get("template") or {}).get("file") or "") else None
        if not (logo or banners or tpl):
            args += ["-map", "0:v:0", "-vf", f"{fit},{tail}"]
        else:
            graph, cur = [f"[0:v:0]{fit}[pic]"], "pic"
            if logo:  # a still picture, held for the whole piece
                args += ["-loop", "1", "-framerate", "5", "-i", "file:" + logo]
                graph.append(f"[{cur}][{n}:v:0]overlay=x=main_w-overlay_w-main_w*0.045:"
                             f"y=main_h-overlay_h-main_h*0.065:eof_action=repeat[pb]")
                cur, n = "pb", n + 1
            for j, ban in enumerate(banners):
                a, b = float(ban.get("t0") or 0), float(ban.get("t1") or 0)
                rest = f"max(0,1-(t-{a:.2f})/0.45)"  # 1 when the banner appears, 0 once it's in place
                motion = ban.get("motion") or "left"  # each style comes on its own way, ending where it sits

                def moved(x, y):
                    if motion == "left":
                        return f"{x}-main_w*0.035*{rest}", y
                    if motion == "right":
                        return f"{x}+main_w*0.05*{rest}", y
                    if motion == "up":
                        return x, f"{y}+main_h*0.06*{rest}"
                    if motion == "rise":  # comes up from below the picture
                        return x, f"{y}+overlay_h*{rest}"
                    return x, y

                place = ban.get("place") or ""
                x, y = "main_w*0.055", "main_h*0.73"
                if place == "bottom":  # along the bottom edge of the picture
                    x, y = "main_w*0.012", "main_h-overlay_h"
                elif place == "edge":  # from the picture's very left edge; whatever's wider runs off the right
                    x, y = "0", "main_h-overlay_h"
                layers = [(ban["file"], *moved(x, y))]
                if ban.get("art") and os.path.isfile(ban["art"]):  # its artwork, near the picture's right edge
                    layers.append((ban["art"], *moved("main_w*0.95-overlay_w", "main_h-overlay_h-4")))
                for i, (pic, lx, ly) in enumerate(layers):
                    args += ["-loop", "1", "-framerate", "15", "-t", f"{duration + 1:.3f}", "-i", "file:" + pic]
                    graph.append(f"[{n}:v:0]format=rgba,fade=t=in:st={a:.2f}:d=0.4:alpha=1,"
                                 f"fade=t=out:st={b - 0.4:.2f}:d=0.4:alpha=1[ban{j}_{i}]")
                    graph.append(f"[{cur}][ban{j}_{i}]overlay=x='{lx}':y='{ly}':eval=frame:"
                                 f"enable='between(t,{a:.2f},{b:.2f})'[pn{j}_{i}]")
                    cur, n = f"pn{j}_{i}", n + 1
            if tpl:
                n, cur = self._template(args, graph, tpl, cur, n, W, H, duration)
                graph.append(f"[{cur}]{self._vf_tail(hw)}[v]")
            else:
                graph.append(f"[{cur}]{tail}[v]")
            args += ["-filter_complex", ";".join(graph), "-map", "[v]"]
        args += ["-map", audio, "-sn", "-dn", *self._video_codec(s, hw), *self._audio_out(s), *self._ts_out(duration)]
        return args

    def _template(self, args: list, graph: list, tpl: dict, cur: str, n: int, W: int, H: int,
                  duration: float) -> tuple[int, str]:
        """Your own "Up next" template, laid over the whole picture (after the side bars), its green or
        black made see-through. With a window, the show squeezes into it while the template plays."""
        a, b = float(tpl["t0"]), float(tpl["t1"])
        pad_tail = f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}"
        if tpl.get("window"):  # the picture itself (no side bars) is what squeezes into the window
            graph.append(f"[{cur}]split=3[p1][p2][p3]")
            graph.append(f"[p1]{pad_tail}[full]")
        else:
            graph.append(f"[{cur}]{pad_tail}[full]")
        if tpl.get("video"):
            args += ["-i", "file:" + tpl["file"]]
            src = f"[{n}:v:0]setpts=PTS+{a:.3f}/TB,"
        else:
            args += ["-loop", "1", "-framerate", "15", "-t", f"{duration + 1:.3f}", "-i", "file:" + tpl["file"]]
            src = f"[{n}:v:0]"
        n += 1
        key = tpl.get("key") or "0x00ff00"
        keyer = (f"colorkey={key}:0.08:0.05" if tpl.get("how") == "black"
                 else f"format=yuva420p,chromakey={key}:0.14:0.06")  # tight: yellows (SpongeBob) sit close to green
        tw, th = int(tpl.get("w") or W), int(tpl.get("h") or H)
        sc = min(W / tw, H / th)
        fw, fh = int(tw * sc) // 2 * 2, int(th * sc) // 2 * 2
        ox, oy = (W - fw) // 2, (H - fh) // 2
        graph.append(f"{src}{keyer},scale={fw}:{fh},format=rgba,pad={W}:{H}:{ox}:{oy}:color=0x00000000[tk]")
        on = f"enable='between(t,{a:.2f},{b:.2f})'"
        if tpl.get("window"):
            wx, wy, ww, wh = tpl["window"]
            sw, shh = int(fw * ww) // 2 * 2, int(fh * wh) // 2 * 2
            sx, sy = ox + int(fw * wx), oy + int(fh * wy)
            # the show fills the window (trimmed a little if the shapes differ); the black behind it is
            # made from the show's own frames, so it ends when the show does
            graph.append(f"[p2]scale={sw}:{shh}:force_original_aspect_ratio=increase,crop={sw}:{shh}[sq]")
            graph.append(f"[p3]{pad_tail},drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill[bg]")
            graph.append(f"[bg][sq]overlay=x={sx}:y={sy}[bs]")
            graph.append(f"[bs][tk]overlay=0:0:eof_action=pass[sqc]")
            graph.append(f"[full][sqc]overlay=0:0:{on}[tp]")
        else:
            graph.append(f"[full][tk]overlay=0:0:eof_action=pass:{on}[tp]")
        cur = "tp"
        if tpl.get("title_png"):
            args += ["-loop", "1", "-framerate", "5", "-i", "file:" + tpl["title_png"]]
            tx, ty = ox + int(fw * tpl["title_x"]), oy + int(fh * tpl["title_y"])
            graph.append(f"[{cur}][{n}:v:0]overlay=x={tx}:y={ty}:eof_action=repeat:{on}[tt]")
            n, cur = n + 1, "tt"
        return n, cur

    def slate_cmd(self, text_file: str | None, duration: float, s: dict, burst: float) -> list[str]:
        hw = self.encoder_status.get("using") or "none"
        W, H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])
        args = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *self._hw_prefix(s, hw)]
        args += [*self._input_pacing(burst), "-f", "lavfi", "-i", f"color=c=0x0f172a:s={W}x{H}:r={FPS}"]
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        vf = ""
        if text_file and self.font and "drawtext" in self.filters:
            vf = (f"drawtext=fontfile={filter_escape(self.font)}:textfile={filter_escape(text_file)}:"
                  f"expansion=none:fontcolor=white:fontsize={H // 16}:line_spacing={H // 40}:"
                  f"x=(w-text_w)/2:y=(h-text_h)/2,")
        args += ["-map", "0:v:0", "-map", "1:a:0", "-vf", vf + self._vf_tail(hw), *self._video_codec(s, hw),
                 "-c:a", "aac", "-b:a", f"{int(s.get('audio_kbps') or 192)}k", "-ac", "2", "-ar", "48000",
                 *self._ts_out(duration)]
        return args

    def joiner_cmd(self, list_url: str, service_name: str, ts_offset: float = 0.0) -> list[str]:
        # ts_offset keeps timestamps moving forward if a channel's joiner ever has to restart.
        offset = ["-output_ts_offset", f"{ts_offset:.3f}"] if ts_offset > 0 else []
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            # Every piece is in the same known format, so there's no need to study the first one for
            # seconds before starting; this gets the picture to the player sooner.
            "-probesize", "500000", "-analyzeduration", "500000",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,http,tcp",
            "-i", list_url,
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", *offset,
            "-metadata", "service_provider=Rerun", "-metadata", f"service_name={service_name}",
            "-mpegts_pmt_start_pid", "4096", "-mpegts_start_pid", "256",  # streaming.py looks for these
            "-f", "mpegts", "pipe:1",
        ]

    def blank_cmd(self, duration: float, s: dict) -> list[str]:
        """A plain dark screen with silence: the last-resort piece when nothing else will play."""
        return self.slate_cmd(None, duration, s, 0)
