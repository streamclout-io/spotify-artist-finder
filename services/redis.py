from redis.asyncio import Redis
from typing import List, Optional, Dict, Set
import time
import logging
from config.rate_limits import get_redis_rate_limit

logger = logging.getLogger(__name__)

# Batch ingestion configuration
BATCH_SIZE = 10
INGESTION_API_URL = "https://apiv2.streamclout.io/fetch/artists/full/batch"
MAX_ALBUMS = 500

class RedisService:
    def __init__(self, redis_url: str, max_workers: int = 20):
        self.redis: Optional[Redis] = None
        self.redis_url = redis_url
        self.max_workers = max_workers
        self.search_timeout = 300  # 5 minutes
        
        # Redis keys
        self.active_searches_key = "active_searches"
        self.active_searches_timestamps = f"{self.active_searches_key}:timestamps"
        self.requests_key = "api_requests"  # Using this as our main sorted set for requests
        self.pending_artists_key = "pending_artist_ids"  # List for batch ingestion
        self.pending_genres_key = "pending_artist_genres"  # Hash for genre batching
        rate_limit_config = get_redis_rate_limit()
        self.rate_limit_window = rate_limit_config["rate_limit_window"]
        self.rate_limit_max = rate_limit_config["rate_limit_max"]

    async def init(self):
        """Initialize Redis connection with retry logic"""
        if not self.redis:
            try:
                self.redis = Redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    max_connections=20
                )
                # Test connection
                await self.redis.ping()
                await self._cleanup_stale_searches()
            except Exception as e:
                logger.error(f"Failed to initialize Redis: {str(e)}")
                if self.redis:
                    await self.redis.close()
                    self.redis = None
                raise

    async def close(self):
        """Close Redis connection safely"""
        if self.redis:
            try:
                await self.redis.close()
            except Exception as e:
                logger.error(f"Error closing Redis connection: {str(e)}")
            finally:
                self.redis = None

    async def record_api_request(self, query: str, offset: int = 0, limit: int = 50) -> bool:
        """
        Record an API request with query details using a sorted set.
        Uses Redis MULTI/EXEC for atomic operations.
        """
        if not self.redis:
            await self.init()
            
        now = time.time()
        window_start = now - self.rate_limit_window
        request_key = f"{query}:{offset}:{now}"
        
        try:
            # Use Redis Lua script for atomic operation
            check_and_add_script = """
            local window_start = tonumber(ARGV[1])
            local now = tonumber(ARGV[2])
            local max_requests = tonumber(ARGV[3])
            
            -- Clean old requests
            redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, window_start)
            
            -- Get current count
            local count = redis.call('ZCOUNT', KEYS[1], window_start, '+inf')
            
            -- Only proceed if under limit
            if count >= max_requests then
                return 0
            end
            
            -- Add new request
            redis.call('ZADD', KEYS[1], now, ARGV[4])
            
            -- Store request details
            redis.call('HSET', 'request:' .. ARGV[4],
                'query', ARGV[5],
                'offset', ARGV[6],
                'limit', ARGV[7],
                'timestamp', tostring(now),
                'artists_found', '0'
            )
            
            -- Set expiration
            redis.call('EXPIRE', KEYS[1], 60)  -- Keep sorted set for 1 minute
            redis.call('EXPIRE', 'request:' .. ARGV[4], 60)
            
            return 1
            """
            
            # Register script once
            if not hasattr(self, '_check_and_add_script'):
                self._check_and_add_script = await self.redis.script_load(check_and_add_script)
            
            # Execute script atomically
            result = await self.redis.evalsha(
                self._check_and_add_script,
                1,  # number of keys
                self.requests_key,  # KEYS[1]
                window_start,  # ARGV[1]
                now,  # ARGV[2]
                self.rate_limit_max,  # ARGV[3]
                request_key,  # ARGV[4]
                query,  # ARGV[5]
                str(offset),  # ARGV[6]
                str(limit)  # ARGV[7]
            )
            
            return bool(result)
                
        except Exception as e:
            logger.error(f"Error recording API request: {str(e)}")
            return False

    async def update_request_artists(self, query: str, offset: int, artists_found: int):
        """Update the artists_found count for a specific request"""
        if not self.redis:
            await self.init()
            
        try:
            # Get all requests in the current window
            now = time.time()
            window_start = now - self.rate_limit_window
            
            # Find the matching request
            requests = await self.redis.zrangebyscore(
                self.requests_key,
                window_start,
                '+inf'
            )
            
            for request_key in requests:
                if request_key.startswith(f"{query}:{offset}:"):
                    # Update the artists_found count
                    await self.redis.hset(
                        f"request:{request_key}",
                        "artists_found",
                        artists_found
                    )
                    break
                    
        except Exception as e:
            logger.error(f"Error updating request artists: {str(e)}")

    async def get_window_requests(self) -> List[Dict]:
        """Get all requests in the current window with their details"""
        if not self.redis:
            await self.init()
            
        try:
            now = time.time()
            window_start = now - self.rate_limit_window
            
            # Get all request keys in window
            request_keys = await self.redis.zrangebyscore(
                self.requests_key,
                window_start,
                '+inf'
            )
            
            requests = []
            for request_key in request_keys:
                details = await self.redis.hgetall(f"request:{request_key}")
                if details:
                    try:
                        request_info = {
                            "query": details.get("query", ""),
                            "offset": int(details.get("offset", 0)),
                            "limit": int(details.get("limit", 50)),
                            "timestamp": float(details.get("timestamp", 0)),
                            "artists_found": int(details.get("artists_found", 0))
                        }
                        requests.append(request_info)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Error parsing request details: {str(e)}")
                        continue
            
            return sorted(requests, key=lambda x: x["timestamp"], reverse=True)
            
        except Exception as e:
            logger.error(f"Error getting window requests: {str(e)}")
            return []

    async def get_rate_limit_info(self) -> Dict:
        """Get current rate limit information"""
        if not self.redis:
            await self.init()
            
        now = time.time()
        window_start = now - self.rate_limit_window
        
        try:
            # Clean up old requests and get current count
            async with self.redis.pipeline() as pipe:
                await pipe.zremrangebyscore(self.requests_key, 0, window_start)
                await pipe.zrange(self.requests_key, 0, -1, withscores=True)
                _, requests = await pipe.execute()
            
            current_requests = len(requests)
            
            # Calculate wait time if at or near limit
            time_until_reset = 0
            if current_requests > 0 and current_requests >= self.rate_limit_max:
                oldest_timestamp = float(requests[0][1])
                time_until_reset = max(0, oldest_timestamp + self.rate_limit_window - now)
            
            return {
                "window_size": self.rate_limit_window,
                "current_requests": current_requests,
                "max_requests": self.rate_limit_max,
                "remaining_requests": max(0, self.rate_limit_max - current_requests),
                "time_until_next_request": time_until_reset,
                "window_start": window_start,
                "window_end": now
            }
        except Exception as e:
            logger.error(f"Error getting rate limit info: {str(e)}")
            return {
                "window_size": self.rate_limit_window,
                "current_requests": 0,
                "max_requests": self.rate_limit_max,
                "remaining_requests": self.rate_limit_max,
                "time_until_next_request": 0,
                "window_start": window_start,
                "window_end": now
            }

    # Active Search Management Methods
    async def add_active_search(self, search_string: str) -> bool:
        """Add search if under worker limit"""
        if not self.redis:
            await self.init()
            
        try:
            if await self.redis.scard(self.active_searches_key) >= self.max_workers:
                return False
                
            async with self.redis.pipeline() as pipe:
                current_time = str(time.time())
                await pipe.sadd(self.active_searches_key, search_string)
                await pipe.hset(self.active_searches_timestamps, search_string, current_time)
                await pipe.execute()
                
            logger.info(f"Added active search: {search_string}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding active search: {str(e)}")
            return False

    async def remove_active_search(self, search_string: str):
        """Remove a search with proper error handling"""
        if not self.redis:
            await self.init()
            
        try:
            async with self.redis.pipeline() as pipe:
                await pipe.srem(self.active_searches_key, search_string)
                await pipe.hdel(self.active_searches_timestamps, search_string)
                await pipe.execute()
                
            logger.info(f"Removed search: {search_string}")
            
        except Exception as e:
            logger.error(f"Error removing search {search_string}: {str(e)}")

    async def get_active_searches(self) -> List[str]:
        """Get current active searches"""
        if not self.redis:
            await self.init()
            
        await self._cleanup_stale_searches()
        return list(await self.redis.smembers(self.active_searches_key))

    async def get_active_search_count(self) -> int:
        """Get count of current active searches"""
        searches = await self.get_active_searches()
        return len(searches)

    async def _cleanup_stale_searches(self):
        """Remove expired searches"""
        if not self.redis:
            await self.init()
            
        try:
            current_time = time.time()
            searches = await self.redis.smembers(self.active_searches_key)
            timestamps = await self.redis.hgetall(self.active_searches_timestamps)
            
            for search in searches:
                if (search in timestamps and 
                    float(timestamps[search]) + self.search_timeout < current_time):
                    logger.info(f"Cleaning up stale search: {search}")
                    await self.remove_active_search(search)
        except Exception as e:
            logger.error(f"Error cleaning up stale searches: {str(e)}")

    # Batch Ingestion Methods
    async def add_pending_artists(self, artist_ids: List[str]) -> List[str]:
        """
        Add artist IDs to pending list and return a batch if we have 20+ artists.
        Returns a list of artist IDs to send to the ingestion API (empty if < 20).
        """
        if not self.redis:
            await self.init()

        if not artist_ids:
            return []

        try:
            # Add all artist IDs to the pending list
            await self.redis.rpush(self.pending_artists_key, *artist_ids)

            # Check current count
            count = await self.redis.llen(self.pending_artists_key)
            logger.info(f"Pending artists count: {count}")

            # If we have enough for a batch, pop them
            if count >= BATCH_SIZE:
                batch = []
                for _ in range(BATCH_SIZE):
                    artist_id = await self.redis.lpop(self.pending_artists_key)
                    if artist_id:
                        batch.append(artist_id)

                logger.info(f"Returning batch of {len(batch)} artists for ingestion")
                return batch

            return []

        except Exception as e:
            logger.error(f"Error adding pending artists: {str(e)}")
            return []

    async def get_pending_artist_count(self) -> int:
        """Get current count of pending artists"""
        if not self.redis:
            await self.init()

        try:
            return await self.redis.llen(self.pending_artists_key)
        except Exception as e:
            logger.error(f"Error getting pending artist count: {str(e)}")
            return 0

    # Genre Batching Methods
    async def add_pending_genres(self, genres_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """
        Add artist genres to pending hash and return a batch if we have enough.
        Returns a dict of artist_id -> genres to send to the API (empty if < BATCH_SIZE).
        """
        if not self.redis:
            await self.init()

        if not genres_map:
            return {}

        try:
            import json
            # Add all genres to the pending hash (artist_id -> JSON encoded genres list)
            for artist_id, genres in genres_map.items():
                await self.redis.hset(self.pending_genres_key, artist_id, json.dumps(genres))

            # Check current count
            count = await self.redis.hlen(self.pending_genres_key)
            logger.info(f"Pending genres count: {count}")

            # If we have enough for a batch, pop them
            if count >= BATCH_SIZE:
                # Get all pending genres
                all_genres = await self.redis.hgetall(self.pending_genres_key)

                # Take BATCH_SIZE items
                batch = {}
                keys_to_delete = []
                for i, (artist_id, genres_json) in enumerate(all_genres.items()):
                    if i >= BATCH_SIZE:
                        break
                    batch[artist_id] = json.loads(genres_json)
                    keys_to_delete.append(artist_id)

                # Delete the items we're returning
                if keys_to_delete:
                    await self.redis.hdel(self.pending_genres_key, *keys_to_delete)

                logger.info(f"Returning batch of {len(batch)} artist genres for API")
                return batch

            return {}

        except Exception as e:
            logger.error(f"Error adding pending genres: {str(e)}")
            return {}

    async def get_pending_genres_count(self) -> int:
        """Get current count of pending genres"""
        if not self.redis:
            await self.init()

        try:
            return await self.redis.hlen(self.pending_genres_key)
        except Exception as e:
            logger.error(f"Error getting pending genres count: {str(e)}")
            return 0