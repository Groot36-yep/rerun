"""Generate a tiny fake media library with ffmpeg, for testing without real files.

    python tests/make_media.py OUTPUT_DIR
"""
import os
import subprocess
import sys

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "testmedia")


def make(path, seconds, size="640x480", audio=True, color=None, chapters=None, interlaced=False, label=""):
    path = os.path.join(OUT, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    src = f"testsrc2=s={size}:r=24" if color is None else f"color=c={color}:s={size}:r=24"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    meta = None
    if chapters:
        meta = path + ".meta.txt"
        with open(meta, "w") as f:
            f.write(";FFMETADATA1\n")
            bounds = [0, *chapters, seconds]
            for a, b in zip(bounds, bounds[1:]):
                f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(a * 1000)}\nEND={int(b * 1000)}\n")
        args += ["-f", "ffmetadata", "-i", meta, "-map_metadata", str(2 if audio else 1)]
        args += ["-map", "0:v"] + (["-map", "1:a"] if audio else [])
    args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if interlaced:
        args += ["-flags", "+ildct+ilme", "-top", "1"]
    if audio:
        args += ["-c:a", "aac", "-ac", "2"]
    args.append(path)
    subprocess.run(args, check=True)
    if meta:
        os.remove(meta)


if __name__ == "__main__":
    # TV: two "Nickelodeon" cartoons, one "Cartoon Network" show, one sitcom
    for i in range(1, 4):
        make(f"tv/Sponge Show/S01E0{i}.mp4", 50, chapters=[18, 34])
        make(f"tv/Rugrats Test/S01E0{i}.mkv", 45, size="720x480", interlaced=True)
        make(f"tv/Dexter Test/S01E0{i}.mp4", 40, size="1280x720")
        make(f"tv/Sitcom Test/S01E0{i}.mp4", 55, size="1920x1080", audio=(i != 2))
    # Movies
    make("movies/Test Movie (1999).mp4", 70, size="1280x536")
    # Commercials, in era folders
    for i in range(1, 5):
        make(f"commercials/90s/Toy Ad {i}.mp4", 5 + i * 2, size="640x480", color=["red", "blue", "green", "orange"][i - 1])
    make("commercials/nick/Nick Bumper.mp4", 4, size="640x480", color="orange")
    # Music videos
    make("music/80s/Test Artist - First Song (1984).mp4", 25, size="640x480")
    make("music/80s/Other Band - Second Song.mp4", 20, size="640x480")
    make("music/90s/Third Act - Third Song (1996).mp4", 22, size="640x480")
    print(OUT)
