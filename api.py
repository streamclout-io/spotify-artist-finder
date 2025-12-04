# main.py
from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
from typing import Dict
from models.spotify import SpotifyArtists
from services.spotify import SpotifyClient
from services.redis import RedisService
from contextlib import asynccontextmanager
from database.database import get_db
import os
from dotenv import load_dotenv
from database.setup import ensure_database_exists
from sqlalchemy.sql import func
from sqlalchemy import select
from models.database import Artist, SearchProgress
from fastapi.middleware.cors import CORSMiddleware
import logging


logger = logging.getLogger(__name__)
load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create database if needed and sync Redis with Postgres
    ensure_database_exists()
    
    yield  # yields control back to FastAPI
    
    # Shutdown: Add any cleanup here if needed
    pass

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # More permissive for development
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_spotify_client():
    return SpotifyClient(
        client_id=os.getenv('SPOTIFY_CLIENT_ID'),
        client_secret=os.getenv('SPOTIFY_CLIENT_SECRET')
    )

async def get_redis_service():
    redis_service = RedisService(
        redis_url=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    )
    await redis_service.init()
    try:
        yield redis_service
    finally:
        await redis_service.close()
    
@app.get("/search", response_model=SpotifyArtists)
async def search_artists(
    q: str = Query(..., description="Search query string"),
    offset: int = Query(default=0, ge=0, le=950),
    spotify_client: SpotifyClient = Depends(get_spotify_client),
):
    """Search for artists using Spotify API"""
    try:
        return await spotify_client.search_artists(
            query=q,
            limit=50,  # hardcoded for max value
            offset=offset
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))

@app.get("/status", response_model=Dict)
async def get_system_status(
    redis_service: RedisService = Depends(get_redis_service),
    db: AsyncSession = Depends(get_db)
):
    """Get overall system status including active searches and rate limits"""
    # Get active searches from Redis
    active_searches = await redis_service.get_active_searches()
    
    # Get rate limit info and window requests
    rate_limit_info = await redis_service.get_rate_limit_info()
    window_requests = await redis_service.get_window_requests()
    
    # Get total artists count
    artist_count_query = select(func.count()).select_from(Artist)
    artist_count = await db.execute(artist_count_query)
    total_artists = artist_count.scalar()
    
    # Get total searches count
    search_count_query = select(func.count()).select_from(SearchProgress)
    search_count = await db.execute(search_count_query)
    total_searches = search_count.scalar()
    
    # Get earliest search time
    earliest_search_query = select(func.min(SearchProgress.created_at)).select_from(SearchProgress)
    earliest_search_result = await db.execute(earliest_search_query)
    earliest_search_time = earliest_search_result.scalar()
    
    # Get recent searches
    recent_searches_query = (
        select(SearchProgress)
        .order_by(SearchProgress.created_at.desc())
        .limit(10)
    )
    recent_searches_result = await db.execute(recent_searches_query)
    recent_searches = recent_searches_result.scalars().all()
    
    return {
        "active_searches": active_searches,
        "active_search_count": len(active_searches),
        "rate_limit_status": rate_limit_info,
        "window_requests": window_requests,
        "total_artists_collected": total_artists,
        "total_searches_completed": total_searches,
        "earliest_search_time": earliest_search_time,
        "recent_searches": [
            {
                "query": search.query,
                "artists_found": search.artists,
                "created_at": search.created_at.isoformat()
            } for search in recent_searches
        ]
    }

