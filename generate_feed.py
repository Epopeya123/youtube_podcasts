#!/usr/bin/env python3
"""Generate a podcast RSS feed from episodes.json."""

import json
import os
import sys
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

EPISODES_FILE = "episodes.json"
OUTPUT_DIR = "public"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "feed.xml")

# Podcast metadata — update these if you fork this repo
GITHUB_REPO = "epopeya123/youtube_podcasts"
GITHUB_PAGES_URL = f"https://epopeya123.github.io/youtube_podcasts"
RELEASE_TAG = "episodes"
RELEASE_BASE_URL = f"https://github.com/{GITHUB_REPO}/releases/download/{RELEASE_TAG}"

PODCAST_TITLE = "AI News & Strategy Daily (YouTube Audio)"
PODCAST_DESCRIPTION = (
    "Audio extracted from Nate B. Jones' YouTube channel 'AI News & Strategy Daily'. "
    "Daily AI strategy and news for the AI curious, builders, and executives."
)
PODCAST_AUTHOR = "Nate B. Jones"
PODCAST_LANGUAGE = "en"
PODCAST_LINK = f"https://www.youtube.com/@natebjones"

# iTunes namespace
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


def format_duration(seconds):
    """Format duration as HH:MM:SS or MM:SS."""
    if not seconds:
        return "00:00"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    secs = int(seconds) % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_pub_date(upload_date):
    """Convert YYYYMMDD to RFC 2822 date format."""
    if not upload_date or len(upload_date) != 8:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    try:
        dt = datetime.strptime(upload_date, "%Y%m%d").replace(
            hour=12, tzinfo=timezone.utc
        )
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def generate_feed(episodes):
    """Generate podcast RSS XML from episodes list."""
    rss = Element("rss")
    rss.set("version", "2.0")
    rss.set("xmlns:itunes", ITUNES_NS)
    rss.set("xmlns:atom", ATOM_NS)

    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = PODCAST_TITLE
    SubElement(channel, "description").text = PODCAST_DESCRIPTION
    SubElement(channel, "link").text = PODCAST_LINK
    SubElement(channel, "language").text = PODCAST_LANGUAGE
    SubElement(channel, "generator").text = "youtube_podcasts"

    # Atom self link
    feed_url = f"{GITHUB_PAGES_URL}/feed.xml"
    atom_link = SubElement(channel, "{%s}link" % ATOM_NS)
    atom_link.set("href", feed_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    # iTunes metadata
    SubElement(channel, "{%s}author" % ITUNES_NS).text = PODCAST_AUTHOR
    SubElement(channel, "{%s}explicit" % ITUNES_NS).text = "false"
    category = SubElement(channel, "{%s}category" % ITUNES_NS)
    category.set("text", "Technology")

    # Last build date
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    # Episodes (sorted newest first)
    sorted_episodes = sorted(
        episodes, key=lambda x: x.get("upload_date", ""), reverse=True
    )

    for ep in sorted_episodes:
        item = SubElement(channel, "item")

        SubElement(item, "title").text = ep.get("title", "Unknown")

        description = ep.get("description", "")
        # Truncate very long descriptions
        if len(description) > 4000:
            description = description[:4000] + "..."
        SubElement(item, "description").text = description

        # Enclosure (the MP3 file URL)
        audio_url = f"{RELEASE_BASE_URL}/{ep['filename']}"
        enclosure = SubElement(item, "enclosure")
        enclosure.set("url", audio_url)
        enclosure.set("length", str(ep.get("filesize", 0)))
        enclosure.set("type", "audio/mpeg")

        # GUID
        guid = SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = ep.get("id", ep.get("filename", ""))

        # Pub date
        SubElement(item, "pubDate").text = format_pub_date(ep.get("upload_date"))

        # iTunes metadata
        SubElement(item, "{%s}duration" % ITUNES_NS).text = format_duration(
            ep.get("duration")
        )
        SubElement(item, "{%s}explicit" % ITUNES_NS).text = "false"

    return rss


def main():
    if not os.path.exists(EPISODES_FILE):
        print(f"No {EPISODES_FILE} found. Run download_audio.py first.")
        sys.exit(1)

    with open(EPISODES_FILE, "r") as f:
        episodes = json.load(f)

    if not episodes:
        print("No episodes yet. Generating empty feed.")

    print(f"Generating RSS feed for {len(episodes)} episodes...")

    rss = generate_feed(episodes)

    # Pretty-print XML
    xml_string = tostring(rss, encoding="unicode", xml_declaration=False)
    xml_pretty = parseString(xml_string).toprettyxml(indent="  ", encoding="utf-8")

    # Write to output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "wb") as f:
        f.write(xml_pretty)

    print(f"Feed written to {OUTPUT_FILE}")
    print(f"Subscribe URL: {GITHUB_PAGES_URL}/feed.xml")


if __name__ == "__main__":
    main()
