"""
PodTidy — Podcast Audio Organizer Engine
=========================================
Core business logic: podcast detection, title/track extraction,
ID3 tag formatting, filename formatting, ReplayGain normalization.
"""

import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# FFmpeg path (via imageio-ffmpeg)
# ---------------------------------------------------------------------------
try:
    import imageio_ffmpeg
    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG_EXE = "ffmpeg"  # fallback to PATH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PODCAST_NAMES = [
    "得体广播站",
    "罗永浩的十字路口",
    "三个火呛手",
    "谐星聊天会",
    "正经叭叭",
]

# Podcasts that extract track number from the filename
TYPE_A_PODCASTS = {"得体广播站", "三个火呛手", "正经叭叭"}

# Podcasts that get track number from directory (max + 1)
TYPE_B_PODCASTS = {"罗永浩的十字路口", "谐星聊天会"}

# Podcast with special title handling (preserve number prefix)
XIE_XING = "谐星聊天会"

# Filename-pattern → podcast mapping (used when ID3 tags are missing/garbled)
FILENAME_TO_PODCAST = [
    ("进击的思宇", "三个火呛手"),
    ("王三_333", "得体广播站"),
    ("罗永浩的十字路口", "罗永浩的十字路口"),
    ("谐星聊天会", "谐星聊天会"),
    ("正经叭叭", "正经叭叭"),
]

# Regex for episode-number prefix in filenames
# Matches: Vol.147, vol.86, Ep40., ep 12, #05, VOL 3 etc.
_RE_EPISODE_PREFIX = re.compile(
    r"^(?:Vol\.?|vol\.?|VOL\.?|Ep\.?|ep\.?|EP\.?|#)\s*(\d+)[\.\s]*(.*)",
    re.DOTALL,
)

# Specific regex for 谐星聊天会 to extract "vol.86" → "86. "
_RE_XIEXING_PREFIX = re.compile(
    r"^(?:vol\.?|Vol\.?|VOL\.?)\s*(\d+)[\.\s]*(.*)",
    re.DOTALL,
)

# Supported audio extensions
SUPPORTED_EXTS = {".mp3", ".m4a", ".flac", ".wma", ".aac", ".ogg", ".wav"}


