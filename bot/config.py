"""
Configuration loaded from environment / .env file.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    poly_api_key: str = os.getenv("POLY_API_KEY", "")
    poly_api_secret: str = os.getenv("POLY_API_SECRET", "")
    poly_api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    poly_private_key: str = os.getenv("POLY_PRIVATE_KEY", "")

    max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", "50.0"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    poly_clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137


cfg = Config()
