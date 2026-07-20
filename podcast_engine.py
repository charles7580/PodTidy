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
    "科技参考5",
    "贤鱼播客集",
]

# Podcasts that extract track number from the filename
TYPE_A_PODCASTS = {"得体广播站", "三个火呛手", "正经叭叭", "科技参考5"}

# Podcasts that get track number from directory (max + 1)
TYPE_B_PODCASTS = {"罗永浩的十字路口", "谐星聊天会", "贤鱼播客集"}

# Podcast with special title handling (preserve number prefix)
XIE_XING = "谐星聊天会"

# Podcast with "zk-0625丨" prefix stripping + "丨" removal
KEJI_CANKAO = "科技参考5"

# Podcasts whose ID3 artist tag differs from the podcast name
# (default: artist = podcast_name)
PODCAST_ARTIST_MAP = {
    "科技参考5": "卓克",
}


def get_artist_name(podcast_name: str) -> str:
    """Return the artist tag value for a podcast (defaults to podcast name)."""
    return PODCAST_ARTIST_MAP.get(podcast_name, podcast_name)

# Regex for stripping the "zk-MMDD丨" date-code prefix from 科技参考5 filenames
_RE_KEJI_PREFIX = re.compile(
    r"^zk-\d+\s*[丨｜]\s*",
    re.IGNORECASE,
)

# Filename-pattern → podcast mapping (used when ID3 tags are missing/garbled)
FILENAME_TO_PODCAST = [
    ("进击的思宇", "三个火呛手"),
    ("王三_333", "得体广播站"),
    ("罗永浩的十字路口", "罗永浩的十字路口"),
    ("谐星聊天会", "谐星聊天会"),
    ("正经叭叭", "正经叭叭"),
    ("漫喜利工作室", "贤鱼播客集"),
    ("科技参考", "科技参考5"),
    ("zk-", "科技参考5"),
    ("ZK-", "科技参考5"),
]

# Regex for episode-number prefix in filenames
# Matches: Vol.147, vol.86, Ep40., ep 12, #05, VOL 3 etc.
_RE_EPISODE_PREFIX = re.compile(
    r"^(?:Vol\.?|vol\.?|VOL\.?|Ep\.?|ep\.?|EP\.?|#)\s*(\d+)[\.\s]*(.*)",
    re.DOTALL,
)

