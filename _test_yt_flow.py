"""Test full YouTube flow: fetch transcript + AI generate"""
import requests, json, sys, os

BASE = "https://summarify-pro-production.up.railway.app"
anon_id = "test-anon-12345"

# Step 1: Fetch transcript
print("=== Step 1: Fetch transcript ===")
r1 = requests.post(f"{BASE}/api/youtube/transcript", 
    headers={"Content-Type": "application/json", "X-Anon-Id": anon_id},
    json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    timeout=30)
print(f"Status: {r1.status_code}")
if r1.ok:
    d1 = r1.json()
    print(f"  language: {d1.get('language')}")
    print(f"  word_count: {d1.get('word_count')}")
    print(f"  text preview: {d1.get('text', '')[:80]}...")
    print(f"  remaining: {d1.get('remaining')}")
    transcript_text = d1['text']
else:
    print(f"  Error: {r1.text[:200]}")
    sys.exit(1)

print()

# Step 2: AI Generate - summary
print("=== Step 2: AI Generate (summary) ===")
r2 = requests.post(f"{BASE}/api/ai/generate",
    headers={"Content-Type": "application/json", "X-Anon-Id": anon_id},
    json={"text": transcript_text[:3000], "action": "summary"},
    timeout=60)
print(f"Status: {r2.status_code}")
if r2.ok:
    d2 = r2.json()
    print(f"  result preview: {d2.get('result', '')[:100]}...")
else:
    print(f"  Error: {r2.text[:200]}")

print()
print("=== Done ===")