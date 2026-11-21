import os
import gzip
import re
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
import requests

try:
    from lxml import etree as lxml_etree
    HAS_LXML = True
except ImportError:
    HAS_LXML = False

# Settings
NAME = "e"
# Get URLS from GitHub Secret
raw_urls = os.environ.get("M3U_URL", "")
URLS = [u.strip() for u in re.split(r'[\r\n,]+', raw_urls) if u.strip()]

if not URLS:
    raise ValueError("M3U_URL secret is empty or not set!")

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "e_dir")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# We only define the .gz path to stay under GitHub's 100MB limit
OUTPUT_FILE_GZ = os.path.join(OUTPUT_DIR, f"{NAME}.xml.gz")

TVG_IDS = ['5', '6', '122', '153', '355', '374']
CHANNEL_PREFIX = "erw-"  # Set to "" if prefix is not required

def get_yesterday_cutoff_for_tz(target_tz):
    """Return yesterday's 00:00:00 in the given timezone."""
    now_in_tz = datetime.now(target_tz)
    return (now_in_tz - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

def parse_xmltv_datetime(start_str):
    """Parse '20261121083000 +0100' into a timezone-aware datetime."""
    start_str = start_str.strip()
    try:
        return datetime.strptime(start_str, "%Y%m%d%H%M%S %z")
    except ValueError:
        if len(start_str) == 14:
            return datetime.strptime(start_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return datetime.strptime(start_str, "%Y%m%d%H%M%S%z")

def sanitize_xml_bytes(content):
    """Strip bytes that are illegal in XML 1.0 but keep valid whitespace."""
    return re.sub(rb'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', b'', content)

def parse_xml(content, url):
    """Try strict stdlib parse first, fall back to lxml recovery, then sanitize + retry."""
    # 1. Fast path: strict stdlib parse
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        pass

    # 2. lxml with recover=True (handles most real-world malformed EPGs)
    if HAS_LXML:
        try:
            root_lxml = lxml_etree.fromstring(content, parser=lxml_etree.XMLParser(recover=True))
            # Convert lxml element back to stdlib ET so the rest of the script is unchanged
            return ET.fromstring(lxml_etree.tostring(root_lxml))
        except Exception:
            pass

    # 3. Strip illegal control characters and retry stdlib
    try:
        return ET.fromstring(sanitize_xml_bytes(content))
    except ET.ParseError as e:
        print(f"  ! Error: {e}")
        return None

def fetch_and_parse(url):
    try:
        print(f"Fetching EPG: {url.split('/')[-1]}")
        response = requests.get(url, timeout=60)
        if response.status_code != 200:
            return None
        content = response.content
        if url.endswith('.gz'):
            content = gzip.decompress(content)
        return parse_xml(content, url)
    except Exception as e:
        print(f"  ! Error: {e}")
        return None

def get_prog_title(prog):
    """Safely extract stripped title text from a <programme> element."""
    title_el = prog.find('title')
    if title_el is not None and title_el.text:
        return title_el.text.strip()
    return ""

def main():
    master_root = ET.Element('tv', {"generator-info-name": "TEST-GZ"})
    master_root.text = "\n"

    for url in URLS:
        epg_data = fetch_and_parse(url)
        if epg_data is None:
            continue

        # 1. Process <channel> elements
        for channel in epg_data.findall('channel'):
            orig_id = channel.get('id')
            if orig_id in TVG_IDS:
                channel.set('id', f"{CHANNEL_PREFIX}{orig_id}")
                master_root.append(channel)

        # 2. Single-pass processing of <programme> elements
        progs = epg_data.findall('programme')
        cutoff_dt = None
        last_seen_channel = None

        i = 0
        while i < len(progs):
            prog = progs[i]
            orig_channel = prog.get('channel')

            # Skip channels not in TVG_IDS
            if orig_channel not in TVG_IDS:
                i += 1
                continue

            # Parse start timestamp and filter out old programmes first
            try:
                prog_start_dt = parse_xmltv_datetime(prog.get('start', ''))
            except ValueError:
                i += 1
                continue

            if cutoff_dt is None:
                cutoff_dt = get_yesterday_cutoff_for_tz(prog_start_dt.tzinfo)
                print(f"  -> Using cutoff for this source: {cutoff_dt.isoformat()}")

            if prog_start_dt < cutoff_dt:
                i += 1
                continue

            # Detect the first retained programme of a channel block
            is_first_for_channel = (orig_channel != last_seen_channel)
            last_seen_channel = orig_channel

            # Rule 1: Ignore the first retained item of a channel if its title is "UNKNOWN"
            if is_first_for_channel and get_prog_title(prog) == "精彩呈现":
                i += 1
                continue

            # Rule 2: If stop ends with "235900" and the next item (same channel) is "UNKNOWN", merge them
            stop_attr = prog.get('stop', '').strip()
            if len(stop_attr) >= 14 and stop_attr[8:14] == "235900":
                if i + 1 < len(progs):
                    next_prog = progs[i + 1]
                    if next_prog.get('channel') == orig_channel and get_prog_title(next_prog) == "精彩呈现":
                        next_stop = next_prog.get('stop', '').strip()
                        if next_stop:
                            prog.set('stop', next_stop)
                        i += 1  # Advance index to skip the merged "UNKNOWN" item

            # Format and append valid programme
            prog.set('channel', f"{CHANNEL_PREFIX}{orig_channel}")

            title = prog.find('title')
            if title is not None and title.text:
                sub = prog.find('sub-title')
                if sub is not None and sub.text:
                    title.text = f"{title.text} {sub.text}"

            master_root.append(prog)
            i += 1

    # Write ONLY the .gz file to save space and stay under 100MB
    print(f"Saving compressed EPG to {OUTPUT_FILE_GZ}...")
    tree = ET.ElementTree(master_root)
    with gzip.open(OUTPUT_FILE_GZ, 'wb') as f:
        tree.write(f, encoding='utf-8', xml_declaration=True)
    
    print("M3U-Filtered EPG (.gz only) generation complete.")

if __name__ == "__main__":
    main()