# ---------------------------------------------------------------------------
# Helper: safe filename
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove characters that are illegal in Windows filenames."""
    illegal = r'[<>:"/\\|?*]'
    sanitized = re.sub(illegal, "_", name)
    # Also strip leading/trailing spaces and dots
    sanitized = sanitized.strip(" .")
    return sanitized


# ---------------------------------------------------------------------------
# Podcast Detection
# ---------------------------------------------------------------------------

def detect_podcast(filepath: str) -> str | None:
    """
    Determine which podcast an audio file belongs to.

    Priority:
      1. Read ``artist`` / ``album`` ID3 tags and match against known names.
      2. Fall back to filename keyword matching.
    """
    # --- 1. Try ID3 tags ---
    try:
        from mutagen import File
        audio = File(filepath, easy=True)
        if audio is not None:
            # Check artist
            artist = (audio.get("artist") or [None])[0]
            if artist:
                for name in PODCAST_NAMES:
                    if name in str(artist):
                        return name
            # Check album
            album = (audio.get("album") or [None])[0]
            if album:
                for name in PODCAST_NAMES:
                    if name in str(album):
                        return name
    except Exception:
        pass

    # --- 2. Fallback: filename matching ---
    basename = os.path.basename(filepath)
    for pattern, podcast_name in FILENAME_TO_PODCAST:
        if pattern in basename:
            return podcast_name

    return None


# ---------------------------------------------------------------------------
# Title & Track Extraction
# ---------------------------------------------------------------------------

def extract_title_and_track(
    filename_stem: str, podcast_name: str
) -> tuple[str, int | None, str | None]:
    """
    Parse title and track number from a filename stem.

    ``filename_stem`` is the filename without the speaker prefix and
    without the extension — i.e. everything after ``" - "``.

    Returns ``(title, track_number, error_message)``.
    ``track_number`` is ``None`` for TYPE_B podcasts (assigned later).
    ``error_message`` is ``None`` on success, or a string describing the
    problem (only raised for TYPE_A podcasts when no number is found).
    """
    stem = filename_stem.strip()

    # ------------------------------------------------------------------
    # 谐星聊天会 — preserve number in "数字. " format, track via dir
    # ------------------------------------------------------------------
    if podcast_name == XIE_XING:
        m = _RE_XIEXING_PREFIX.match(stem)
        if m:
            num = m.group(1)
            rest = m.group(2).strip()
            title = f"{num}. {rest}"
        else:
            # No vol. prefix — keep original
            title = stem
        return (title, None, None)  # track assigned later (TYPE_B)

    # ------------------------------------------------------------------
    # Other TYPE_A podcasts: 得体, 三个火呛手, 正经叭叭
    # ------------------------------------------------------------------
    if podcast_name in TYPE_A_PODCASTS:
        m = _RE_EPISODE_PREFIX.match(stem)
        if m:
            track_num = int(m.group(1))
            title = m.group(2).strip()
            return (title, track_num, None)
        else:
            # No episode number found → error
            return (
                stem,
                None,
                f"无法从文件名中提取音轨号: 「{filename_stem}」\n"
                f"播客「{podcast_name}」需要文件名包含序号（如 Vol.147、Ep40）",
            )

    # ------------------------------------------------------------------
    # TYPE_B: 罗永浩的十字路口
    # ------------------------------------------------------------------
    if podcast_name in TYPE_B_PODCASTS:
        m = _RE_EPISODE_PREFIX.match(stem)
        if m:
            # Strip the episode prefix from the title
            title = m.group(2).strip()
        else:
            title = stem
        return (title, None, None)  # track assigned later (TYPE_B)

    # Fallback (shouldn't reach here)
    return (stem, None, None)


def extract_speaker_prefix(filename_stem: str) -> str:
    """
    Remove the speaker/author prefix from a filename.

    E.g. ``"进击的思宇 - Vol.147 ..."`` → ``"Vol.147 ..."``

    Returns the part after ``" - "``, or the original if no separator found.
    """
    # Try common separators: " - ", "_-_", " – "
    for sep in (" - ", " – ", "_-_"):
        if sep in filename_stem:
            parts = filename_stem.split(sep, 1)
            return parts[1].strip()
    return filename_stem


# ---------------------------------------------------------------------------
# Track Number Assignment (TYPE_B)
# ---------------------------------------------------------------------------

def get_max_track_in_dir(podcast_dir: str) -> int:
    """
    Scan all MP3 files in *podcast_dir* and return the highest track
    number found in ID3 tags. Returns 0 if no files / no track tags.
    """
    max_track = 0
    if not os.path.isdir(podcast_dir):
        return 0

    try:
        from mutagen import File
        for entry in os.scandir(podcast_dir):
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue
            try:
                audio = File(entry.path, easy=True)
                if audio is None:
                    continue
                tn = audio.get("tracknumber")
                if tn and tn[0]:
                    # tracknumber can be "3" or "3/10"
                    num_str = str(tn[0]).split("/")[0].strip()
                    if num_str.isdigit():
                        max_track = max(max_track, int(num_str))
            except Exception:
                pass
    except Exception:
        pass

    return max_track


def assign_track_numbers(
    podcast_dir: str, files: list[str]
) -> dict[str, int]:
    """
    Assign track numbers to a batch of files (all belonging to the same
    TYPE_B podcast).  Files are sorted by creation time (oldest first)
    and assigned ``max_existing + 1``, ``max_existing + 2``, …

    Returns a mapping: ``{filepath: track_number}``
    """
    # Ensure directory exists
    os.makedirs(podcast_dir, exist_ok=True)

    base = get_max_track_in_dir(podcast_dir)

    # Sort by creation time (oldest first)
    def _ctime(p: str) -> float:
        try:
            return os.path.getctime(p)
        except Exception:
            return 0.0

    sorted_files = sorted(files, key=_ctime)

    assignments = {}
    for i, f in enumerate(sorted_files, start=1):
        assignments[f] = base + i

    return assignments


# ---------------------------------------------------------------------------
# ID3 Tag Formatting
# ---------------------------------------------------------------------------

def format_id3_tags(
    filepath: str,
    podcast_name: str,
    title: str,
    track_num: int | None,
    podcast_dir: str,
) -> None:
    """
    Rewrite ID3 tags on *filepath*:

    - Deletes **all** existing tags.
    - Writes: artist, album, title, tracknumber.
    - Replaces album art with ``folder.jpg`` (or ``folder.png``) from
      *podcast_dir*.
    """
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, TXXX
    from mutagen.mp3 import MP3

    # Detect file type and choose appropriate approach
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".mp3":
        _format_mp3_tags(filepath, podcast_name, title, track_num, podcast_dir)
    else:
        # Use EasyID3/ mutagen's generic approach for other formats
        _format_generic_tags(filepath, podcast_name, title, track_num, podcast_dir)


def _format_mp3_tags(
    filepath: str, podcast_name: str, title: str,
    track_num: int | None, podcast_dir: str,
) -> None:
    """Format ID3v2 tags on an MP3 file."""
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK

    # ---- Delete all existing tags ----
    try:
        audio = ID3(filepath)
        audio.delete()
        audio.save()
    except Exception:
        pass  # File may not have any ID3 tags yet

    # ---- Write fresh tags ----
    audio = ID3()
    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=podcast_name))
    audio.add(TALB(encoding=3, text=podcast_name))
    if track_num is not None:
        audio.add(TRCK(encoding=3, text=str(track_num)))

    # ---- Album art ----
    art_data = _load_album_art(podcast_dir)
    if art_data is not None:
        mime_type = _get_art_mime(podcast_dir)
        audio.add(
            APIC(
                encoding=3,
                mime=mime_type,
                type=3,  # Cover (front)
                desc="Cover",
                data=art_data,
            )
        )

    audio.save(filepath, v2_version=3)


def _format_generic_tags(
    filepath: str, podcast_name: str, title: str,
    track_num: int | None, podcast_dir: str,
) -> None:
    """Format tags on non-MP3 files using mutagen's generic API."""
    from mutagen import File
    from mutagen.id3 import ID3, APIC

    audio = File(filepath, easy=True)
    if audio is None:
        return

    # Delete all existing tags
    audio.delete()

    # Write new tags
    audio["artist"] = podcast_name
    audio["album"] = podcast_name
    audio["title"] = title
    if track_num is not None:
        audio["tracknumber"] = str(track_num)

    audio.save()

    # For album art on non-MP3, we need format-specific handling.
    # Only attempt on MP4/M4A files.
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".m4a", ".mp4", ".m4b"):
        _format_mp4_art(filepath, podcast_dir)


