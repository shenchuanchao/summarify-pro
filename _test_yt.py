"""Test YouTube transcript fetching"""
import sys, json, traceback

# Test 1: youtube-transcript-api v1.2.4
print("=== Test 1: youtube-transcript-api v1.2.4 ===")
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    ytt = YouTubeTranscriptApi()
    video_id = "dQw4w9WgXcQ"  # Rick Astley - known to have captions
    print(f"Fetching transcript for: {video_id}")
    fetched = ytt.fetch(video_id)
    print(f"Type: {type(fetched).__name__}")
    print(f"Language: {fetched.language_code}")
    print(f"Snippets count: {len(fetched)}")
    print(f"First snippet: {fetched[0].text[:80]}")
    print("OK!")
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")

print()

# Test 2: innertube API approach
print("=== Test 2: Innertube API ===")
try:
    import requests, re, xml.etree.ElementTree as ET
    ua = 'com.google.android.youtube/19.33.35 (Linux; U; Android 14; en_US)'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': ua,
        'Origin': 'https://www.youtube.com',
    }
    clients = [
        {"client": {"clientName": "ANDROID", "clientVersion": "19.33.35", "hl": "en", "gl": "US"}},
        {"client": {"clientName": "IOS", "clientVersion": "19.33.35", "hl": "en", "gl": "US"}},
        {"client": {"clientName": "WEB", "clientVersion": "2.20250501.00.00", "hl": "en", "gl": "US"}},
    ]
    video_id = "dQw4w9WgXcQ"
    tracks = None
    for i, ctx in enumerate(clients):
        payload = {"context": ctx, "videoId": video_id}
        resp = requests.post('https://www.youtube.com/youtubei/v1/player', headers=headers, json=payload, timeout=15)
        print(f"Client {i+1} ({ctx['client']['clientName']}): HTTP {resp.status_code}")
        if resp.status_code == 200:
            pr = resp.json()
            tracks = pr.get('captions', {}).get('playerCaptionsTracklistRenderer', {}).get('captionTracks', [])
            if tracks:
                print(f"  Found {len(tracks)} tracks")
                for t in tracks[:3]:
                    print(f"  - {t.get('languageCode', '??')}: {t.get('name', {}).get('simpleText', 'N/A')}")
                break
            else:
                print("  No caption tracks")
    if not tracks:
        print("FAILED: No tracks from any client")
    else:
        print("OK!")
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()