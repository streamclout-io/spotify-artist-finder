# Artist Genres API

Endpoint for storing artist genre mappings from Spotify API.

## Endpoint

```
POST /db/insert/artist-genres
```

## Request

**Content-Type:** `application/json`

**Body:** A JSON object where keys are Spotify artist IDs and values are arrays of genre strings.

```json
{
  "4LLpKhyESsyAXpc4laK94U": ["pop", "dance pop", "post-teen pop"],
  "1Xyo4u8uXC1ZmMpatF05PJ": ["pop", "canadian pop"],
  "6qqNVTkY8uBg9cP3Jd7DAH": []
}
```

- Genres can be an empty array `[]` if the artist has no genres
- Null values will be converted to empty arrays automatically

## Response

**Success (200):**
```json
{
  "status": "success",
  "message": "Appended genres for 3 artists",
  "count": 3,
  "details": {
    "status": "success",
    "table": "artist_genres",
    "records": 3,
    "bytes": 1234,
    "s3_key": "artist_genres/batch_20260111_143052_a1b2c3d4.parquet"
  }
}
```

**Empty request (200):**
```json
{
  "status": "skipped",
  "message": "No genres provided",
  "count": 0
}
```

**Error (500):**
```json
{
  "detail": "Failed to append artist genres: <error message>"
}
```

## Python Example

```python
import requests

STREAMCLOUT_API = "https://your-api-url.com"

def upload_artist_genres(genres_map: dict[str, list[str]]):
    """
    Upload artist genres to StreamClout.

    Args:
        genres_map: Dict mapping artist_id -> list of genre strings
    """
    response = requests.post(
        f"{STREAMCLOUT_API}/db/insert/artist-genres",
        json=genres_map,
        timeout=30
    )
    response.raise_for_status()
    return response.json()

# Example usage with batch from Spotify API
genres_batch = {
    "4LLpKhyESsyAXpc4laK94U": ["pop", "dance pop"],
    "1Xyo4u8uXC1ZmMpatF05PJ": ["pop", "canadian pop"],
}

result = upload_artist_genres(genres_batch)
print(f"Uploaded genres for {result['count']} artists")
```

## Batching Recommendation

For efficiency, batch multiple artists per request rather than one at a time:

```python
# Collect genres as you fetch artists
genres_buffer = {}

for artist in fetch_artists_batch():
    artist_id = artist["id"]
    genres = artist.get("genres", [])
    genres_buffer[artist_id] = genres

    # Upload every 50 artists
    if len(genres_buffer) >= 50:
        upload_artist_genres(genres_buffer)
        genres_buffer = {}

# Don't forget remaining items
if genres_buffer:
    upload_artist_genres(genres_buffer)
```

## Data Storage

- **Table:** `streamclout.artist_genres`
- **Location:** `s3://streamclout-data/artist_genres/`
- **No TTL** - genres persist indefinitely
- **Deduplication:** Use `streamclout.artist_genres_latest` view to get most recent genres per artist

## Querying Genres (Athena)

```sql
-- Get genres for a specific artist
SELECT artist_id, genres
FROM streamclout.artist_genres_latest
WHERE artist_id = '4LLpKhyESsyAXpc4laK94U';

-- Find artists by genre
SELECT artist_id, genres
FROM streamclout.artist_genres_latest
WHERE contains(genres, 'pop');

-- Count artists per genre (unnest the array)
SELECT genre, COUNT(*) as artist_count
FROM streamclout.artist_genres_latest
CROSS JOIN UNNEST(genres) AS t(genre)
GROUP BY genre
ORDER BY artist_count DESC;
```