def _format_mp4_art(filepath: str, podcast_dir: str) -> None:
    """Add album art to an MP4/M4A file."""
    from mutagen.mp4 import MP4, MP4Cover

    art_data = _load_album_art(podcast_dir)
    if art_data is None:
        return

    try:
        audio = MP4(filepath)
        cover = MP4Cover(art_data, imageformat=MP4Cover.FORMAT_JPEG)
        audio["covr"] = [cover]
        audio.save()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Album Art Helpers
# ---------------------------------------------------------------------------

def _load_album_art(podcast_dir: str) -> bytes | None:
    """
    Load album art from folder.jpg or folder.png in *podcast_dir*.
    PNG files are converted to JPEG in memory.
    Returns raw JPEG bytes, or None if no art file found.
    """
    for fname in ("folder.jpg", "folder.jpeg", "folder.png"):
        art_path = os.path.join(podcast_dir, fname)
        if not os.path.isfile(art_path):
            continue

        ext = os.path.splitext(fname)[1].lower()
        if ext in (".jpg", ".jpeg"):
            with open(art_path, "rb") as f:
                return f.read()
        elif ext == ".png":
            # Convert PNG → JPEG
            try:
                from PIL import Image
                import io
                img = Image.open(art_path).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                return buf.getvalue()
            except Exception:
                # Fallback: return raw PNG (some players support it)
                with open(art_path, "rb") as f:
                    return f.read()

    return None


def _get_art_mime(podcast_dir: str) -> str:
    """Return the MIME type of the album art file."""
    for fname in ("folder.jpg", "folder.jpeg"):
        if os.path.isfile(os.path.join(podcast_dir, fname)):
            return "image/jpeg"
    if os.path.isfile(os.path.join(podcast_dir, "folder.png")):
        return "image/png"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# ReplayGain via ffmpeg
# ---------------------------------------------------------------------------

