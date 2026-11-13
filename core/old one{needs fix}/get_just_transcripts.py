# --- START OF FILE get_just_transcripts_clean.py ---
# --- START COPYING HERE ---
import os
import time
import re
import sys
import csv # Keep for potential future use
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
import traceback
from tqdm import tqdm
import logging
from collections import Counter
import platform  # For system info logging
import concurrent.futures
import threading # For Locks
import math # For timestamp formatting

# --- Constants ---
RETRY_DELAY = 5
MAX_RETRIES = 3
DEFAULT_PLAYLIST_VIDEO_LIMIT = 500
CONSOLE_WIDTH = 90
SCRIPT_VERSION = "3.4.1" # Version update (PyInstaller Syntax Cleanup)
MAX_WORKERS = 10 # Number of parallel threads for fetching
YDL_SHARED_OPTS = {
    'quiet': True, 'skip_download': True, 'extract_flat': False,
    'force_generic_extractor': False, 'ignoreerrors': True,
    'socket_timeout': 20,
    'retries': 3, 'no_warnings': True,
    'logtostderr': False, 'source_address': '0.0.0.0'
}

# --- Logging Setup ---
LOG_FILENAME = "TranscriptExtractor_detailed.log"
LOG_FORMAT_FILE = '%(asctime)s - %(levelname)s - %(threadName)s - %(filename)s:%(lineno)d - %(message)s'
LOG_FORMAT_CONSOLE = '%(message)s'
logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT_CONSOLE, stream=sys.stdout) # Basic placeholder
logger = logging.getLogger(__name__)
logging_initialized = False

def setup_logging(log_dir):
    """Sets up file and console logging with different levels/formats."""
    global logger, logging_initialized
    if logging_initialized:
        # Avoid reconfiguring if already done
        if logger.handlers:
             # Assuming filehandler is first if exists
             return True, logger.handlers[0].baseFilename
        else:
             return True, None # Logging was initialized but handlers might be gone?

    log_filepath = os.path.join(log_dir, LOG_FILENAME)
    try:
        # Remove existing basicConfig handlers if any
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        # Create file handler (DEBUG level, detailed format)
        file_handler = logging.FileHandler(log_filepath, encoding='utf-8', mode='a') # Append mode
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT_FILE))

        # Create console handler (INFO level, simple format)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO) # Only show INFO and above on console
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT_CONSOLE))

        # Add handlers to the root logger
        logging.root.addHandler(file_handler)
        logging.root.addHandler(console_handler)

        # Set root logger level (should be lowest of handlers to allow filtering)
        logging.root.setLevel(logging.DEBUG)

        # Use the configured root logger directly
        logger = logging.getLogger(__name__)

        logger.info(f"File logging initialized: {log_filepath}") # Goes to both file and console (INFO)
        logging_initialized = True
        return True, log_filepath
    except Exception as e:
        # Fallback to basic stdout logging if file setup fails
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT_CONSOLE, stream=sys.stdout) # Use simple format
        logger = logging.getLogger(__name__) # Re-get logger after fallback basicConfig
        logger.error(f"Failed to set up file logging at {log_filepath}: {e}. Logging ONLY essentials to console.", exc_info=True)
        print(f"ERROR: Failed to set up file logging: {e}") # Explicit print for critical error
        logging_initialized = True # Mark as initialized to prevent retries
        return False, None

# --- Helper Functions ---
def sanitize_filename(filename):
    """Removes invalid characters for filenames and limits length."""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", filename)
    sanitized = re.sub(r'\s+', '_', sanitized)
    max_len = 150
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len]
    if not sanitized:
        sanitized = "untitled"
    return sanitized.strip('_')

def is_playlist_or_channel(url):
    """Checks if a URL likely points to a YouTube playlist or channel."""
    playlist_pattern = r'youtube\.com/(playlist|watch\?v=.*&list=|channel/|c/|user/|@)'
    return re.search(playlist_pattern, url, re.IGNORECASE) is not None

