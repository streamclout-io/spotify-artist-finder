from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from typing import List, Set
from models.database import Artist
from models.spotify import SpotifyArtist
import logging

logger = logging.getLogger(__name__)

class DatabaseService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_artists(self, artists: List[SpotifyArtist]) -> Set[str]:
        """
        Upsert multiple artists into the database with explicit transaction.
        Returns the set of NEW artist IDs (ones that didn't exist before).
        """
        if not artists:
            return set()

        try:
            # First, check which artists already exist
            artist_ids = [artist.id for artist in artists]
            existing_ids = await self._get_existing_artist_ids(artist_ids)
            new_ids = set(artist_ids) - existing_ids

            # Prepare values for upsert
            values = [
                {
                    "id": artist.id,
                    "name": artist.name,
                    "genres": artist.genres,
                    "popularity": artist.popularity
                }
                for artist in artists
            ]

            # Construct upsert statement
            stmt = insert(Artist).values(values)
            stmt = stmt.on_conflict_do_nothing()

            await self.session.execute(stmt)
            await self.session.commit()
            logger.info(f"Successfully upserted {len(artists)} artists ({len(new_ids)} new)")

            return new_ids

        except Exception as e:
            logger.error(f"Failed to upsert artists: {str(e)}")
            await self.session.rollback()
            raise

    async def _get_existing_artist_ids(self, artist_ids: List[str]) -> Set[str]:
        """Get set of artist IDs that already exist in the database"""
        if not artist_ids:
            return set()

        stmt = select(Artist.id).where(Artist.id.in_(artist_ids))
        result = await self.session.execute(stmt)
        return {row[0] for row in result}
    
    async def get_missing_artist_ids(self, artist_ids: List[str]) -> Set[str]:
        """
        Given a list of artist IDs, return set of IDs that don't exist in database.
        """
        if not artist_ids:
            return set()
            
        # Query existing artists
        stmt = select(Artist.id).where(Artist.id.in_(artist_ids))
        result = await self.session.execute(stmt)
        existing_ids = {row[0] for row in result}
        
        # Return ids that don't exist
        return set(artist_ids) - existing_ids
    