import os
from functools import lru_cache

import msgspec
from dotenv import load_dotenv

load_dotenv()


class Settings(msgspec.Struct):
    database_url: str
    openai_api_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
    )