def apply_replaygain(filepath: str) -> bool:
    """
    Scan *filepath* with ffmpeg's ``replaygain`` filter and write
    ReplayGain 2.0 tags (``replaygain_track_gain``,
    ``replaygain_track_peak``) via mutagen.

    Returns ``True`` on success, ``False`` on failure (non-fatal).
    """
    # ---- Step 1: ffmpeg scan ----
    cmd = [
        _FFMPEG_EXE,
        "-i", filepath,
        "-af", "replaygain",
        "-f", "null",
        "-",
    ]
    try:
        # Use bytes mode + manual decode to avoid GBK encoding errors on
        # Chinese Windows — ffmpeg stderr mixes UTF-8 and raw binary.
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        stderr_output = result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return False

    if not stderr_output:
        return False

    # ---- Step 2: Parse gain & peak ----
    gain_match = re.search(
        r"track_gain\s*=\s*([+-]?\d+\.?\d*)\s*dB", stderr_output
    )
    peak_match = re.search(
        r"track_peak\s*=\s*([\d.]+)", stderr_output
    )

    if not gain_match or not peak_match:
        return False

    gain_val = gain_match.group(1)
    peak_val = peak_match.group(1)
    gain_text = f"{gain_val} dB"

    # ---- Step 3: Write ID3 TXXX tags ----
    try:
        from mutagen.id3 import ID3, TXXX

        audio = ID3(filepath)
        # Remove any existing ReplayGain tags
        to_delete = []
        for key, frame in audio.items():
            if key.startswith("TXXX"):
                desc = getattr(frame, "desc", "")
                if "replaygain" in desc.lower():
                    to_delete.append(key)
        for key in to_delete:
            del audio[key]

        audio.add(TXXX(encoding=3, desc="replaygain_track_gain", text=gain_text))
        audio.add(TXXX(encoding=3, desc="replaygain_track_peak", text=peak_val))
        audio.save()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Podcast Engine (threaded workflow)
# ---------------------------------------------------------------------------

