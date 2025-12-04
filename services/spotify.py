from typing import Optional
import httpx
import asyncio
from redis.asyncio import Redis
from services.redis import RedisService
from models.spotify import SpotifyArtist, SpotifyArtists, SpotifyToken
from datetime import datetime, timedelta
import logging
from config.rate_limits import get_spotify_rate_limit
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redis_url: str = "redis://localhost:6379/0",
        base_url: str = "https://api.spotify.com/v1",
        auth_url: str = "https://accounts.spotify.com/api/token",
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        bearer_token: Optional[str] = None,
        rate_limit_window: int = 30,
        rate_limit_max: int = 10
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self.auth_url = auth_url
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self._bearer_token = bearer_token
        self.redis_url = redis_url
        rate_limit_config = get_spotify_rate_limit()
        self.rate_limit_window = rate_limit_config["window_seconds"]
        self.rate_limit_max = rate_limit_config["max_requests"]
        
        # Services
        self._redis_service: Optional[RedisService] = None
        self._redis: Optional[Redis] = None
        self._initialized = False
        self.query_windows = {}  # Track rate limits per query
        
    async def _ensure_initialized(self):
        """Ensure all services are initialized"""
        if not self._initialized:
            try:
                self._redis_service = RedisService(
                    self.redis_url,
                    max_workers=5
                )
                await self._redis_service.init()
                self._redis = self._redis_service.redis
                self._initialized = True
            except Exception as e:
                logger.error(f"Failed to initialize services: {str(e)}")
                if self._redis_service:
                    await self._redis_service.close()
                self._redis_service = None
                self._redis = None
                raise

    async def _get_token(self) -> str:
        """Get authentication token - either from bearer token or client credentials"""
        if self._bearer_token:
            return self._bearer_token
            
        await self._ensure_initialized()
        token_key = "spotify:auth:token"
        
        # Check Redis for cached token
        token_data = await self._redis.get(token_key)
        if token_data:
            token = SpotifyToken.parse_raw(token_data)
            if datetime.now() < token.expires_at - timedelta(minutes=5):
                return token.access_token
        
        # Get new token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            
            token_data = response.json()
            token = SpotifyToken(
                access_token=token_data["access_token"],
                token_type=token_data["token_type"],
                expires_in=token_data["expires_in"],
                expires_at=datetime.now() + timedelta(seconds=token_data["expires_in"])
            )
            
            # Cache in Redis
            await self._redis.set(
                token_key, 
                token.json(), 
                ex=token_data["expires_in"] - 300  # Expire 5 mins early
            )
            
            return token.access_token

    async def _make_request(
        self, 
        method: str, 
        url: str, 
        **kwargs
    ) -> httpx.Response:
        """Make HTTP request with optimized rate limiting"""
        await self._ensure_initialized()
        
        try:
            # Extract query parameters for rate limiting
            params = kwargs.get('params', {})
            query = params.get('q', '')
            offset = params.get('offset', 0)
            limit = params.get('limit', 50)
            
            # Try to record the request - this is our single source of truth for rate limiting
            while True:
                recorded = await self._redis_service.record_api_request(
                    query=query,
                    offset=offset,
                    limit=limit
                )
                if recorded:
                    break
                    
                # If we couldn't record, get precise timing for next available slot
                rate_info = await self._redis_service.get_rate_limit_info()
                if rate_info["time_until_next_request"] > 0:
                    # Only sleep for the exact time needed, with a tiny buffer
                    await asyncio.sleep(rate_info["time_until_next_request"] + 0.01)
                
            # Get token and make request
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            kwargs['headers'] = headers
            
            async with httpx.AsyncClient() as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
                
        except Exception as e:
            logger.error(f"Error in _make_request: {str(e)}")
            raise

    async def search_artists(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0
    ) -> SpotifyArtists:
        """Search for artists with rate limiting and retries"""
        if limit > 50:
            raise ValueError("Maximum limit is 50")
        
        # Ensure initialization before search
        await self._ensure_initialized()
        
        response = await self._make_request(
            'GET',
            f"{self.base_url}/search",
            params={
                "q": query,
                "type": "artist",
                "limit": limit,
                "offset": offset
            }
        )
        
        search_results = response.json()
        artists = [
            SpotifyArtist(
                id=artist["id"],
                name=artist["name"],
                genres=artist.get("genres", []),
                popularity=artist.get("popularity", 0)
            )
            for artist in search_results["artists"]["items"]
        ]
        
        # Update Redis with the number of artists found
        await self._redis_service.update_request_artists(
            query=query,
            offset=offset,
            artists_found=len(artists)
        )
        
        return SpotifyArtists(artists=artists)

    async def close(self):
        """Close all connections"""
        if self._redis_service:
            try:
                await self._redis_service.close()
            except Exception as e:
                logger.error(f"Error closing Redis service: {str(e)}")
            finally:
                self._redis_service = None
                self._redis = None
                self._initialized = False