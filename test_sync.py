#!/usr/bin/env python3
"""
Test script to demonstrate data synchronization features
"""
import requests
import time
import json

BASE_URL = "http://localhost:8000"

def test_caching_and_sync():
    print("=" * 60)
    print("Testing Data Synchronization Features")
    print("=" * 60)
    
    username = "torvalds"  # Using a known GitHub user
    
    # Test 1: First request (cold cache)
    print("\n1. First Request (Cold Cache)")
    print("-" * 60)
    start = time.time()
    response = requests.get(f"{BASE_URL}/analyze/{username}")
    elapsed = time.time() - start
    
    print(f"Status: {response.status_code}")
    print(f"Time: {elapsed:.3f}s")
    print(f"ETag: {response.headers.get('ETag')}")
    print(f"Cache-Control: {response.headers.get('Cache-Control')}")
    
    # Store ETag
    etag = response.headers.get('ETag')
    data1 = response.json()
    print(f"Data timestamp: {data1.get('last_updated')}")
    
    # Test 2: Second request (warm cache)
    print("\n2. Second Request (Warm Cache)")
    print("-" * 60)
    time.sleep(1)
    start = time.time()
    response = requests.get(f"{BASE_URL}/analyze/{username}")
    elapsed = time.time() - start
    
    print(f"Status: {response.status_code}")
    print(f"Time: {elapsed:.3f}s (should be much faster!)")
    print(f"ETag: {response.headers.get('ETag')}")
    data2 = response.json()
    print(f"Data timestamp: {data2.get('last_updated')}")
    print(f"Same data: {data1 == data2}")
    
    # Test 3: Request with ETag (304 response)
    print("\n3. Request with ETag (Conditional GET)")
    print("-" * 60)
    headers = {"If-None-Match": etag}
    start = time.time()
    response = requests.get(f"{BASE_URL}/analyze/{username}", headers=headers)
    elapsed = time.time() - start
    
    print(f"Status: {response.status_code}")
    if response.status_code == 304:
        print("✓ Got 304 Not Modified - data unchanged!")
        print(f"Time: {elapsed:.3f}s")
        print(f"Response size: {len(response.content)} bytes (minimal!)")
    else:
        print("✗ Expected 304, got full response")
    
    # Test 4: Cache stats
    print("\n4. Cache Statistics")
    print("-" * 60)
    response = requests.get(f"{BASE_URL}/cache/stats")
    stats = response.json()
    print(json.dumps(stats, indent=2))
    
    # Test 5: Multiple endpoints
    print("\n5. Testing Multiple Endpoints")
    print("-" * 60)
    
    endpoints = [
        ("leetcode", "tourist"),
        ("codeforces", "tourist"),
    ]
    
    for endpoint, handle in endpoints:
        print(f"\nTesting /{endpoint}/{handle}")
        start = time.time()
        response = requests.get(f"{BASE_URL}/{endpoint}/{handle}")
        elapsed = time.time() - start
        
        print(f"  Status: {response.status_code}")
        print(f"  Time: {elapsed:.3f}s")
        print(f"  ETag: {response.headers.get('ETag')}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"  Timestamp: {data.get('last_updated')}")
    
    print("\n" + "=" * 60)
    print("✓ Synchronization tests complete!")
    print("=" * 60)
    print("\nKey Benefits:")
    print("- Cached responses are 10-100x faster")
    print("- 304 responses save bandwidth (< 1KB vs 10-100KB)")
    print("- Data consistent across all devices within cache TTL")
    print("- Rate limit protection for external APIs")

if __name__ == "__main__":
    print("\nMake sure the server is running:")
    print("uvicorn main:app --port 8000 --reload\n")
    
    try:
        # Check if server is running
        response = requests.get(BASE_URL, timeout=2)
        if response.status_code == 200:
            test_caching_and_sync()
        else:
            print("Server responded but with unexpected status")
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot connect to server. Please start it first:")
        print("  uvicorn main:app --port 8000 --reload")
    except Exception as e:
        print(f"ERROR: {e}")