def format_timestamp(seconds):
    """Converts seconds (float) to HH:MM:SS.fff string format."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "00:00:00.000"
    milliseconds = int((seconds - int(seconds)) * 1000)
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"

# --- Playlist Extraction ---
def extract_videos_from_playlist_or_channel(url, ydl_opts, limit=DEFAULT_PLAYLIST_VIDEO_LIMIT):
    """Extracts individual video URLs from a playlist or channel URL using yt-dlp."""
    logger.info(f"Attempting to extract videos from playlist/channel URL: {url} (limit: {limit})")
    videos = []
    status_message = "Extraction started"
    error_details = None
    try:
        playlist_opts = {**ydl_opts, 'extract_flat': True, 'force_generic_extractor': False}
        if limit and isinstance(limit, int) and limit > 0:
            playlist_opts['playlist_items'] = f'1-{limit}'

        # Use temporary instance for playlist/channel expansion
        with yt_dlp.YoutubeDL(playlist_opts) as playlist_ydl:
            info = playlist_ydl.extract_info(url, download=False)

        if not info:
            status_message = "Failed: yt-dlp returned no information."
            logger.warning(f"{status_message} URL: {url}")
            return [], status_message, "No info returned"

        entries = info.get('entries')
        title = info.get('title', 'N/A')

        if entries is not None:
            count = 0
            for entry in entries:
                if entry and 'url' in entry:
                    videos.append(entry['url'])
                    count += 1
                else:
                    logger.warning(f"Playlist/Channel entry missing URL: {entry} in source '{title}' ({url})")

            if count > 0:
                type_label = "playlist" if info.get('_type') == 'playlist' else "source"
                status_message = f"Success: Extracted {count} video URLs from {type_label} '{title}'."
                logger.info(status_message)
            else:
                status_message = f"Warning: Source '{title}' found but contains no accessible video entries."
                logger.warning(f"{status_message} URL: {url}")
                error_details = "Source empty or entries inaccessible"

        elif 'webpage_url' in info:
            # Resolved to a single video page
            status_message = "Input URL resolved to a single video, not a playlist/channel."
            logger.info(f"URL {url} resolved to single video: {info['webpage_url']}. Treating as single URL.")
            # Return empty list but indicate it's not expandable
            return [], status_message, "Not a playlist/channel"
        else:
            status_message = "Failed: Could not recognize the URL type or extract entries."
            logger.error(f"{status_message} URL: {url} Info received: {info}")
            error_details = "Unrecognized structure or extraction failure"
            return [], status_message, error_details

        return videos, status_message, None # Success or partial success

    except yt_dlp.utils.DownloadError as de:
        status_message = f"Failed: yt-dlp DownloadError accessing playlist/channel '{url}'"
        error_details = f"yt-dlp DownloadError: {de}"
        logger.error(f"{status_message}. Details: {de}", exc_info=False)
        return [], status_message, error_details
    except Exception as e:
        status_message = f"Failed: Unexpected error extracting videos from '{url}'"
        error_details = f"Exception: {e.__class__.__name__}: {e}"
        logger.error(f"{status_message}. Error: {e}", exc_info=True)
        return [], status_message, error_details

# --- Get Video ID and Title ---
def get_video_id_and_title(url, ydl_instance):
    """Fetches video ID and Title using a yt-dlp instance."""
    try:
        info = ydl_instance.extract_info(url, download=False, process=False) # Try faster first
        video_id = info.get('id')
        video_title = info.get('title')
        if video_id and not video_title: # Fallback if title missing
            # logger.debug(f"Title missing for {video_id}, retrying with process=True") # Keep log minimal
            info = ydl_instance.extract_info(url, download=False, process=True)
            video_title = info.get('title')

        if video_id and video_title:
            return video_id, video_title
        else:
            reason = f"Missing {'ID' if not video_id else ''}{' and ' if not video_id and not video_title else ''}{'Title' if not video_title else ''}"
            logger.warning(f"Could not retrieve valid video ID/Title for URL: {url}. Reason: {reason}.")
            return None, None
    except yt_dlp.utils.DownloadError as de:
        logger.warning(f"yt-dlp DownloadError getting info for {url}: {de}")
        return None, None
    except Exception as e:
        logger.warning(f"Generic exception getting info for {url}: {e.__class__.__name__}")
        logger.debug(f"Detailed exception for {url}", exc_info=True) # Keep details in debug log
        return None, None

# --- Get Transcript Text with Timestamps ---
def get_transcript_text(video_id):
    """
    Fetches transcript text with timestamps for a given YouTube video ID.
    Uses transcript.fetch() and parses FetchedTranscript/Snippet objects.
    Returns: (timestamped_transcript_text, None) on success, (None, reason_string) on failure.
    """
    preferred_langs = ['en', 'en-US', 'en-GB']
    reason = "Unknown transcript fetch failure"
    transcript_details = "N/A"

    for attempt in range(MAX_RETRIES):
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = None

            # Find transcript using list (stable method)
            try:
                transcript = transcript_list.find_manually_created_transcript(preferred_langs)
            except NoTranscriptFound:
                try:
                    transcript = transcript_list.find_generated_transcript(preferred_langs)
                except NoTranscriptFound:
                    # logger.debug(f"No transcript in {preferred_langs} for {video_id}. Trying generic 'en'.")
                    try:
                        transcript = transcript_list.find_manually_created_transcript(['en'])
                    except NoTranscriptFound:
                        try:
                            transcript = transcript_list.find_generated_transcript(['en'])
                        except NoTranscriptFound:
                            available_codes = [t.language_code for t in transcript_list]
                            reason = f"No suitable English transcript found (Available: {available_codes})"
                            logger.warning(f"{reason} for video ID: {video_id}")
                            return None, reason # Exit early, no point retrying

            if transcript:
                transcript_details = f"Lang: {transcript.language_code}, Manual: {not transcript.is_generated}"
                # logger.debug(f"Found transcript for {video_id} ({transcript_details})")
                fetched_transcript_data = transcript.fetch() # Use fetch()

                timestamped_segments = []
                malformed_segments_count = 0

                # Primary parsing logic for FetchedTranscript(snippets=[FetchedTranscriptSnippet(...)])
                if hasattr(fetched_transcript_data, 'snippets') and isinstance(fetched_transcript_data.snippets, list):
                    # logger.debug(f"Parsing FetchedTranscript with {len(fetched_transcript_data.snippets)} snippets for {video_id}")
                    for snippet in fetched_transcript_data.snippets:
                        if hasattr(snippet, 'text') and hasattr(snippet, 'start'):
                            text = snippet.text
                            start_time = snippet.start
                            if text and isinstance(text, str) and isinstance(start_time, (int, float)):
                                formatted_time = format_timestamp(start_time)
                                timestamped_segments.append(f"[{formatted_time}] {text}")
                            else:
                                malformed_segments_count += 1
                                logger.debug(f"Snippet in {video_id} lacked valid text/start content: {snippet}")
                        else:
                            malformed_segments_count += 1
                            logger.debug(f"Snippet object in {video_id} lacked 'text' or 'start' attribute: {snippet}")

                # Fallback check: list of dicts (less likely but for safety)
                elif isinstance(fetched_transcript_data, list):
                    logger.warning(f"Fetched data for {video_id} was list, not expected 'snippets' object. Trying legacy list-of-dicts parsing (timestamps may be missing)...")
                    for item in fetched_transcript_data:
                        if isinstance(item, dict):
                            text = item.get('text')
                            start_time = item.get('start')
                            if text and isinstance(text, str):
                                if start_time is not None and isinstance(start_time, (int, float)):
                                    formatted_time = format_timestamp(start_time)
                                    timestamped_segments.append(f"[{formatted_time}] {text}")
                                else:
                                    timestamped_segments.append(f"[?:??:??.???] {text}") # Indicate missing time
                            else:
                                malformed_segments_count += 1
                        else:
                            malformed_segments_count += 1
                else:
                    # Structure is completely unrecognized
                    reason = "Transcript fetched but structure was unrecognized"
                    logger.warning(f"{reason} for video ID: {video_id} ({transcript_details}). Type: {type(fetched_transcript_data)}")
                    try:
                        raw_data_preview = str(fetched_transcript_data)[:500]
                        logger.debug(f"Unrecognized structure raw preview for {video_id}: {raw_data_preview}")
                    except Exception as log_e:
                        logger.error(f"Could not log raw data for {video_id}: {log_e}")
                    return None, reason # Exit, unknown format

                # Check if parsing yielded any results
                if not timestamped_segments:
                    if reason == "Transcript fetched but structure was unrecognized":
                        pass # Reason already set above
                    else:
                        reason = "Transcript fetched but contained no parseable text segments"
                        if malformed_segments_count > 0:
                            reason += f" ({malformed_segments_count} segments lacked valid text/start)"
                    logger.warning(f"{reason} for video ID: {video_id} ({transcript_details}).")
                    # Log preview only if parsing failed and structure wasn't 'unrecognized'
                    if "structure was unrecognized" not in reason:
                        try:
                            raw_data_preview = str(fetched_transcript_data)[:500]
                            logger.debug(f"Raw fetched data preview (parsing failed) for {video_id}: {raw_data_preview}")
                        except Exception as log_e:
                            logger.error(f"Could not log raw data for {video_id}: {log_e}")
                    return None, reason # Exit early

                # SUCCESS PATH: Join timestamped segments
                full_text_with_timestamps = "\n".join(timestamped_segments) # Join with newline for readability
                full_text_with_timestamps = full_text_with_timestamps.replace('\xa0', ' ').strip()
                # Replace newlines *within* original segments if they sneaked through
                full_text_with_timestamps = full_text_with_timestamps.replace('\n[', ' [') # Avoid breaking start format
                # logger.debug(f"Successfully fetched and processed timestamped transcript for video ID: {video_id}")
                return full_text_with_timestamps, None # SUCCESS

            # else: Transcript object not found (should have been caught by inner try/except)
            # logger.warning(f"Transcript object was None for {video_id} after checks.")
            # return None, "Failed to obtain transcript object"

        # --- Exception handling for API calls ---
        except TranscriptsDisabled:
            reason = "Transcripts disabled for this video"
            logger.warning(f"{reason} (ID: {video_id})")
            return None, reason # No retry
        except NoTranscriptFound:
            reason = "No transcripts available at all for this video (API list empty or initial error)"
            logger.warning(f"{reason} (ID: {video_id})")
            return None, reason # No retry
        except Exception as e:
            error_type = e.__class__.__name__
            reason = f"API Error ({error_type})"
            logger.warning(f"Transcript API error for {video_id} (Attempt {attempt + 1}/{MAX_RETRIES}): {e}", exc_info=False) # Log error, traceback maybe excessive unless final attempt
            if "too many requests" in str(e).lower() or "429" in str(e):
                reason += " (Rate Limited?)"
                delay = RETRY_DELAY * (attempt + 2)
                logger.warning(f"Potential rate limit detected for {video_id}. Waiting {delay}s...")
                time.sleep(delay)
            elif attempt < MAX_RETRIES - 1:
                logger.info(f"Retrying API call for {video_id} after delay...")
                time.sleep(RETRY_DELAY / 2) # Shorter retry delay for general errors
            else:
                reason = f"Max retries reached for API call ({reason})"
                logger.error(f"{reason} for video ID: {video_id}")
                # Log traceback on final failure for API errors
                logger.debug(f"Traceback for final API failure on {video_id}:", exc_info=True)
                return None, reason

    # Fallback if loop somehow completes without returning
    logger.error(f"Transcript fetch loop completed without success for {video_id}. Final reason: {reason}")
    return None, reason

# --- Progress File Handling ---
progress_lock = threading.Lock()
def load_processed_links(filename):
    """Loads processed URLs from the progress file."""
    processed = set()
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                processed = {line.strip() for line in f if line.strip()}
            if processed:
                logger.debug(f"Loaded {len(processed)} URLs from progress file: '{os.path.basename(filename)}'")
            else:
                 logger.debug(f"Progress file '{os.path.basename(filename)}' exists but is empty.")
        except Exception as e:
            logger.warning(f"Could not load progress file '{filename}': {e}")
            print(f"Warning: Could not load progress file '{filename}': {e}") # Keep user warning
    else:
        logger.info(f"Progress file not found: '{filename}'. Starting fresh.") # User info message ok
    return processed

def save_processed_link(filename, url):
    """Appends a successfully processed or attempted URL to the progress file (Thread-Safe)."""
    try:
        # Directory should exist by now, but be defensive? No, assume created in main.
        with progress_lock: # Acquire lock before file access
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(url + '\n')
        # logger.debug(f"Saved URL to progress file '{os.path.basename(filename)}': {url}") # Reduce noise
    except Exception as e:
        logger.error(f"Could not write URL '{url}' to progress file '{filename}': {e}", exc_info=True)
        # Avoid printing from worker thread

# --- Worker Function (Creates own ydl instance) ---
def process_single_url_worker(url, ydl_opts, output_dir, is_multi_file_mode):
    """
    Processes a single video URL in a worker thread. Creates own ydl instance.
    Fetches transcript with timestamps.
    Returns dict: {'status': 'success'/'failure', 'url': url, 'reason': str, 'filepath': str|None, 'title': str, 'id': str, 'transcript_data': dict|None}
    'transcript_data' is {'header': str, 'transcript': str, 'footer': str} for single file mode success.
    """
    # logger.debug(f"Processing: {url}") # Minimal logging
    result = {'status': 'failure', 'url': url, 'reason': "Unknown Error", 'filepath': None, 'title': "N/A", 'id': "N/A", 'transcript_data': None}
    start_single = time.time()

    try:
        # Create short-lived ydl instance within the thread (stable method)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl_instance:
            # --- 1. Get Video Info ---
            video_id, video_title = get_video_id_and_title(url, ydl_instance)

        result['id'] = video_id if video_id else "N/A"
        result['title'] = video_title if video_title else "N/A"

        if video_id and video_title:
            # --- 2. Fetch Transcript (Timestamped) ---
            transcript_text, fetch_fail_reason = get_transcript_text(video_id) # Gets text with timestamps

            if transcript_text: # transcript_text now contains timestamps
                result['status'] = 'success'
                result['reason'] = "Success"

                # --- 3. Save or Prepare Data ---
                if is_multi_file_mode:
                    safe_filename = f"{sanitize_filename(video_title)}_{video_id}.txt"
                    filepath = os.path.join(output_dir, safe_filename)
                    result['filepath'] = filepath
                    try:
                        # Ensure directory exists (should be safe, created in main)
                        os.makedirs(output_dir, exist_ok=True)
                        with open(filepath, 'w', encoding='utf-8') as f:
                             f.write(transcript_text) # Write text with timestamps
                        # logger.debug(f"Successfully saved timestamped transcript to: {filepath}")
                    except Exception as e:
                        result['status'] = 'failure'
                        result['reason'] = f"File write error: {e}"
                        logger.error(f"Failed to save transcript file {filepath}: {e}", exc_info=True)
                else: # Single file mode
                    # Header/Footer remain same, the transcript content itself is now timestamped
                    header = f"=== VIDEO START ===\nTitle: {result['title']}\nURL: {url}\nVideo ID: {result['id']}\n--- Transcript ---"
                    footer = "=== VIDEO END ===\n" + "="*50 + "\n"
                    # The transcript text already includes timestamps and newlines
                    result['transcript_data'] = {'header': header, 'transcript': transcript_text, 'footer': footer}
                    # logger.debug(f"Successfully fetched timestamped transcript for combined file: {url}")
            else:
                # Transcript fetch failed
                result['reason'] = fetch_fail_reason if fetch_fail_reason else "Transcript fetch failed (Unknown)"
                logger.warning(f"Transcript fetch failed for '{video_title}' (ID: {video_id}). Reason: {result['reason']}")
        else:
            # Getting video ID or Title failed
            result['reason'] = "Could not retrieve video details (deleted, private, unavailable?)"
            logger.warning(f"Failed to get video info for URL: {url}. Reason: {result['reason']}")

    except KeyboardInterrupt:
         logger.warning(f"Interruption signal received for URL: {url}")
         result['reason'] = "User Interruption (Worker)"
         # Let main thread handle pool shutdown
    except Exception as e:
        result['reason'] = f"Unexpected worker error: {e.__class__.__name__}: {e}"
        logger.exception(f"An unexpected error occurred processing URL {url}: {e}")

    end_single = time.time()
    processing_time = end_single - start_single
    result['duration'] = processing_time
    return result

# --- Main Execution Logic ---
if __name__ == "__main__":
    # Print header early
    print("=" * 60)
    print(f" YouTube Transcript Extractor v{SCRIPT_VERSION} (Workers: {MAX_WORKERS})")
    print("=" * 60)

    # --- Mode Determination ---
    is_interactive_mode = len(sys.argv) == 1
    is_file_input_mode = len(sys.argv) > 1

    # --- Determine Default Output Base Directory (Cleaned up) ---
    default_output_base_dir = os.getcwd()
    default_display_name = "current directory"
    try:
        home_dir = os.path.expanduser("~")
        default_desktop_path = os.path.join(home_dir, "Desktop")

        if os.path.isdir(default_desktop_path):
            default_output_base_dir = default_desktop_path
            default_display_name = "Desktop"
        elif os.path.isdir(home_dir):
            default_output_base_dir = home_dir
            default_display_name = "your Home directory"
        # If neither exists, the initial default (os.getcwd()) remains

    except Exception as e:
        print(f"\n⚠️ Warning: Could not determine default Desktop/Home path: {e}. Defaulting to {default_display_name}.")
        # Ensure defaults are set even if error occurred
        if 'default_output_base_dir' not in locals(): default_output_base_dir = os.getcwd()
        if 'default_display_name' not in locals(): default_display_name = "current directory"

    # --- yt-dlp options are defined in YDL_SHARED_OPTS, instance created per worker ---

    log_file_path = None # Defined later after output path is known

    # ========================================
    # --- BRANCH 1: INTERACTIVE MODE ---
    # ========================================
    if is_interactive_mode:
        print("\n▶️ Interactive Mode Activated.")

        # --- Get Output Location (Cleaned up) ---
        interactive_output_dir = None
        while interactive_output_dir is None:
            print("\n📁 --- Output Location ---")
            user_path_input = input(f"Enter FULL path for output folder (Enter for default: '{default_display_name}'): ").strip()

            if not user_path_input:
                # User pressed Enter for default path
                chosen_output_base_dir = default_output_base_dir
                interactive_output_dir = os.path.join(chosen_output_base_dir, "Interactive_Transcripts")
                print(f"  -> Using default subfolder: '{interactive_output_dir}'")
                break # Exit the while loop
            else:
                # User provided a path, validate it
                interactive_output_dir_candidate = os.path.abspath(user_path_input)
                parent_dir = os.path.dirname(interactive_output_dir_candidate)

                # Check if parent directory exists
                if not os.path.isdir(parent_dir):
                    print(f"  -> ⚠️ Error: Parent directory '{parent_dir}' does not exist.")
                    continue # Ask for input again

                # Check if the path exists and is a file (not a directory)
                if os.path.exists(interactive_output_dir_candidate) and not os.path.isdir(interactive_output_dir_candidate):
                    print(f"  -> ⚠️ Error: Path exists but is a file.")
                    continue # Ask for input again

                # If all checks passed, accept the path
                interactive_output_dir = interactive_output_dir_candidate
                print(f"  -> Using specified path: {interactive_output_dir}")
                break # Exit the while loop

        # --- Setup Logging (Cleaned up) ---
        try:
            os.makedirs(interactive_output_dir, exist_ok=True)
            log_setup_success, log_file_path = setup_logging(interactive_output_dir)

            if not log_setup_success:
                # Error message printed inside setup_logging fallback
                pass

            # Log startup info (even if file logging failed, basicConfig logs to console)
            logger.info("--- Interactive Mode Started ---")
            logger.debug(f"Script Version: {SCRIPT_VERSION}")
            logger.debug(f"Python Version: {sys.version}")
            logger.debug(f"Platform: {platform.platform()}")
            logger.info(f"Output Directory: {interactive_output_dir}")

        except Exception as e:
            print(f"\n❌ Error creating output directory or setting up logging: {e}")
            # Use basic print as logger might not be fully functional
            print(f"CRITICAL: Logger potentially not available for error message: {e}")
            traceback.print_exc() # Show traceback for critical setup error
            input("Press Enter to exit.")
            sys.exit(1)

        # --- Interactive Loop ---
        print("\n" + "=" * 60)
        print("🚀 Enter YouTube video, playlist, or channel URLs.")
        print("   Type 'quit' or 'exit' (or just press Enter) to finish.")
        print("=" * 60)

        success_count_interactive = 0
        failure_count_interactive = 0
        total_processed_interactive = 0
        playlist_expansion_failures_interactive = []
        interactive_success_lock = threading.Lock()
        interactive_failure_lock = threading.Lock()

        while True:
            try:
                url_input = input("\nEnter URL (or type 'quit'): ").strip()
                if not url_input or url_input.lower() in ['quit', 'exit']:
                    logger.info("User requested exit from interactive mode.")
                    print("\n🏁 Exiting interactive mode.")
                    break

                if not re.match(r'^https?://', url_input.lower()):
                    print("⚠️ Invalid input. Please enter a valid URL starting with http:// or https://")
                    logger.warning(f"Invalid URL format entered by user: {url_input}")
                    continue

                urls_to_process_interactive = []
                is_single_video = True

                # Expand Playlist/Channel if applicable
                if is_playlist_or_channel(url_input):
                    is_single_video = False
                    logger.info(f"Playlist/Channel URL detected: {url_input}")
                    print(f"  Detected Playlist/Channel URL. Expanding...")
                    playlist_limit = DEFAULT_PLAYLIST_VIDEO_LIMIT # Or prompt user?
                    # Pass YDL_SHARED_OPTS for expansion which uses temporary instance
                    extracted_videos, status_msg, error_detail = extract_videos_from_playlist_or_channel(url_input, YDL_SHARED_OPTS, limit=playlist_limit)

                    if extracted_videos:
                        print(f"  -> Extracted {len(extracted_videos)} video URLs.")
                        logger.info(status_msg)
                        urls_to_process_interactive.extend(extracted_videos)
                    elif status_msg == "Input URL resolved to a single video, not a playlist/channel.":
                        # Treat as single video
                        print(f"  -> URL is a single video.")
                        is_single_video = True
                        urls_to_process_interactive.append(url_input)
                    else:
                        # Expansion failed
                        print(f"  ⚠️ Expansion Failed: {status_msg}")
                        logger.warning(f"Failed to expand playlist/channel {url_input}: {status_msg} ({error_detail})")
                        playlist_expansion_failures_interactive.append({'url': url_input, 'reason': status_msg})
                        continue # Ask for next URL
                else:
                    # Single video URL
                    logger.info(f"Single video URL detected: {url_input}")
                    urls_to_process_interactive.append(url_input)

                if not urls_to_process_interactive:
                    continue # Should not happen often if expansion logic is correct

                # --- Process URLs ---
                num_urls = len(urls_to_process_interactive)
                if num_urls == 0:
                    continue

                if num_urls > 1:
                    print(f"  Processing {num_urls} video URL(s) using up to {MAX_WORKERS} workers...")
                    futures = []
                    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='InteractiveWorker') as executor:
                        for video_url in urls_to_process_interactive:
                            # Interactive mode always saves individual files (is_multi_file_mode=True)
                            future = executor.submit(process_single_url_worker, video_url, YDL_SHARED_OPTS, interactive_output_dir, True)
                            futures.append(future)

                        pbar_format_interactive = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"
                        batch_success = 0
                        batch_failure = 0
                        with tqdm(total=num_urls, desc="Processing Batch", unit="Video", ncols=min(CONSOLE_WIDTH, os.get_terminal_size().columns if hasattr(os, 'get_terminal_size') else CONSOLE_WIDTH), leave=False, file=sys.stdout, bar_format=pbar_format_interactive) as pbar_interactive:
                            for future in concurrent.futures.as_completed(futures):
                                try:
                                    result = future.result()
                                    total_processed_interactive += 1
                                    if result['status'] == 'success':
                                        with interactive_success_lock:
                                            success_count_interactive += 1
                                        batch_success += 1
                                        status_msg = f"✅ {os.path.basename(result.get('filepath','OK'))}"
                                        pbar_interactive.set_postfix_str(status_msg, refresh=True)
                                    else:
                                        with interactive_failure_lock:
                                            failure_count_interactive += 1
                                        batch_failure += 1
                                        status_msg = f"❌ {result['reason']:.30s}"
                                        pbar_interactive.set_postfix_str(status_msg, refresh=True)
                                except Exception as e:
                                    logger.error(f"Error processing future in interactive mode: {e}", exc_info=True)
                                    with interactive_failure_lock:
                                        failure_count_interactive += 1
                                    batch_failure += 1
                                    pbar_interactive.set_postfix_str("‼️ Future Error", refresh=True)
                                finally:
                                    pbar_interactive.update(1) # Update progress bar

                    print(f"\nBatch Summary: {batch_success} succeeded, {batch_failure} failed.")

                elif num_urls == 1:
                    # Process single URL sequentially
                    video_url = urls_to_process_interactive[0]
                    print(f"  Processing single video...")
                    # Call worker directly (simpler than setting up pool for one)
                    # Interactive mode always saves individual files (is_multi_file_mode=True)
                    result = process_single_url_worker(video_url, YDL_SHARED_OPTS, interactive_output_dir, True)
                    total_processed_interactive += 1
                    if result['status'] == 'success':
                        with interactive_success_lock:
                            success_count_interactive += 1
                        print(f"✅ Success! Saved to {os.path.basename(result.get('filepath','N/A'))} ({result['duration']:.2f}s)")
                    else:
                        with interactive_failure_lock:
                             failure_count_interactive += 1
                        print(f"❌ Failed: {result['reason']} ({result['duration']:.2f}s)")

            except KeyboardInterrupt:
                logger.warning("User interrupted interactive loop.")
                print("\n🛑 User interrupted. Exiting interactive mode.")
                break
            except Exception as loop_e:
                 logger.exception(f"An unexpected error occurred in the interactive input loop: {loop_e}")
                 print(f"\n‼️ An unexpected error occurred in the input loop: {loop_e}")
                 traceback.print_exc()


        # --- Interactive Summary ---
        print("-" * 60)
        print("\n📋 Interactive Mode Summary:")
        logger.info("--- Interactive Mode Summary ---")
        print(f"   Output Directory:   '{interactive_output_dir}'")
        logger.info(f"Output Directory: {interactive_output_dir}")
        if log_file_path:
            print(f"   Detailed Log File:  '{log_file_path}'")
            logger.info(f"Detailed Log File: {log_file_path}")
        else:
            print(f"   Detailed Log File:  (File logging failed)")
            logger.warning("Detailed Log File: N/A (File logging failed)")

        print(f"   Total Videos Processed: {total_processed_interactive}")
        logger.info(f"Total Videos Processed: {total_processed_interactive}")
        print(f"   ✅ Successful Files:   {success_count_interactive}")
        logger.info(f"Successful Files: {success_count_interactive}")
        print(f"   ❌ Failed Attempts:    {failure_count_interactive}")
        logger.info(f"Failed Attempts: {failure_count_interactive}")
        if playlist_expansion_failures_interactive:
            print(f"   ⚠️ Playlist Expansion Failures: {len(playlist_expansion_failures_interactive)}")
            logger.warning(f"Playlist Expansion Failures: {len(playlist_expansion_failures_interactive)}")

    # ========================================
    # --- BRANCH 2: FILE INPUT MODE ---
    # ========================================
    elif is_file_input_mode:
        print("\n▶️ File Input Mode Activated.")
        print("   (Processing URLs from a .txt file using multiple workers)")

        # --- Input File Validation ---
        if len(sys.argv) < 2:
             print(f"\n❌ Error: Please provide the input .txt file path as a command-line argument.")
             input("Press Enter to exit.")
             sys.exit(1)
        input_links_filepath = sys.argv[1]
        print(f"\n▶️ Input file: {os.path.basename(input_links_filepath)}")
        if not os.path.isfile(input_links_filepath):
            print(f"\n❌ Error: Input file not found: '{input_links_filepath}'")
            input("Press Enter to exit.")
            sys.exit(1)
        if not input_links_filepath.lower().endswith('.txt'):
            print(f"\n❌ Error: Input must be a .txt file.")
            input("Press Enter to exit.")
            sys.exit(1)

        # --- Output Mode Selection ---
        output_mode = None
        while output_mode not in ['1', '2']:
            print("\n📄 Choose Output Mode:")
            print("  [1] Single Combined TXT File")
            print("  [2] Multiple Individual TXT Files (in a folder)")
            output_mode = input("Enter choice (1 or 2): ").strip()
            if output_mode not in ['1', '2']:
                print("⚠️ Invalid choice.")

        # --- Output Location Selection (Cleaned up) ---
        chosen_output_base_dir_filemode = None
        while chosen_output_base_dir_filemode is None:
            print("\n📁 --- Output Location ---")
            user_path_input = input(f"Enter FULL path for output (Enter for default: '{default_display_name}'): ").strip()
            if not user_path_input:
                chosen_output_base_dir_filemode = default_output_base_dir
                print(f"  -> Using default: {chosen_output_base_dir_filemode}")
                break
            else:
                output_base_candidate = os.path.abspath(user_path_input)
                # Check if parent directory exists (important!)
                parent_dir = os.path.dirname(output_base_candidate)
                if not os.path.isdir(parent_dir):
                    print(f"  -> ⚠️ Error: Parent directory '{parent_dir}' does not exist.")
                    continue
                # Check if path exists and is a file
                if os.path.exists(output_base_candidate) and not os.path.isdir(output_base_candidate):
                    print(f"  -> ⚠️ Error: Path exists but is a file.")
                    continue
                chosen_output_base_dir_filemode = output_base_candidate
                print(f"  -> Using specified path: {chosen_output_base_dir_filemode}")
                break

        # --- Determine Final Paths & Setup Logging (File Mode - Cleaned up) ---
        is_single_file_mode = (output_mode == '1')
        is_multi_file_mode = (output_mode == '2')
        output_path = None
        log_and_progress_dir = None
        progress_file_path = None

        try:
            input_filename_base = os.path.splitext(os.path.basename(input_links_filepath))[0]
            sanitized_base = sanitize_filename(input_filename_base)

            if is_single_file_mode:
                output_filename = f"Combined_Transcripts_from_{sanitized_base}.txt"
                # Output file goes directly into chosen base directory
                output_path = os.path.join(chosen_output_base_dir_filemode, output_filename)
                # Logs and progress file also go into chosen base directory
                log_and_progress_dir = chosen_output_base_dir_filemode
                print(f"\n⚙️ Mode: Single Combined TXT")
                print(f"   Output file: '{output_path}'")
            else: # Multi-file mode
                output_directory_name = f"Transcripts_from_{sanitized_base}"
                # Output folder goes into chosen base directory
                output_path = os.path.join(chosen_output_base_dir_filemode, output_directory_name)
                # Logs and progress file go *inside* the output folder for this batch
                log_and_progress_dir = output_path
                print(f"\n⚙️ Mode: Multiple Individual TXT")
                print(f"   Output folder: '{output_path}'")

            # Ensure log/progress dir exists *before* logging setup
            os.makedirs(log_and_progress_dir, exist_ok=True)
            if not os.path.isdir(log_and_progress_dir):
                # If creation failed despite exist_ok, raise error
                raise OSError(f"Failed to create log/progress directory: '{log_and_progress_dir}'")

            # Setup File Logging
            log_setup_success, log_file_path = setup_logging(log_and_progress_dir)
            if not log_setup_success:
                # Warning already printed by setup_logging fallback
                pass

            # Log initial info (goes to console via basicConfig if file log failed)
            logger.info("--- File Input Mode Started ---")
            logger.debug(f"Script Version: {SCRIPT_VERSION}")
            logger.debug(f"Python Version: {sys.version}")
            logger.debug(f"Platform: {platform.platform()}")
            logger.info(f"Input File: {input_links_filepath}")
            logger.info(f"Output Mode: {'Single File' if is_single_file_mode else 'Multiple Files'}")
            logger.info(f"Output Base Directory: {chosen_output_base_dir_filemode}")
            logger.info(f"Final Output Path: {output_path}")
            logger.info(f"Log/Progress Directory: {log_and_progress_dir}")

            # Define progress file path
            progress_filename = f".progress_{sanitized_base}.log"
            progress_file_path = os.path.join(log_and_progress_dir, progress_filename)
            print(f"   Progress file: '{progress_file_path}'")
            logger.info(f"Progress File Path: {progress_file_path}")

        except Exception as e:
            print(f"\n❌ Error setting up paths or logging: {e}")
            if logger: # Log only if logger was somewhat initialized
                 logger.error(f"Critical error during file mode setup: {e}", exc_info=True)
            traceback.print_exc()
            input("Press Enter to exit.")
            sys.exit(1)

        # --- Ensure Final Output Directory/Path exists ---
        try:
            # If multi-mode, the dir is output_path itself.
            # If single-mode, the dir is the parent of output_path.
            actual_output_dir = output_path if is_multi_file_mode else os.path.dirname(output_path)
            os.makedirs(actual_output_dir, exist_ok=True)
            if not os.path.isdir(actual_output_dir):
                 raise OSError(f"Failed to create final output directory: '{actual_output_dir}'")
            logger.debug(f"Confirmed final output directory exists/created: {actual_output_dir}")
        except Exception as e:
             print(f"\n❌ Error creating/accessing final output directory: {e}")
             logger.error(f"Error ensuring final output directory '{actual_output_dir}' exists: {e}", exc_info=True)
             input("Press Enter to exit.")
             sys.exit(1)

        # --- Load Links & Expand ---
        raw_urls_from_file = []
        total_raw_urls = 0
        try:
            with open(input_links_filepath, 'r', encoding='utf-8') as f:
                # Read and filter for valid http/https URLs immediately
                raw_urls_from_file = [line.strip() for line in f if line.strip() and re.match(r'^https?://', line.strip().lower())]
            total_raw_urls = len(raw_urls_from_file)
            if total_raw_urls == 0:
                print(f"\n⚠️ Warning: No valid URLs found in '{os.path.basename(input_links_filepath)}'.")
                logger.warning(f"No valid URLs found in input file: {input_links_filepath}")
            else:
                 print(f"\n📊 Found {total_raw_urls} valid URL line(s) in file.")
                 logger.info(f"Found {total_raw_urls} valid URL lines in input file.")
        except Exception as e:
            print(f"\n❌ Error reading links file '{input_links_filepath}': {e}")
            logger.error(f"Error reading input file: {e}", exc_info=True)
            input("Press Enter to exit.")
            sys.exit(1)

        # --- Expand Playlists/Channels ---
        all_individual_video_urls = []
        playlist_expansion_failures = []
        playlist_limit_file_mode = DEFAULT_PLAYLIST_VIDEO_LIMIT

        if raw_urls_from_file:
             print("   Expanding playlists/channels (if any)...")
             logger.info("Starting playlist/channel expansion phase.")
             # Use tqdm for visual feedback during expansion
             pbar_format_expand = '{l_bar}{bar}| {n_fmt}/{total_fmt}'
             with tqdm(raw_urls_from_file, desc="Expanding URLs", unit="URL", ncols=min(CONSOLE_WIDTH, os.get_terminal_size().columns if hasattr(os, 'get_terminal_size') else CONSOLE_WIDTH), leave=False, file=sys.stdout, bar_format=pbar_format_expand) as pbar_expand:
                 for url_line in pbar_expand:
                     if is_playlist_or_channel(url_line):
                         # pbar_expand.set_postfix_str(f"Expanding: {url_line[:50]}...") # Can be noisy
                         logger.debug(f"Expanding playlist/channel URL: {url_line}")
                         # Pass YDL_SHARED_OPTS for expansion process
                         extracted, status, err_detail = extract_videos_from_playlist_or_channel(url_line, YDL_SHARED_OPTS, limit=playlist_limit_file_mode)
                         if extracted:
                             all_individual_video_urls.extend(extracted)
                             # logger.info(f"Expanded '{url_line}' into {len(extracted)} video URLs.") # Reduce log noise
                         elif status == "Input URL resolved to a single video, not a playlist/channel.":
                             # Handle case where yt-dlp identifies it as single video
                             logger.info(f"URL '{url_line}' resolved to single video during expansion.")
                             all_individual_video_urls.append(url_line)
                         else:
                             # Log failure
                             logger.warning(f"Failed to expand playlist/channel '{url_line}': {status} ({err_detail})")
                             playlist_expansion_failures.append({'url': url_line, 'reason': status})
                     else:
                         # It's already an individual video URL
                         all_individual_video_urls.append(url_line)
                         # logger.debug(f"Adding single video URL directly: {url_line}") # Reduce noise
                     # time.sleep(0.02) # Small delay might help avoid rapid-fire API hits if needed? Probably not.

             total_individual_urls = len(all_individual_video_urls)
             print(f"   -> Found {total_individual_urls} total individual video URLs.")
             logger.info(f"Expansion phase finished. Found {total_individual_urls} individual video URLs.")
             if playlist_expansion_failures:
                 print(f"   ⚠️ Encountered {len(playlist_expansion_failures)} errors during expansion.")
                 logger.warning(f"Encountered {len(playlist_expansion_failures)} errors during playlist/channel expansion.")
        # else: No raw URLs to begin with


        # --- Load Progress & Prepare URLs ---
        processed_links = set()
        if progress_file_path: # Check if path was successfully determined
            processed_links = load_processed_links(progress_file_path)
        else:
            logger.warning("Progress file path not set, cannot load or save progress.")

        # Deduplicate while preserving order (important for potential later sorting)
        unique_individual_urls = list(dict.fromkeys(all_individual_video_urls))
        logger.info(f"Found {len(unique_individual_urls)} unique individual video URLs after deduplication.")

        urls_to_process = [url for url in unique_individual_urls if url not in processed_links]
        skipped_count = len(unique_individual_urls) - len(urls_to_process)

        if skipped_count > 0:
            print(f"   Skipping {skipped_count} video(s) found in progress file.")
            logger.info(f"Skipped {skipped_count} URLs found in progress file.")

        remaining_count = len(urls_to_process)
        if remaining_count == 0:
            if total_individual_urls > 0 or total_raw_urls > 0:
                 print("\n✅ No new videos to process (all in progress log or expansion failed).")
                 logger.info("No new video URLs remain to be processed.")
            # else: Input file was empty or only contained invalid URLs - handled earlier
        else:
            print(f"   Processing {remaining_count} new video URL(s) using up to {MAX_WORKERS} workers...")
            logger.info(f"Starting processing loop for {remaining_count} new video URLs.")


        # --- Initialize Loop Variables (File Mode) ---
        collected_data_single_mode = [] # Stores {'header': str, 'transcript': str, 'footer': str}
        failed_video_details = []
        success_count = 0
        total_time_spent_processing = 0 # Track actual processing time across threads
        start_time_batch = time.time() # Overall batch start time

        # Locks for shared resources in file mode
        success_lock = threading.Lock()
        failure_lock = threading.Lock()
        data_collect_lock = threading.Lock()
        # progress_lock is defined globally near save_processed_link

        # --- Process Videos Loop (Multithreaded) ---
        if remaining_count > 0:
            print("-" * 60)
            logger.info(f"Starting main video processing loop with {MAX_WORKERS} workers.")
            futures = []
            try:
                # Using ThreadPoolExecutor to manage workers
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='FileWorker') as executor:
                    # Submit all tasks to the executor
                    for video_url in urls_to_process:
                        # Pass YDL_SHARED_OPTS; worker will create its own instance
                        future = executor.submit(process_single_url_worker, video_url, YDL_SHARED_OPTS, actual_output_dir, is_multi_file_mode)
                        futures.append(future)

                    # Process results as they complete using tqdm progress bar
                    pbar_format = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"
                    with tqdm(total=remaining_count, desc="Processing Videos", unit="Video", ncols=min(CONSOLE_WIDTH, os.get_terminal_size().columns if hasattr(os, 'get_terminal_size') else CONSOLE_WIDTH), leave=True, file=sys.stdout, bar_format=pbar_format) as pbar:
                        for future in concurrent.futures.as_completed(futures):
                            url_processed_or_attempted = False
                            processed_successfully = False
                            result = None
                            try:
                                result = future.result() # Get result from worker
                                url_processed_or_attempted = True # Mark as attempted once result is retrieved
                                total_time_spent_processing += result.get('duration', 0) # Accumulate worker time

                                if result['status'] == 'success':
                                    processed_successfully = True
                                    with success_lock:
                                        success_count += 1
                                    # If single file mode, collect the transcript data (Thread-Safe)
                                    if is_single_file_mode and result.get('transcript_data'):
                                        with data_collect_lock:
                                            collected_data_single_mode.append(result['transcript_data'])
                                        # logger.debug(f"Collected timestamped transcript for {result['url']} for combined file.")
                                    # Update progress bar postfix for success
                                    status_msg = f"✅ {result['title'][:30]}..." if result['title'] != 'N/A' else "✅ Success"
                                    pbar.set_postfix_str(f"{status_msg} ({result['duration']:.1f}s)", refresh=False) # Less frequent refresh helps performance

                                else:
                                    # Add to failure list (Thread-Safe)
                                    with failure_lock:
                                        failed_video_details.append({
                                            'url': result['url'],
                                            'title': result['title'],
                                            'reason': result['reason']
                                        })
                                    # Update progress bar postfix for failure
                                    pbar.set_postfix_str(f"❌ {result['reason']:.30s} ({result['duration']:.1f}s)", refresh=False)

                            except Exception as e:
                                # Handle exceptions raised *during future.result()* (e.g., worker crashed badly)
                                url_processed_or_attempted = True # Still counts as attempted
                                final_reason = f"Future processing error: {e.__class__.__name__}"
                                logger.error(f"Error retrieving result for a future: {e}", exc_info=True)
                                with failure_lock:
                                     failed_video_details.append({'url': 'Unknown (Future Error)', 'title': 'N/A', 'reason': final_reason})
                                pbar.set_postfix_str("‼️ Future Error", refresh=False)
                            finally:
                                # Save progress regardless of success/failure, if attempted
                                if url_processed_or_attempted and result and 'url' in result:
                                    if progress_file_path: # Ensure path is valid
                                        save_processed_link(progress_file_path, result['url'])
                                    # else: Warning logged earlier if path is not set
                                pbar.update(1) # Update progress bar count

            except KeyboardInterrupt:
                print("\n" + ("-" * 60) + "\n🛑 User interrupted. Waiting for active tasks to finish... Progress saved for completed items.")
                logger.warning("User interrupted batch processing loop. Allowing active threads to finish.")
                # The 'with executor' block will wait for running threads upon exit
            except Exception as e:
                print(f"\n‼️ Unexpected error managing thread pool: {e}")
                logger.exception("Unexpected error outside main processing loop (ThreadPoolExecutor).")
            finally:
                 logger.info("Finished main video processing loop.")

            print("-" * 60) # Separator after loop finishes or is interrupted

        # --- Write Combined File (Mode 1) ---
        print("\n🏁 Finished processing URLs.")
        logger.info("Finished processing URLs loop.")
        write_error = None
        if is_single_file_mode:
            # Sort collected data? Optional, depends if order matters. Currently order of completion.
            if collected_data_single_mode:
                print(f"\n💾 Writing {len(collected_data_single_mode)} transcript(s) to combined file...")
                logger.info(f"Writing {len(collected_data_single_mode)} transcripts to combined file: {output_path}")
                try:
                    with open(output_path, 'w', encoding='utf-8') as f:
                        # The 'transcript' in collected_data already has timestamps and newlines
                        for item in collected_data_single_mode:
                            f.write(item['header'] + "\n")
                            f.write(item['transcript'] + "\n") # Transcript content
                            f.write(item['footer'] + "\n") # Includes === VIDEO END === and separator + newline
                    print(f"   -> Saved: '{os.path.basename(output_path)}'")
                    logger.info(f"Successfully saved combined file: {output_path}")
                except Exception as e:
                    write_error = f"writing combined TXT file '{os.path.basename(output_path)}': {e}"
                    logger.error(f"Error {write_error}", exc_info=True)
                    print(f"\n❌ Error {write_error}")
            elif remaining_count > 0: # Only mention if we attempted to process files
                print("\nℹ️ No transcripts were successfully processed to be included in the combined file.")
                logger.info("No successful transcripts collected for combined file.")
            # else: No URLs were processed, message handled earlier.

        elif is_multi_file_mode:
             # Multi-file mode automatically gets timestamped content in each file
             if success_count > 0:
                 print(f"\n💾 {success_count} individual timestamped TXT files saved in folder:")
                 print(f"   '{output_path}'")
                 logger.info(f"{success_count} individual files saved in: {output_path}")
             # Message for zero successes handled implicitly by summary

        # --- Final Summary (File Mode) ---
        end_time_batch = time.time() # Overall batch end time
        total_wall_clock_duration = end_time_batch - start_time_batch if (success_count + len(failed_video_details) > 0) else 0
        failure_count = len(failed_video_details)

        print("-" * 60)
        print("\n📋 File Mode Execution Summary:")
        logger.info("--- File Mode Execution Summary ---")

        # Print paths
        print(f"   Input File:          '{os.path.basename(input_links_filepath)}'")
        logger.info(f"Input File: {input_links_filepath}")
        if is_single_file_mode:
            print(f"   Output File:         '{output_path}'")
            logger.info(f"Output File: {output_path}")
        else: # is_multi_file_mode
            print(f"   Output Folder:       '{output_path}'")
            logger.info(f"Output Folder: {output_path}")
        if log_file_path:
            print(f"   Detailed Log File:   '{log_file_path}'")
            logger.info(f"Detailed Log File: {log_file_path}")
        else:
            print(f"   Detailed Log File:   N/A (File logging failed)")
            logger.info("Detailed Log File: N/A (File logging failed)")
        if progress_file_path:
            print(f"   Progress File:       '{progress_file_path}'")
            logger.info(f"Progress File: {progress_file_path}")
        else:
            print(f"   Progress File:       N/A (Path not set)")
            logger.warning("Progress File: N/A (Path not set)")

        # Print counts
        print("-" * 30)
        print(f"   URLs in Input:       {total_raw_urls}")
        logger.info(f"URLs in Input File: {total_raw_urls}")
        print(f"   Expanded Videos:     {total_individual_urls}")
        logger.info(f"Expanded Videos: {total_individual_urls}")
        print(f"   Unique Videos:       {len(unique_individual_urls)}")
        logger.info(f"Unique Videos: {len(unique_individual_urls)}")
        print(f"   Skipped (Progress):  {skipped_count}")
        logger.info(f"Skipped (in log): {skipped_count}")
        print(f"   Attempted This Run:  {remaining_count}")
        logger.info(f"Attempted This Run: {remaining_count}")
        print(f"   ✅ Success:           {success_count}")
        logger.info(f"Successful Fetches: {success_count}")
        print(f"   ❌ Failed Videos:     {failure_count}")
        logger.info(f"Failed Videos: {failure_count}")
        if playlist_expansion_failures:
            print(f"   ⚠️ Playlist Errors:   {len(playlist_expansion_failures)}")
            logger.info(f"Playlist Expansion Failures: {len(playlist_expansion_failures)}")

        # Print timing
        print("-" * 30)
        attempted_count = success_count + failure_count
        if attempted_count > 0:
            print(f"   Total Wall Time:     {total_wall_clock_duration:.2f} seconds")
            logger.info(f"Total Wall Time: {total_wall_clock_duration:.2f} seconds")
            avg_wall_time_per_video = total_wall_clock_duration / attempted_count
            print(f"   Avg. Wall Time/Vid:  {avg_wall_time_per_video:.2f} seconds")
            logger.info(f"Avg. Wall Time per Video: {avg_wall_time_per_video:.2f} seconds")
        else:
            print("   Total Wall Time:      N/A (No new videos processed)")
            logger.info("Total Wall Time: N/A")

        # --- Failure Details ---
        if playlist_expansion_failures:
            print("-" * 60)
            print(f"\n⚠️ Playlist/Channel Expansion Failure Details ({len(playlist_expansion_failures)}):")
            logger.warning("--- Playlist/Channel Expansion Failures ---")
            for failure in playlist_expansion_failures:
                print(f"   - URL: {failure['url']}\n     Reason: {failure['reason']}")
                logger.warning(f"  URL: {failure['url']}, Reason: {failure['reason']}")

        if failed_video_details:
            print("-" * 60)
            print(f"\n⚠️ Individual Video Failure Summary ({failure_count} total):")
            logger.warning("--- Individual Video Failures ---")
            failure_reasons = Counter(f['reason'] for f in failed_video_details)
            print("   Failures by Reason:")
            for reason, count in failure_reasons.items():
                print(f"     - {count}: {reason}")
                logger.warning(f"  Reason: '{reason}', Count: {count}")

            # Ask to show details only if there were failures
            show_details = input("\n   List all failed video URLs? (y/N): ").strip().lower()
            if show_details == 'y':
                print("\n   --- Full Failed Video List ---")
                logger.info("Displaying full list of failed video URLs:")
                for failure in failed_video_details:
                     title_info = f"(Title: {failure.get('title', 'N/A')[:50]}{'...' if len(failure.get('title', 'N/A')) > 50 else ''})" if failure.get('title', 'N/A') != 'N/A' else ""
                     print(f"   - URL: {failure['url']} {title_info}\n     Reason: {failure['reason']}")
                     logger.info(f"    - Fail: {failure['url']}, Title: {failure.get('title', 'N/A')}, Reason: {failure['reason']}")
            else:
                 logger.info("User opted not to display full list of failed video URLs.")
        elif remaining_count > 0 and failure_count == 0 and not playlist_expansion_failures:
            # Only print if videos were attempted and all succeeded
            print("-" * 60)
            print("\n✅ All attempted operations completed without reported errors.")
            logger.info("All attempted operations completed without reported errors.")
        # Else: Either no videos attempted, or failures occurred (covered above)


    # ========================================
    # --- Fallback/Error Case ---
    # ========================================
    else:
         # This case should not be reachable if sys.argv logic is correct
         print("\n❌ Error: Could not determine execution mode (Invalid arguments?).")
         logger.critical("Could not determine execution mode.")

    # --- Common Exit ---
    print("=" * 60)
    print(f"✅ Processing finished.") # Explicit finish message
    if log_file_path:
        print(f"ℹ️ For detailed logs, see: {log_file_path}")
    elif logging_initialized: # Check if logger was setup even if path failed
        print(f"⚠️ Detailed file logging failed. Check console output/basic log if configured.")
    # else: Logging setup failed very early, message printed then.

    print("=" * 60)
    logger.info("--- Script Execution Finished ---")
    input("\nPress Enter to close this window.") # Keep window open
    logging.shutdown() # Properly close handlers
    sys.exit(0)
    # --- END MAIN ---

# --- END COPYING HERE ---
# --- END OF FILE get_just_transcripts_clean.py ---