class PodcastEngine:
    """
    Background engine that processes audio files:

    1. Detect podcast for each file
    2. Group files by podcast
    3. Pre-assign track numbers for TYPE_B podcasts
    4. For each file: extract title, format tags, apply ReplayGain,
       move to target directory, rename
    5. Report progress + final result
    """

    def __init__(
        self,
        progress_callback,
        log_callback,
        complete_callback,
    ):
        """
        All callbacks are called from the worker thread — the GUI layer
        must use ``.after(0, …)`` to marshal them to the main thread.

        :param progress_callback: ``fn(message: str, percent: int, is_error: bool)``
        :param log_callback:      ``fn(message: str)``
        :param complete_callback: ``fn(success: bool, message: str, results: list[dict])``
        """
        self._progress_cb = progress_callback
        self._log_cb = log_callback
        self._complete_cb = complete_callback
        self._cancel_flag = threading.Event()
        self._thread = None

    # ---- Public API ----

    def start(self, files: list[str], podcast_root: str) -> None:
        """Launch processing in a daemon thread."""
        if self.is_running:
            return
        self._cancel_flag.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(files, podcast_root),
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Signal cancellation (processing stops at the next checkpoint)."""
        self._cancel_flag.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- Internal ----

    def _report(self, message: str, percent: int = -1, is_error: bool = False) -> None:
        """Thread-safe progress report."""
        try:
            self._progress_cb(message, percent, is_error)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        try:
            self._log_cb(message)
        except Exception:
            pass

    def _cancelled(self) -> bool:
        return self._cancel_flag.is_set()

    def _run(self, files: list[str], podcast_root: str) -> None:
        results = []
        errors = []
        total = len(files)

        self._report(f"准备处理 {total} 个文件…", 0)

        # ---- Pass 1: Group files by podcast ----
        groups: dict[str, list[str]] = {}
        unrecognized: list[str] = []

        for f in files:
            if self._cancelled():
                self._complete(False, "已取消", [])
                return

            podcast = detect_podcast(f)
            if podcast is None:
                unrecognized.append(f)
            else:
                groups.setdefault(podcast, []).append(f)

        if unrecognized:
            names = "\n".join(
                f"  - {os.path.basename(p)}" for p in unrecognized
            )
            errors.append(f"以下文件无法识别播客归属:\n{names}")

        if not groups:
            self._complete(
                False,
                f"没有可处理的文件。\n{chr(10).join(errors)}" if errors
                else "没有可处理的文件。",
                [],
            )
            return

        # ---- Pass 2: Pre-assign TYPE_B track numbers ----
        track_map: dict[str, int] = {}
        for podcast_name, podcast_files in groups.items():
            if podcast_name in TYPE_B_PODCASTS:
                podcast_dir = os.path.join(podcast_root, podcast_name)
                assignments = assign_track_numbers(podcast_dir, podcast_files)
                track_map.update(assignments)

        # ---- Pass 3: Process each file ----
        processed_count = 0

        for podcast_name, podcast_files in groups.items():
            podcast_dir = os.path.join(podcast_root, podcast_name)
            os.makedirs(podcast_dir, exist_ok=True)

            for i, filepath in enumerate(podcast_files):
                if self._cancelled():
                    self._complete(False, "已取消", results)
                    return

                basename = os.path.basename(filepath)
                stem, ext = os.path.splitext(basename)
                ext = ext.lower()

                self._report(f"正在处理第 {processed_count + 1}/{total} 个文件...",
                             int(processed_count / max(total, 1) * 100))

                try:
                    # -- 3a. Detect podcast (already done, but double-check) --
                    podcast = detect_podcast(filepath)
                    if podcast is None:
                        errors.append(f"「{basename}」: 无法识别播客归属")
                        continue

                    # -- 3b. Extract speaker prefix for title parsing --
                    content_stem = extract_speaker_prefix(stem)

                    # -- 3c. Extract title & track --
                    title, track_num, err_msg = extract_title_and_track(
                        content_stem, podcast
                    )

                    if err_msg is not None:
                        errors.append(f"「{basename}」: {err_msg}")
                        continue

                    # -- 3d. Assign track number for TYPE_B --
                    if podcast in TYPE_B_PODCASTS:
                        track_num = track_map.get(filepath)
                        if track_num is None:
                            errors.append(
                                f"「{basename}」: 无法分配音轨号"
                            )
                            continue

                    self._log(f"  → 播客: {podcast} | 标题: {title} | "
                              f"音轨号: {track_num}")

                    # -- 3e. Format ID3 tags --
                    format_id3_tags(filepath, podcast, title, track_num, podcast_dir)
                    self._log(f"  → 标签已格式化")

                    # -- 3f. Apply ReplayGain --
                    self._report(f"扫描增益 ({processed_count + 1}/{total})...",
                                 int(processed_count / max(total, 1) * 100))
                    rg_ok = apply_replaygain(filepath)
                    if rg_ok:
                        self._log(f"  → ReplayGain 已写入")
                    else:
                        self._log(f"  → [!] ReplayGain 扫描失败 (文件可能已损坏)")

                    # -- 3g. Build new filename & move --
                    new_filename = _sanitize_filename(f"{podcast} - {title}{ext}")
                    dest_path = os.path.join(podcast_dir, new_filename)

                    # Avoid overwriting; append (2), (3) if needed
                    dest_path = _unique_path(dest_path)

                    shutil.move(filepath, dest_path)
                    self._log(f"  → 已移动至: {os.path.basename(dest_path)}")

                    results.append({
                        "original": basename,
                        "podcast": podcast,
                        "title": title,
                        "track": track_num,
                        "dest": dest_path,
                        "replaygain": rg_ok,
                    })
                    processed_count += 1

                except Exception as exc:
                    errors.append(f"「{basename}」: 处理失败 — {exc}")
                    self._log(f"  → [ERROR] {exc}")

        # ---- Done ----
        message_parts = [f"成功处理 {processed_count} 个文件"]
        if errors:
            message_parts.append(f"{len(errors)} 个错误:")
            message_parts.extend(errors)

        self._report("完成!", 100)
        self._complete(
            len(errors) == 0,
            "\n".join(message_parts),
            results,
        )

    def _complete(
        self, success: bool, message: str, results: list[dict]
    ) -> None:
        """Signal completion back to the GUI."""
        try:
            self._complete_cb(success, message, results)
        except Exception:
            pass


def _unique_path(filepath: str) -> str:
    """If *filepath* exists, append `` (2)``, `` (3)``, … before the extension."""
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    counter = 2
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1