# Fallback for bare track numbers: "259 - vol.259标题", "001 标题", etc.
_RE_BARE_TRACK = re.compile(
    r"^(\d{1,4})[\.\s\-\|｜]+(.*)",
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

# Regex to strip leading track-number prefixes from tag titles:
#   "85. 标题"      → "标题"
#   "064 标题"      → "标题"
#   "001 - 标题"    → "标题"
#   "Vol.147 标题"  → "标题"
#   "ep 12 标题"    → "标题"
_RE_TAG_TITLE_TRACK = re.compile(
    r"^(?:(?:Vol\.?|vol\.?|VOL\.?|Ep\.?|ep\.?|EP\.?|#)\s*\d{1,4}[\.\s\-\|｜]*"
    r"|\d{1,4}[\.\s\-\|｜]+)",
)


def _strip_title_track_number(title: str) -> str:
    """Remove a leading track number (e.g. ``85. ``) from *title*."""
    return _RE_TAG_TITLE_TRACK.sub("", title).strip()


def _read_tags(filepath: str) -> tuple[str | None, int | None]:
    """
    Read ``(title, track_number)`` from the ID3 tags of *filepath*.
    Returns ``(None, None)`` when tags are missing or unreadable.
    """
    try:
        from mutagen import File
        audio = File(filepath, easy=True)
        if audio is None:
            return None, None

        title: str | None = None
        t = audio.get("title")
        if t and t[0] and t[0].strip():
            title = t[0].strip()

        track_num: int | None = None
        tn = audio.get("tracknumber")
        if tn and tn[0]:
            num_str = str(tn[0]).split("/")[0].strip()
            if num_str.isdigit():
                track_num = int(num_str)

        return title, track_num
    except Exception:
        return None, None


def extract_title_and_track(
    filename_stem: str, podcast_name: str, filepath: str | None = None
) -> tuple[str, int | None, str | None]:
    """
    Parse title and track number from ID3 tags first, then fall back
    to filename parsing.

    ``filename_stem`` is the filename without the speaker prefix and
    without the extension — i.e. everything after ``" - "``.

    When *filepath* is provided, ID3 tags are read first; if both
    ``title`` and ``tracknumber`` are present in the tags, they are
    used directly.  Otherwise the function falls through to the
    existing filename-based logic, merging any partial tag data.

    Returns ``(title, track_number, error_message)``.
    ``track_number`` is ``None`` for TYPE_B podcasts (assigned later).
    ``error_message`` is ``None`` on success, or a string describing the
    problem (only raised for TYPE_A podcasts when no number is found).
    """
    stem = filename_stem.strip()

    # ------------------------------------------------------------------
    # Stage 0 — try ID3 tags first (for re-processing already-tagged files)
    # ------------------------------------------------------------------
    tag_title, tag_track = None, None
    if filepath:
        tag_title, tag_track = _read_tags(filepath)
        # Strip leading track-number prefixes from tag titles
        # e.g. "85. 标题" → "标题",  "064标题" → "标题"
        if tag_title:
            tag_title = _strip_title_track_number(tag_title)
        if tag_title and tag_track is not None:
            # Tags provide everything we need — skip filename parsing
            return (tag_title, tag_track, None)

    # ------------------------------------------------------------------
    # Stage 1 — filename-based extraction (compute; don't return yet)
    # ------------------------------------------------------------------
    fn_title: str = stem
    fn_track: int | None = None
    fn_err: str | None = None

    # ------------------------------------------------------------------
    # 谐星聊天会 — preserve number in "数字. " format, track via dir
    # ------------------------------------------------------------------
    if podcast_name == XIE_XING:
        m = _RE_XIEXING_PREFIX.match(stem)
        if m:
            num = m.group(1)
            rest = m.group(2).strip()
            fn_title = f"{num}. {rest}"
        else:
            fn_title = stem
        # track stays None (assigned later for TYPE_B); no error

    # ------------------------------------------------------------------
    # 科技参考5 — strip "zk-MMDD丨" prefix + "丨" separators
    # ------------------------------------------------------------------
    elif podcast_name == KEJI_CANKAO:
        cleaned = _RE_KEJI_PREFIX.sub("", stem)
        cleaned = cleaned.replace("丨", "").replace("｜", "")
        cleaned = cleaned.strip()
        m = re.match(r"^0*(\d+)\s*(.*)", cleaned)
        if m:
            fn_track = int(m.group(1))
            fn_title = m.group(2).strip()
        else:
            fn_err = (
                f"无法从文件名中提取音轨号: 「{filename_stem}」\n"
                f"播客「{podcast_name}」需要文件名包含音轨号（如 zk-0625丨064丨标题）"
            )

    # ------------------------------------------------------------------
    # Other TYPE_A podcasts: 得体, 三个火呛手, 正经叭叭
    # ------------------------------------------------------------------
    elif podcast_name in TYPE_A_PODCASTS:
        m = _RE_EPISODE_PREFIX.match(stem)
        if not m:
            m = _RE_BARE_TRACK.match(stem)  # fallback: "259 - vol.259标题"
        if m:
            fn_track = int(m.group(1))
            fn_title = m.group(2).strip()
            # Strip any residual track-number prefix from the title
            # (e.g. "vol.259装忙…" → "装忙…")
            fn_title = _strip_title_track_number(fn_title)
        else:
            fn_err = (
                f"无法从文件名中提取音轨号: 「{filename_stem}」\n"
                f"播客「{podcast_name}」需要文件名包含序号（如 Vol.147、Ep40）"
            )

    # ------------------------------------------------------------------
    # TYPE_B: 罗永浩的十字路口
    # ------------------------------------------------------------------
    elif podcast_name in TYPE_B_PODCASTS:
        m = _RE_EPISODE_PREFIX.match(stem)
        if not m:
            m = _RE_BARE_TRACK.match(stem)  # fallback: bare track number
        if m:
            fn_title = m.group(2).strip()
            fn_title = _strip_title_track_number(fn_title)
        else:
            fn_title = stem
        # track stays None (assigned later for TYPE_B); no error

    # ------------------------------------------------------------------
    # Stage 2 — merge: tag values preferred, filename values fill gaps
    # ------------------------------------------------------------------
    title = tag_title if tag_title else fn_title
    track_num = tag_track if tag_track is not None else fn_track

    # ------------------------------------------------------------------
    # Stage 3 — validate TYPE_A track number
    # ------------------------------------------------------------------
    if podcast_name in TYPE_A_PODCASTS and track_num is None:
        # Try reading existing tracknumber tag as last resort
        if filepath:
            existing = _read_existing_track(filepath)
            if existing is not None:
                track_num = existing
                fn_err = None  # recovered — clear the filename error
        if track_num is None and fn_err is not None:
            return (title, None, fn_err)

    return (title, track_num, None)


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
# Track Number Helpers
# ---------------------------------------------------------------------------

def _read_existing_track(filepath: str) -> int | None:
    """
    Read the existing tracknumber tag from *filepath* as a fallback.
    Returns the integer track number, or None if not found.
    """
    try:
        from mutagen import File
        audio = File(filepath, easy=True)
        if audio is None:
            return None
        tn = audio.get("tracknumber")
        if tn and tn[0]:
            num_str = str(tn[0]).split("/")[0].strip()
            if num_str.isdigit():
                return int(num_str)
    except Exception:
        pass
    return None


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
    """Format ID3v2 tags on an MP3 file — single-pass clear + rewrite."""
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK

    # ---- Open file, clear all frames, add new ones, save once ----
    try:
        audio = ID3(filepath)
    except Exception:
        audio = ID3()

    audio.delete()  # wipe all existing frames (including old APIC)

    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=get_artist_name(podcast_name)))
    audio.add(TALB(encoding=3, text=podcast_name))
    if track_num is not None:
        audio.add(TRCK(encoding=3, text=str(track_num)))

    # ---- Album art — embed original file bytes without conversion ----
    art_data, art_mime = _load_album_art_raw(podcast_dir)
    if art_data is not None:
        audio.add(
            APIC(
                encoding=3,
                mime=art_mime,
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
    audio["artist"] = get_artist_name(podcast_name)
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

def _load_album_art_raw(podcast_dir: str) -> tuple[bytes | None, str | None]:
    """
    Load album art from folder.jpg / folder.png in *podcast_dir*.
    Returns (raw_bytes, mime_type) — NO format conversion.
    Returns (None, None) if no art file found.
    """
    for fname, mime in (
        ("folder.jpg", "image/jpeg"),
        ("folder.jpeg", "image/jpeg"),
        ("folder.png", "image/png"),
    ):
        art_path = os.path.join(podcast_dir, fname)
        if os.path.isfile(art_path):
            with open(art_path, "rb") as f:
                return f.read(), mime
    return None, None


def _load_album_art(podcast_dir: str) -> bytes | None:
    """Legacy wrapper — returns raw bytes (any format)."""
    data, _ = _load_album_art_raw(podcast_dir)
    return data


# ---------------------------------------------------------------------------
# ReplayGain via ffmpeg (EBU R128)
# ---------------------------------------------------------------------------

# Target loudness level in LUFS for ReplayGain calculation.
# EBU R128 / ReplayGain 2.0 targets -18 LUFS, matching foobar2000's default.
# Adjust this if you prefer a different reference level.
_REPLAYGAIN_TARGET_LUFS = -18.0


def _has_replaygain(filepath: str) -> bool:
    """
    Return ``True`` if *filepath* already has both
    ``replaygain_track_gain`` and ``replaygain_track_peak`` TXXX tags.
    """
    try:
        from mutagen.id3 import ID3
        audio = ID3(filepath)
        has_gain = False
        has_peak = False
        for _key, frame in audio.items():
            if hasattr(frame, "desc"):
                d = getattr(frame, "desc", "")
                if d.lower() == "replaygain_track_gain":
                    has_gain = True
                elif d.lower() == "replaygain_track_peak":
                    has_peak = True
        return has_gain and has_peak
    except Exception:
        return False


def _read_replaygain_values(filepath: str) -> tuple[str, str] | None:
    """
    Read existing ReplayGain ``(gain_text, peak_val)`` from *filepath*.
    Returns ``None`` when tags are missing or unreadable.
    """
    try:
        from mutagen.id3 import ID3
        audio = ID3(filepath)
        gain_text = None
        peak_val = None
        for _key, frame in audio.items():
            if hasattr(frame, "desc"):
                d = getattr(frame, "desc", "")
                if d.lower() == "replaygain_track_gain":
                    if hasattr(frame, "text") and frame.text:
                        gain_text = str(frame.text[0])
                elif d.lower() == "replaygain_track_peak":
                    if hasattr(frame, "text") and frame.text:
                        peak_val = str(frame.text[0])
        if gain_text and peak_val:
            return (gain_text, peak_val)
        return None
    except Exception:
        return None


def _write_replaygain_values(filepath: str, gain_text: str, peak_val: str) -> bool:
    """Write ReplayGain TXXX tags to *filepath*. Returns ``True`` on success."""
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


def apply_replaygain(filepath: str) -> bool:
    """
    Scan *filepath* with ffmpeg's ``ebur128`` filter (EBU R128 standard,
    the same loudness measurement used by foobar2000) and write
    ReplayGain 2.0 tags (``replaygain_track_gain``,
    ``replaygain_track_peak``) via mutagen.

    Note: the caller is responsible for checking ``_has_replaygain``
    beforehand if skipping already-tagged files is desired.

    Returns ``True`` on success, ``False`` on failure (non-fatal).
    """
    # ---- Step 1: ffmpeg scan with EBU R128 ----
    cmd = [
        _FFMPEG_EXE,
        "-i", filepath,
        "-af", "ebur128=peak=true",
        "-f", "null",
        "-",
    ]
    try:
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

    # ---- Step 2: Parse integrated loudness & true peak ----
    # The ebur128 filter outputs per-frame log lines that also contain
    # "I:" / "Peak:" markers.  Only parse the final Summary section:
    #   Summary:
    #   Integrated loudness:
    #   I:         -20.5 LUFS
    #   ...
    #   True peak:
    #   Peak:       -1.2 dBFS
    summary_idx = stderr_output.find("Summary:")
    if summary_idx < 0:
        return False

    summary_text = stderr_output[summary_idx:]

    il_match = re.search(
        r"I:\s+(-?\d+\.?\d*)\s*LUFS", summary_text
    )
    peak_match = re.search(
        r"Peak:\s+(-?\d+\.?\d*)\s*dBFS", summary_text
    )

    if not il_match or not peak_match:
        return False

    integrated_lufs = float(il_match.group(1))
    peak_dbfs = float(peak_match.group(1))

    # ---- Step 3: Compute gain to reach target LUFS ----
    # Positive gain = boost, negative gain = cut
    gain_db = _REPLAYGAIN_TARGET_LUFS - integrated_lufs
    gain_text = f"{gain_db:.2f} dB"

    # Convert peak from dBFS to linear (ReplayGain convention)
    peak_linear = 10 ** (peak_dbfs / 20)
    peak_val = f"{peak_linear:.6f}"

    # ---- Step 4: Write ID3 TXXX tags ----
    return _write_replaygain_values(filepath, gain_text, peak_val)


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

                    # -- 3c. Extract title & track (tags first, filename fallback) --
                    title, track_num, err_msg = extract_title_and_track(
                        content_stem, podcast, filepath
                    )

                    if err_msg is not None:
                        errors.append(f"「{basename}」: {err_msg}")
                        continue

                    # -- 3d. Assign track number for TYPE_B --
                    if podcast in TYPE_B_PODCASTS and track_num is None:
                        # Only use dir-increment if tags didn't provide a track
                        track_num = track_map.get(filepath)
                        if track_num is None:
                            errors.append(
                                f"「{basename}」: 无法分配音轨号"
                            )
                            continue

                    self._log(f"  → 播客: {podcast} | 标题: {title} | "
                              f"音轨号: {track_num}")

                    # -- 3e. Save ReplayGain BEFORE formatting (formatting wipes all tags) --
                    saved_rg = _read_replaygain_values(filepath)

                    # -- 3f. Format ID3 tags --
                    format_id3_tags(filepath, podcast, title, track_num, podcast_dir)
                    self._log(f"  → 标签已格式化")

                    # -- 3g. Apply / restore ReplayGain --
                    if saved_rg is not None:
                        # Restore saved values — no need to re-scan
                        rg_ok = _write_replaygain_values(
                            filepath, saved_rg[0], saved_rg[1]
                        )
                        self._log(f"  → ReplayGain 已保留 (无需重新扫描)")
                    else:
                        self._report(f"扫描增益 ({processed_count + 1}/{total})...",
                                     int(processed_count / max(total, 1) * 100))
                        rg_ok = apply_replaygain(filepath)
                        if rg_ok:
                            self._log(f"  → ReplayGain 已写入")
                        else:
                            self._log(f"  → [!] ReplayGain 扫描失败 (文件可能已损坏)")

                    # -- 3g. Build new filename & copy --
                    track_str = f"{track_num:03d}" if track_num is not None else "000"
                    new_filename = _sanitize_filename(
                        f"{podcast} - {track_str} - {title}{ext}"
                    )
                    dest_path = os.path.join(podcast_dir, new_filename)

                    # Avoid overwriting; append (2), (3) if needed
                    dest_path = _unique_path(dest_path)

                    shutil.copy2(filepath, dest_path)
                    self._log(f"  → 已复制至: {os.path.basename(dest_path)}")

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
