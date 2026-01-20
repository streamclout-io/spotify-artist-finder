from typing import List, Set
from sqlalchemy import select
from models.database import SearchProgress
from database.database import AsyncSessionLocal
import os
import csv
import random
import logging
from dotenv import load_dotenv
import multiprocessing


load_dotenv()
logger = logging.getLogger(__name__)

# Path to the artist prefixes CSV file
PREFIXES_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    'artist_prefixes.csv'
)


class SearchStringGenerator:
    """Generates search strings prioritizing 4-char prefixes from CSV, then random"""
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SearchStringGenerator, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._initialized = True
            self.max_workers = min(
                int(os.getenv('MAX_WORKERS', multiprocessing.cpu_count() * 2)),
                20  # Cap at 20 concurrent searches
            )
            self._prefixes: List[str] = []
            self._four_char_prefixes: List[str] = []
            self._other_prefixes: List[str] = []
            self._prefixes_loaded = False

    def _load_prefixes(self) -> None:
        """Load prefixes from CSV file and separate 4-char from others"""
        if self._prefixes_loaded:
            return

        try:
            with open(PREFIXES_CSV_PATH, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                # Skip header
                next(reader, None)

                for row in reader:
                    if row:
                        prefix = row[0].strip()
                        if prefix:
                            self._prefixes.append(prefix)
                            if len(prefix) == 4:
                                self._four_char_prefixes.append(prefix)
                            else:
                                self._other_prefixes.append(prefix)

            self._prefixes_loaded = True
            logger.info(
                f"Loaded {len(self._prefixes)} prefixes: "
                f"{len(self._four_char_prefixes)} 4-char, "
                f"{len(self._other_prefixes)} other"
            )
        except FileNotFoundError:
            logger.warning(f"Prefixes CSV not found at {PREFIXES_CSV_PATH}, using empty list")
            self._prefixes_loaded = True
        except Exception as e:
            logger.error(f"Error loading prefixes CSV: {e}")
            self._prefixes_loaded = True

    async def initialize(self) -> None:
        """Initialize by loading prefixes"""
        self._load_prefixes()

    async def _get_completed_searches(self) -> Set[str]:
        """Get all completed search strings from the database"""
        async with AsyncSessionLocal() as session:
            query = select(SearchProgress.query)
            result = await session.execute(query)
            return {row[0] for row in result.fetchall()}

    async def generate_batch(self) -> List[str]:
        """
        Generate a batch of search strings.
        Priority: 4-character prefixes first, then random from remaining.
        """
        await self.initialize()

        # Get already completed searches
        completed = await self._get_completed_searches()

        strings = []
        needed = self.max_workers

        # First, prioritize unsearched 4-character prefixes
        unsearched_4char = [p for p in self._four_char_prefixes if p not in completed]
        if unsearched_4char:
            # Shuffle to randomize which 4-char strings we pick
            random.shuffle(unsearched_4char)
            batch_from_4char = unsearched_4char[:needed]
            strings.extend(batch_from_4char)
            logger.info(f"Selected {len(batch_from_4char)} 4-char prefixes")

        # If we still need more, pick randomly from other unsearched prefixes
        if len(strings) < needed:
            remaining_needed = needed - len(strings)
            unsearched_other = [p for p in self._other_prefixes if p not in completed]

            if unsearched_other:
                random.shuffle(unsearched_other)
                batch_from_other = unsearched_other[:remaining_needed]
                strings.extend(batch_from_other)
                logger.info(f"Selected {len(batch_from_other)} other prefixes")

        # If we've exhausted all CSV prefixes, log a warning
        if not strings:
            logger.warning("All prefixes from CSV have been searched!")

        return strings
