from celery import group
from celery_config import celery_app
import asyncio
from sqlalchemy import select
from models.database import SearchProgress
from services.spotify import SpotifyClient
from services.database import DatabaseService
from database.database import AsyncSessionLocal
from services.redis import RedisService, INGESTION_API_URL, MAX_ALBUMS
import os
from dotenv import load_dotenv
import httpx
import logging
import backoff
from datetime import datetime, timezone
from services.search_generator import SearchStringGenerator

logger = logging.getLogger(__name__)
load_dotenv()


async def send_batch_to_ingestion_api(artist_ids: list[str]) -> bool:
    """Send a batch of artist IDs to the ingestion API"""
    if not artist_ids:
        return True

    payload = {
        "artist_ids": artist_ids,
        "max_albums": MAX_ALBUMS,
        "force": False,
        "trigger_compaction": False
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(INGESTION_API_URL, json=payload)
            response.raise_for_status()
            logger.info(f"Successfully sent batch of {len(artist_ids)} artists to ingestion API")
            return True
    except httpx.HTTPStatusError as e:
        logger.error(f"Ingestion API HTTP error: {e.response.status_code} - {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Failed to send batch to ingestion API: {str(e)}")
        return False

@celery_app.task(name='tasks.generate_search_strings')
def generate_search_strings():
    """Generate next batch of search strings based on available capacity"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(_async_generate_search_strings())

async def _async_generate_search_strings():
    """Async implementation of search string generation"""
    redis_service = RedisService(os.getenv('REDIS_URL', 'redis://localhost:6379/0'))
    await redis_service.init()
    
    try:
        # Get current active search count
        active_count = await redis_service.get_active_search_count()
        available_slots = redis_service.max_workers - active_count
        
        logger.info(f"Active searches: {active_count}, Available slots: {available_slots}")
        
        if available_slots <= 0:
            return {"generated_strings": []}
        
        # Get next batch of search strings
        generator = SearchStringGenerator()
        await generator.initialize()
        search_strings = await generator.generate_batch()
        
        # Add new searches to Redis
        added_strings = []
        for search_str in search_strings:
            if await redis_service.add_active_search(search_str):
                added_strings.append(search_str)
                logger.info(f"Added search string to Redis: {search_str}")
        
        # Spawn group of search tasks
        if added_strings:
            logger.info(f"Spawning search tasks for strings: {added_strings}")
            search_group = group(search_artist_string.s(search_str) for search_str in added_strings)
            search_group.apply_async()
        
        return {
            "generated_strings": added_strings
        }
        
    except Exception as e:
        logger.error(f"Error in generate_search_strings: {str(e)}")
        raise
    finally:
        await redis_service.close()

@celery_app.task(
    name='tasks.search_artist_string',
    bind=True,
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=300,  # Max 5 minutes between retries
    retry_jitter=True
)
def search_artist_string(self, search_string: str):
    """Search for artists using a specific string until no more results"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    try:
        logger.info(f"Starting search for string: {search_string}")
        return loop.run_until_complete(_async_search_artist_string(search_string))
    except Exception as exc:
        retry_count = self.request.retries
        
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = int(exc.response.headers.get('Retry-After', 30))
            logger.warning(f"Rate limit error for {search_string}, retrying in {retry_after}s (attempt {retry_count + 1})")
            
            # Clean up Redis before retry
            loop.run_until_complete(_cleanup_failed_search(search_string))
            raise self.retry(exc=exc, countdown=retry_after)
        
        logger.error(f"Non-retryable error in search_artist_string for {search_string}: {str(exc)}")
        loop.run_until_complete(_cleanup_failed_search(search_string))
        raise

async def _async_search_artist_string(search_string: str):
    """Async implementation of artist search with immediate replacement"""
    spotify_client = None
    redis_service = None
    
    try:
        spotify_client = SpotifyClient(
            client_id=os.getenv('SPOTIFY_CLIENT_ID'),
            client_secret=os.getenv('SPOTIFY_CLIENT_SECRET'),
            redis_url=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
            bearer_token=os.getenv('SPOTIFY_BEARER_TOKEN'),
            rate_limit_window=30,
            rate_limit_max=10
        )
        
        redis_service = RedisService(os.getenv('REDIS_URL', 'redis://localhost:6379/0'))
        await redis_service.init()
        
        offset = 0
        total_artists = []
        
        async with AsyncSessionLocal() as session:
            db_service = DatabaseService(session)
            
            # First check if this search has already been completed
            existing_search = await session.execute(
                select(SearchProgress).where(SearchProgress.query == search_string)
            )
            if existing_search.scalar_one_or_none():
                logger.info(f"Search for {search_string} already completed, skipping")
                await redis_service.remove_active_search(search_string)
                # Immediately queue a new search to replace this one
                await _queue_next_search(redis_service)
                return {
                    "search_string": search_string,
                    "status": "already_completed"
                }
            
            while offset <= 950:  # Ensure we never exceed 950
                try:
                    logger.info(f"Searching {search_string} with offset {offset}")
                    result = await spotify_client.search_artists(
                        query=search_string,
                        offset=offset
                    )
                except Exception as e:
                    logger.error(f"Error searching {search_string} at offset {offset}: {str(e)}")
                    await redis_service.remove_active_search(search_string)
                    await _queue_next_search(redis_service)
                    raise
                
                current_batch_size = len(result.artists)
                logger.info(f"Found {current_batch_size} artists for {search_string} at offset {offset}")
                
                if result.artists:
                    # upsert_artists now returns only NEW artist IDs
                    new_artist_ids = await db_service.upsert_artists(result.artists)
                    total_artists.extend(result.artists)

                    # Add new artists to pending batch and check if we need to send
                    if new_artist_ids:
                        batch_to_send = await redis_service.add_pending_artists(list(new_artist_ids))
                        if batch_to_send:
                            await send_batch_to_ingestion_api(batch_to_send)
                
                if current_batch_size == 0 or current_batch_size < 50:
                    break
                    
                # Calculate next offset
                next_offset = offset + 50
                if next_offset > 950:  # Check if next offset would exceed limit
                    break
                    
                offset = next_offset
            
            # Record search completion and queue next search immediately
            try:
                search_progress = SearchProgress(
                    query=search_string,
                    artists=len(total_artists),
                    created_at=datetime.now(timezone.utc)
                )
                session.add(search_progress)
                await session.flush()
                await session.commit()
                logger.info(f"Successfully recorded search progress for {search_string}")
                
                # Remove this search and queue next one immediately
                await redis_service.remove_active_search(search_string)
                await _queue_next_search(redis_service)
                
            except Exception as e:
                if 'UniqueViolation' in str(e):
                    logger.warning(f"Search progress for {search_string} already exists, continuing")
                    await session.rollback()
                else:
                    logger.error(f"Failed to record search progress: {str(e)}")
                    await session.rollback()
                    raise
            
            logger.info(f"Completed search for {search_string}: {len(total_artists)} artists found")
            
    except Exception as e:
        logger.error(f"Error in _async_search_artist_string for {search_string}: {str(e)}")
        if redis_service:
            await redis_service.remove_active_search(search_string)
            await _queue_next_search(redis_service)
        raise
        
    finally:
        if redis_service:
            await redis_service.close()
        if spotify_client:
            await spotify_client.close()
    
    return {
        "search_string": search_string,
        "total_artists": len(total_artists),
        "final_offset": offset
    }

async def _queue_next_search(redis_service: RedisService):
    """Immediately queue next search when a slot opens"""
    try:
        # Get current active count
        active_count = await redis_service.get_active_search_count()
        
        if active_count >= redis_service.max_workers:
            return
            
        # Generate and queue next search immediately
        generator = SearchStringGenerator()
        await generator.initialize()
        search_strings = await generator.generate_batch()
        
        for search_str in search_strings:
            if await redis_service.add_active_search(search_str):
                # Spawn search task immediately
                search_artist_string.apply_async((search_str,))
                break  # Only queue one search to replace the completed one
                
    except Exception as e:
        logger.error(f"Error queueing next search: {str(e)}")

@backoff.on_exception(
    backoff.expo,
    Exception,
    max_tries=5,
    max_time=30
)
async def _cleanup_failed_search(search_string: str):
    """Clean up Redis after a failed search with retry logic"""
    redis_service = RedisService(os.getenv('REDIS_URL', 'redis://localhost:6379/0'))
    await redis_service.init()
    try:
        await redis_service.remove_active_search(search_string)
        logger.info(f"Cleaned up failed search from Redis: {search_string}")
    except Exception as e:
        logger.error(f"Error cleaning up failed search {search_string}: {str(e)}")
        raise
    finally:
        await redis_service.close()